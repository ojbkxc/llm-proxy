#!/usr/bin/env python3
"""
win-code.py — Windows 本机 Codex 启动器（本地桌面 / 远程服务器二选一）

解决一个核心诉求：本地桌面/CLI 连服务器上那个 codex app-server 时，
用的是"服务器的会话与模型"，与本机正在跑的 Codex 完全隔离，互不影响。

用法:
    python win-code.py local               # 启动本地 Codex(交互 TUI, 用本地配置)
    python win-code.py local --profile 档位  # 按模型档位启动(见下方 PROFILES)
    python win-code.py remote              # 连远程服务器 app-server(交互 TUI, 服务器会话)
    python win-code.py remote --exec "提示词"  # 连远程跑一条非交互(走 JSON-RPC)
    python win-code.py list                # 列出远程线程
    python win-code.py health              # 探活远程 app-server
    python win-code.py config              # 打印当前连接配置
    python win-code.py                     # 无参数 = 数字交互菜单

模型档位(--profile, 对应 ~/.codex/<名>.config.toml):
    deep  = glm-5.3(主力)  code = kimi-k2.7-code  dspro = glm-5.3
    dfast = deepseek-v4-flash-0731  fast = glm-5.3-flash
    mid = glm-5.2  kimi = kimi-k2.6  fast47 = glm-4.7-flash

GPT 假名(网关 cf-ai-gw 提供, 任何 -m / 模型名处都可直接用):
    gpt-6-astra   → glm-5.3                (指挥官位, 1.25M)
    gpt-5.6-sol   → deepseek-v4-pro-0813   (深度分析位, 1M)
    gpt-5.6-luna  → kimi-k2.7-code         (执行杂活位, 262K)
    gpt-5.6-sol-fast  → deepseek-v4-flash-0731
    gpt-5.6-luna-fast → glm-5.3-flash
    假名管理: dashboard cfapi.1232333.xyz → Settings → GPT 假名(独立表, 与模型映射互斥)

环境变量(可选覆盖):
    CODEX_BIN    本地 codex.exe 路径
    CODEX_REMOTE_WS    远程 ws 地址(默认从 codex-remote-config.json 读)
    CODEX_WS_TOKEN     远程 capability token(默认同配置文件)

与本地任务互不影响的原因:
  本地 codex 用 本机 ~/.codex/config.toml(线程存本机);
  远程连过去用 服务器 ~/.codex/config.toml(线程存服务器)。
  两个独立实例, 两套 state, 各自干活。

服务器侧配套: /opt/codex/codex-tm.py (多模型 tmux 场景管理, start-all 一键并行)。
注意: CLI 0.153.4 无 spawn_agent 子代理工具, three-tier 等编排 skill 在 CLI 环境不可用。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "codex-remote-config.json")

DEFAULT_CODEX_BIN = r"C:\Users\Administrator\AppData\Local\OpenAI\Codex\bin\27d6a192e9c98618\codex.exe"

# Windows 上 codex 全局安装后是 codex.cmd / codex.ps1（npm shim），
# subprocess.run(["codex", ...]) 不走 shell 时 CreateProcess 找不到 .CMD，
# 报 WinError 2「系统找不到指定的文件」。必须显式解析出完整路径。


def resolve_codex_bin():
    """解析出可在 subprocess 中直接执行的 codex 完整路径。"""
    env = os.environ.get("CODEX_BIN")
    if env and os.path.exists(env):
        return env
    if os.path.exists(DEFAULT_CODEX_BIN):
        return DEFAULT_CODEX_BIN
    # shutil.which 能找到 codex.cmd（npm shim），返回完整路径供 subprocess 用
    found = shutil.which("codex")
    if found:
        return found
    return "codex"  # 兜底走 PATH（Linux/macOS 可直接执行）

# 模型档位（对应 ~/.codex/<名>.config.toml profile 文件）
PROFILES = {
    "deep":   {"model": "glm-5.3",               "label": "glm-5.3 (深度推理主力)"},
    "dspro":  {"model": "glm-5.3",               "label": "glm-5.3 (同 deep 档)"},
    "code":   {"model": "kimi-k2.7-code",        "label": "kimi-k2.7-code (写码)"},
    "dfast":  {"model": "deepseek-v4-flash-0731", "label": "deepseek-v4-flash-0731 (快速)"},
    "fast":   {"model": "glm-5.3-flash",         "label": "glm-5.3-flash (快速)"},
    "mid":    {"model": "glm-5.2",               "label": "glm-5.2 (均衡)"},
    "kimi":   {"model": "kimi-k2.6",             "label": "kimi-k2.6 (写码)"},
    "fast47": {"model": "glm-4.7-flash",         "label": "glm-4.7-flash (最快)"},
    # GPT 假名档(网关转发到真实模型, 效果等同)
    "astra":  {"model": "gpt-6-astra",           "label": "gpt-6-astra → glm-5.3 (指挥官位)"},
    "sol":    {"model": "gpt-5.6-sol",           "label": "gpt-5.6-sol → deepseek-v4-pro (分析位)"},
    "luna":   {"model": "gpt-5.6-luna",          "label": "gpt-5.6-luna → kimi-k2.7-code (杂活位)"},
}
DEFAULT_PROFILE = "deep"


def load_cfg():
    cfg = {"host": "104.223.65.202", "port": 20130, "token": ""}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            cfg.update({k: v for k, v in data.items() if k in cfg and v})
        except Exception as e:
            print(f"警告: 读取配置失败: {e}", file=sys.stderr)
    cfg["host"] = os.environ.get("CODEX_REMOTE_HOST", cfg["host"])
    cfg["port"] = int(os.environ.get("CODEX_REMOTE_PORT", cfg["port"]))
    cfg["token"] = os.environ.get("CODEX_WS_TOKEN", cfg["token"])
    return cfg


def codex_bin():
    return resolve_codex_bin()


def ensure_token(cfg):
    if cfg["token"]:
        return cfg["token"]
    # 兜底: 本机 ws-cap-token.txt
    for p in [r"C:\Users\Administrator\.codex\ws-cap-token.txt"]:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
    print("错误: 未找到远程 token(--token / CODEX_WS_TOKEN / codex-remote-config.json)", file=sys.stderr)
    sys.exit(2)


def run_local(profile=None):
    cmd = [codex_bin()]
    label = "本机 config.toml 默认模型(deepseek-v4-pro-0813)"
    if profile:
        cmd += ["--profile", profile]
        label = PROFILES.get(profile, {}).get("label", profile)
    print(f"[本地 Codex] 模型: {label}")
    print("  执行:", " ".join(cmd))
    subprocess.run(cmd)
    sys.exit(0)


def run_remote(exec_prompt=None):
    cfg = load_cfg()
    token = ensure_token(cfg)
    ws = f"ws://{cfg['host']}:{cfg['port']}"

    # 非交互 exec: `codex exec` 不支持 --remote，改走 JSON-RPC 客户端
    if exec_prompt:
        cli = os.path.join(SCRIPT_DIR, "codex-remote-cli.py")
        if os.path.exists(cli):
            env = dict(os.environ)
            env["CODEX_WS_TOKEN"] = token
            print(f"[远程 exec] {ws} (服务器线程)")
            subprocess.run([sys.executable, cli, exec_prompt], env=env)
        else:
            print("未找到 codex-remote-cli.py", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    # 交互 TUI: --remote 连服务器
    # codex 0.153 安全策略：--remote-auth-token-env 只允许 wss:// 或 loopback ws://，
    # 明文 ws://公网IP 会被拒绝（ERROR: requires a `wss://` or loopback `ws://` remote）。
    # 公网明文 ws 场景改用 -c remote_auth_token_env=... 配置注入，校验只针对 CLI flag。
    is_loopback = cfg["host"] in ("127.0.0.1", "localhost", "::1")
    cmd = [codex_bin(), "--remote", ws]
    if is_loopback:
        cmd += ["--remote-auth-token-env", "CODEX_WS_TOKEN"]
    env = dict(os.environ)
    env["CODEX_WS_TOKEN"] = token
    print(f"[远程 Codex] {ws} (服务器会话, 模型 kimi-k2.7-code, 线程存服务器)")
    if not is_loopback:
        print("  [提示] 公网明文 ws:// 不允许 CLI 传 token，改用 SSH 隧道更安全:")
        print("         ssh -L 20130:127.0.0.1:20130 user@104.223.65.202")
        print("         然后远程地址用 ws://127.0.0.1:20130")
    print("  执行:", " ".join(cmd))
    subprocess.run(cmd, env=env)
    sys.exit(0)


def list_remote():
    cfg = load_cfg()
    token = ensure_token(cfg)
    # 复用 codex-remote-cli.py 的 --list
    cli = os.path.join(SCRIPT_DIR, "codex-remote-cli.py")
    if os.path.exists(cli):
        env = dict(os.environ)
        env["CODEX_WS_TOKEN"] = token
        subprocess.run([sys.executable, cli, "--list"], env=env)
    else:
        print("未找到 codex-remote-cli.py", file=sys.stderr)
        sys.exit(1)


def health():
    cfg = load_cfg()
    import urllib.request
    url = f"http://{cfg['host']}:{cfg['port']}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            print(f"远程 app-server healthz: HTTP {r.status} (OK)")
    except Exception as e:
        print(f"远程 app-server 探活失败: {e}", file=sys.stderr)
        sys.exit(1)


def show_config():
    cfg = load_cfg()
    print(json.dumps({
        "本地 codex.exe": DEFAULT_CODEX_BIN,
        "远程 ws": f"ws://{cfg['host']}:{cfg['port']}",
        "远程 token": (cfg["token"] or "")[:8] + "..." if cfg["token"] else "(未设置)",
        "本地默认模型": "deepseek-v4-pro-0813(本机 config.toml, 可 -p 换档)",
        "服务器默认模型": "kimi-k2.7-code(服务器 config.toml)",
        "GPT 假名": "gpt-6-astra→glm-5.3 / gpt-5.6-sol→deepseek-pro / gpt-5.6-luna→kimi-k2.7-code(+2 fast)",
        "服务器多场景": "/opt/codex/codex-tm.py (glm/deep/kimi/dfast/gfast/astra/sol/luna)",
        "网关": "cfapi.1232333.xyz (dashboard → Settings 可管模型映射与 GPT 假名)",
    }, ensure_ascii=False, indent=2))


def pick_profile():
    """让用户选一个模型档位，返回 profile 名。"""
    names = list(PROFILES)
    print("  模型档位:")
    for i, n in enumerate(names, 1):
        print(f"    {i}. {PROFILES[n]['label']}")
    s = input(f"  选择 [1-{len(names)}] 回车={DEFAULT_PROFILE}: ").strip()
    if not s.isdigit() or not (1 <= int(s) <= len(names)):
        return DEFAULT_PROFILE
    return names[int(s) - 1]


def menu():
    """无参数启动时：数字交互菜单。"""
    choices = [
        ("local",  "启动本地 Codex (选模型档位)"),
        ("remote", "连接远程服务器        (服务器 kimi-k2.7-code, 交互 TUI)"),
        ("list",   "列出远程线程"),
        ("health", "探活远程服务器"),
        ("config", "查看配置"),
        ("quit",   "退出"),
    ]
    while True:
        print("\n" + "=" * 44)
        print("  Codex 启动器 (win-code)")
        print("=" * 44)
        for i, (_, label) in enumerate(choices, 1):
            print(f"  {i}. {label}")
        print("=" * 44)
        # 支持环境变量自动选择（测试/CI 用），正常交互则 input()
        auto = os.environ.get("WINCODE_CHOICE", "")
        try:
            s = auto.strip() if auto else input("  请选择 [1-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if not s.isdigit() or not (1 <= int(s) <= len(choices)):
            print("  无效输入，请重新输入")
            continue
        key = choices[int(s) - 1][0]
        if key == "quit":
            print("再见")
            return
        try:
            if key == "local":
                run_local(pick_profile())
            elif key == "remote":
                run_remote(None)
            elif key == "list":
                list_remote()
            elif key == "health":
                health()
            elif key == "config":
                show_config()
        except KeyboardInterrupt:
            print("\n已取消，返回菜单")
        except Exception as e:
            print(f"\n出错: {e}")


def main():
    ap = argparse.ArgumentParser(description="Windows 本机 Codex 启动器 (本地/远程)")
    ap.add_argument("action", nargs="?", choices=["local", "remote", "list", "health", "config"], help="local=本地桌面 | remote=连远程服务器 | list=远程线程 | health=探活 | config=配置")
    ap.add_argument("--profile", "-p", default=None, help="local 时指定模型档位: " + "/".join(PROFILES))
    ap.add_argument("--exec", default=None, help="remote 时附加非交互提示词")
    args = ap.parse_args()

    if not args.action:
        menu()
        return

    if args.action == "local":
        run_local(args.profile)
    elif args.action == "remote":
        run_remote(args.exec)
    elif args.action == "list":
        list_remote()
    elif args.action == "health":
        health()
    elif args.action == "config":
        show_config()


if __name__ == "__main__":
    main()