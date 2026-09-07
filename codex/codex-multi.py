#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex-multi.py — 跨平台 codex 多模型切换启动器（本地 Linux / 本地 Windows / 远程 Linux）

一个入口搞定三种场景的模型切换：
  1. 本地（Linux/Windows）: codex --profile <档位>（档位写死在脚本，不读 models.json）
  2. 远程 Linux app-server : SSH 到服务器改 config.toml 的 model 并重启，再 codex --remote 连上
  3. MCP 多模型团队       : 启动 mcp_server.py，codex 会话内可直接调度多模型团队

网关 API 与档位全部硬编码（单人使用）。

用法:
    python codex-multi.py                      # 数字交互菜单
    python codex-multi.py luna                 # 本地 codex --profile luna
    python codex-multi.py luna --exec "..."    # 本地 codex exec -m（非交互）
    python codex-multi.py list                 # 列档位
    python codex-multi.py remote luna          # 切远程模型并连 codex --remote
    python codex-multi.py remote luna --set-only  # 只切远程模型，不连
    python codex-multi.py remote --exec "..."  # 远程非交互（走 codex-remote-cli.py）
    python codex-multi.py mcp                  # 启动 MCP 多模型调度 server

依赖: 仅 Python 标准库 + 系统 codex / ssh / paramiko(远程时)。
"""
import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = platform.system() == "Windows"

# --------------------------------------------------------------------------- #
# 硬编码配置（单人使用，不读 models.json / 环境变量）
# --------------------------------------------------------------------------- #

# 网关 API（写死）
GATEWAY_BASE_URL = "https://cfapi.1232333.xyz/v1"
GATEWAY_API_KEY = "sk-wa-f9cb7d4ba48f403797fc3f55b928ceac"
LOCAL_BASE_URL = "http://127.0.0.1:8787/v1"   # 本地 ws-proxy（公司代理）

# 档位 -> (模型假名, reasoning effort, 说明)。profile 文件名 = 档位名。
PROFILES = {
    "fast":  ("gpt-5.6-luna-fast", "low",    "快速"),
    "sfast": ("gpt-5.6-sol-fast",  "low",    "最快"),
    "mid":   ("gpt-5.6-sol",       "medium", "深度推理"),
    "code":  ("gpt-5.6-luna",      "high",   "写码主力"),
    "deep":  ("gpt-6-astra",       "high",   "旗舰推理"),
}
DEFAULT_PROFILE = "code"

# 短别名 -> 档位名（直接写假名也能用，由 resolve 兜底）
ALIASES = {
    "astra": "gpt-6-astra",
    "sol": "gpt-5.6-sol",
    "luna": "gpt-5.6-luna",
    "sol-fast": "gpt-5.6-sol-fast",
    "luna-fast": "gpt-5.6-luna-fast",
}

# .env 优先（REMOTE_* 等），没有 .env 则用下面内置值
def _load_dotenv(path):
    cfg = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                if k:
                    cfg[k] = v
    except OSError:
        pass
    return cfg


_DOTENV = _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


def _env(key, default=""):
    return os.environ.get(key) or _DOTENV.get(key) or default


# 远程服务器（.env 可覆盖）
REMOTE_HOST = _env("REMOTE_HOST", "104.223.65.202")
REMOTE_PORT = int(_env("REMOTE_PORT", "10122"))
REMOTE_USER = _env("REMOTE_USER", "root")
REMOTE_PASS = _env("REMOTE_PASS", "mzyxc8520#")
REMOTE_WS_PORT = int(_env("REMOTE_WS_PORT", "20130"))
REMOTE_MODEL_PY = _env("REMOTE_MULTI_MODEL_PY", "/opt/codex/remote-model.py")

# 本地 codex 可执行路径（Windows 需解析 npm shim；Linux 直接用 codex）
LOCAL_CODEX_BIN = None


def resolve_model(name):
    """档位名/别名 -> 模型假名；未知输入原样返回。"""
    name = (name or "").strip()
    if name in PROFILES:
        return PROFILES[name][0]
    if name in ALIASES:
        return ALIASES[name]
    return name


def resolve_codex_bin():
    global LOCAL_CODEX_BIN
    if LOCAL_CODEX_BIN:
        return LOCAL_CODEX_BIN
    # Windows: codex 是 npm shim (.cmd)，subprocess 直接执行会 WinError 2
    if IS_WINDOWS:
        known = [
            r"C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\27d6a192e9c98618\codex.exe",
        ]
        for p in known:
            if os.path.exists(p):
                LOCAL_CODEX_BIN = p
                return p
        found = shutil.which("codex")
        if found:
            LOCAL_CODEX_BIN = found
            return found
    else:
        found = shutil.which("codex")
        if found:
            LOCAL_CODEX_BIN = found
            return found
    return "codex"


def remote_token():
    """远程 capability token：优先读 codex-remote-config.json，其次 ws-cap-token.txt。"""
    cfg_path = os.path.join(SCRIPT_DIR, "codex-remote-config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("token"):
                return data["token"]
        except Exception:
            pass
    for p in [os.path.expanduser("~/.codex/ws-cap-token.txt")]:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    return ""


# --------------------------------------------------------------------------- #
# 本地启动
# --------------------------------------------------------------------------- #
def run_local(profile, exec_prompt=None):
    model = resolve_model(profile) if profile else None
    bin_path = resolve_codex_bin()
    if exec_prompt is not None:
        cmd = [bin_path, "exec", "--sandbox", "danger-full-access"]
        if model:
            cmd += ["-c", f"model={model}"]
        cmd += [exec_prompt]
    else:
        cmd = [bin_path]
        if model:
            cmd += ["--profile", profile] if profile in PROFILES else ["-c", f"model={model}"]
    print(f"[本地 codex] 模型: {model or '默认'}  网关: {LOCAL_BASE_URL}")
    print("  执行:", " ".join(cmd))
    subprocess.run(cmd)
    sys.exit(0)


# --------------------------------------------------------------------------- #
# 远程 Linux
# --------------------------------------------------------------------------- #
def ssh_connect():
    try:
        import paramiko
    except ImportError:
        print("缺少 paramiko，请先 pip install paramiko（远程切模型需要）", file=sys.stderr)
        sys.exit(1)
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(REMOTE_HOST, port=REMOTE_PORT, username=REMOTE_USER,
              password=REMOTE_PASS, timeout=20)
    return c


def remote_set_model(model, ssh=None):
    """在服务器上切模型：优先 remote-model.py，否则直接改 config.toml。"""
    own = ssh is None
    if own:
        ssh = ssh_connect()
    try:
        cmd = f"python3 {shlex.quote(REMOTE_MODEL_PY)} set {shlex.quote(model)}"
        _, o, e = ssh.exec_command(cmd, timeout=120)
        out = o.read().decode("utf-8", "replace").strip()
        err = e.read().decode("utf-8", "replace").strip()
        rc = o.channel.recv_exit_status()
        if rc != 0 and "No such file" in err:
            # 服务器没有 remote-model.py → 直接改 config.toml + 重启
            return remote_set_model_fallback(model, ssh)
        print(out)
        if err:
            print(err, file=sys.stderr)
        return rc == 0
    finally:
        if own:
            ssh.close()


def remote_set_model_fallback(model, ssh):
    """没有 remote-model.py 时：直接 sed 改 model 行 + systemctl 重启。"""
    sed = f"sed -i 's/^model[[:space:]]*=.*/model = \"{model}\"/' ~/.codex/config.toml"
    restart = "systemctl restart codex-app-server"
    cmd = f"{sed} && {restart} && grep -m1 '^model' ~/.codex/config.toml"
    _, o, e = ssh.exec_command(cmd, timeout=120)
    out = o.read().decode("utf-8", "replace").strip()
    err = e.read().decode("utf-8", "replace").strip()
    rc = o.channel.recv_exit_status()
    print(f"[远程已切模型] {out}")
    if err:
        print(err, file=sys.stderr)
    return rc == 0


def run_remote(profile=None, exec_prompt=None, set_only=False):
    model = resolve_model(profile) if profile else None

    if model and not remote_set_model(model):
        print("[错误] 远程切模型失败，中止", file=sys.stderr)
        sys.exit(1)

    if set_only:
        print("[OK] 远程模型已切换，不建立连接")
        return

    token = remote_token()
    ws = f"ws://{REMOTE_HOST}:{REMOTE_WS_PORT}"

    if exec_prompt is not None:
        # 非交互：走 codex-remote-cli.py（codex exec 不支持 --remote）
        cli = os.path.join(SCRIPT_DIR, "codex-remote-cli.py")
        if not os.path.exists(cli):
            print("未找到 codex-remote-cli.py", file=sys.stderr)
            sys.exit(1)
        env = dict(os.environ)
        if token:
            env["CODEX_WS_TOKEN"] = token
        print(f"[远程 exec] {ws} (模型: {model or '服务器默认'})")
        subprocess.run([sys.executable, cli, exec_prompt], env=env)
    else:
        # 交互 TUI: codex --remote（token 走 env 注入，公网明文 ws 不传 flag）
        bin_path = resolve_codex_bin()
        cmd = [bin_path, "--remote", ws]
        env = dict(os.environ)
        if token:
            env["CODEX_WS_TOKEN"] = token
        print(f"[远程 Codex] {ws} (模型: {model or '服务器默认'}, 线程存服务器)")
        print("  [提示] 公网明文 ws:// 建议走 SSH 隧道:")
        print(f"         ssh -L {REMOTE_WS_PORT}:127.0.0.1:{REMOTE_WS_PORT} {REMOTE_USER}@{REMOTE_HOST}")
        print("         然后远程地址用 ws://127.0.0.1:%d" % REMOTE_WS_PORT)
        print("  执行:", " ".join(cmd))
        subprocess.run(cmd, env=env)
    sys.exit(0)


# --------------------------------------------------------------------------- #
# MCP 多模型团队
# --------------------------------------------------------------------------- #
def run_mcp():
    mcp = os.path.join(SCRIPT_DIR, "mcp_server.py")
    if not os.path.exists(mcp):
        print("未找到 mcp_server.py", file=sys.stderr)
        sys.exit(1)
    print("[MCP] 启动多模型调度 server（stdio，供 codex 会话调用）")
    print("  工具: multi_team_start / multi_orchestrate_start / multi_parallel_start /")
    print("        multi_ask / multi_task_status / multi_task_result / multi_ping / ...")
    subprocess.run([sys.executable, mcp])


# --------------------------------------------------------------------------- #
# 菜单 / 入口
# --------------------------------------------------------------------------- #
def list_profiles():
    print("可选档位（codex --profile <名>）:")
    for name, (model, effort, label) in PROFILES.items():
        print(f"  {name:6s} {model:20s} effort={effort:7s} {label}")
    print("别名: " + ", ".join(f"{k}->{v}" for k, v in ALIASES.items()))
    print(f"本地网关: {LOCAL_BASE_URL}")
    print(f"云网关:   {GATEWAY_BASE_URL}")


def menu():
    while True:
        print("\n" + "=" * 52)
        print("  codex 多模型切换启动器 (codex-multi)")
        print("=" * 52)
        print("  1. 本地 codex（选档位，交互 TUI）")
        print("  2. 远程 Linux app-server（选档位，交互 TUI）")
        print("  3. 列出档位")
        print("  4. 启动 MCP 多模型调度 server")
        print("  5. 退出")
        print("=" * 52)
        try:
            s = input("  请选择 [1-5]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if s == "5":
            print("再见")
            return
        try:
            if s == "1":
                run_local(pick_profile())
            elif s == "2":
                run_remote(pick_profile())
            elif s == "3":
                list_profiles()
            elif s == "4":
                run_mcp()
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def pick_profile():
    names = list(PROFILES)
    print("  模型档位:")
    for i, n in enumerate(names, 1):
        m, _, label = PROFILES[n]
        print(f"    {i}. {n:6s} {m:20s} {label}")
    s = input(f"  选择 [1-{len(names)}] 回车={DEFAULT_PROFILE}: ").strip()
    if not s.isdigit() or not (1 <= int(s) <= len(names)):
        return DEFAULT_PROFILE
    return names[int(s) - 1]


def main():
    ap = argparse.ArgumentParser(description="跨平台 codex 多模型切换启动器")
    ap.add_argument("profile", nargs="?", help="档位名/别名/模型假名（本地或远程）")
    ap.add_argument("--exec", dest="exec_prompt", default=None, help="非交互提示词")
    ap.add_argument("--remote", action="store_true", help="连远程 Linux app-server")
    ap.add_argument("--set-only", action="store_true", help="只切远程模型不连")
    ap.add_argument("--list", action="store_true", help="列档位")
    ap.add_argument("--mcp", action="store_true", help="启动 MCP 多模型调度 server")
    args = ap.parse_args()

    # 支持位置参数形式的 list / mcp / remote（如 `codex-multi.py list`）
    if args.profile == "list":
        args.list = True
        args.profile = None
    if args.profile == "mcp":
        args.mcp = True
        args.profile = None
    if args.profile == "remote":
        args.remote = True
        args.profile = None

    if args.list:
        list_profiles()
        return
    if args.mcp:
        run_mcp()
        return
    if args.remote:
        run_remote(args.profile, args.exec_prompt, args.set_only)
        return
    if args.profile:
        run_local(args.profile, args.exec_prompt)
        return
    menu()


if __name__ == "__main__":
    main()