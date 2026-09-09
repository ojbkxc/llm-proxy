#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_yume.py —— 在新 Windows 电脑上一键部署 yume 全套环境：

  1. Node.js 22 LTS（官方 MSI，已装 >=22 则跳过；安装时弹一次 UAC）
  2. 官方 Claude Code CLI（npm -g @anthropic-ai/claude-code@latest）
  3. yume 桌面版（GitHub Releases，x64 / arm64 自动识别，per-user 安装免管理员）
  4. 用户级环境变量 + ~/.claude/settings.json：接入 cf-ai-gw 网关、
     模型映射（opus/sonnet=glm-5.3，haiku=glm-5.3-flash，deepseek 两个为备选）、
     全局跳过权限
  5. 端到端验证：CLI 版本 + 逐个模型 ping 网关

用法：
  python setup_yume.py                                          # 全新机器一键装齐
  python setup_yume.py --proxy http://127.0.0.1:7890            # 需要代理访问 GitHub/npm 时
  python setup_yume.py --registry https://registry.npmmirror.com  # npm 走镜像
  python setup_yume.py --skip-node --skip-yume                  # 只补 CLI 和配置
  python setup_yume.py --interactive                           # yume 安装器走图形界面
  python setup_yume.py --opus-model deepseek-v4-pro-0813        # 把 opus 槽换到 deepseek

注意（都是踩过的坑）：
  * 绝不设置 ANTHROPIC_MODEL —— 官方 CLI 本地校验模型名，设成 glm-5.3 会报
    unrecognized_model。模型映射必须用 ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL。
  * 装完必须重开终端 / 重启 yume，旧进程不会继承新环境变量。
  * glm-5.3-flash 思考贪婪：网关把 thinking 转成 reasoning_effort=high，max_tokens
    给太小会 thinking 吞光 text → 空响应。这不是模型坏了。
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if os.name != "nt":
    sys.exit("本脚本仅支持 Windows。Linux/macOS 请手动安装 node + npm 包后参照文末配置。")

import winreg  # noqa: E402

# ---------------------------------------------------------------- 常量 ----

GATEWAY = "https://cfapi.1232333.xyz"
API_KEY = "sk-wa-f9cb7d4ba48f403797fc3f55b928ceac"
YUME_VERSION = "v0.43.1"
YUME_REPO = "aofp/yume"
NODE_DIST = "https://nodejs.org/dist"
NPM_REGISTRY_DEFAULT = "https://registry.npmjs.org"

# ---------------------------------------------------------------- 模型 ----
# 网关 cf-ai-gw 已内置这些模型（DEFAULT_MODEL_MAP → @cf/...）。yume 只能通过
# opus / sonnet / haiku 三个槽位映射到具体模型，所以默认组合如下；deepseek 两个
# 是备选，可用 --opus-model / --sonnet-model / --haiku-model 换上去。
MODEL_MAIN = "glm-5.3"                    # 默认 opus/sonnet 槽位（1.25M，主力，支持图片）
MODEL_HAIKU = "glm-5.3-flash"            # 默认 haiku 槽位（1.25M，支持图片）
MODEL_DEEPSEEK_PRO = "deepseek-v4-pro-0813"    # 备选：复杂推理（1M，偶发空响应）
MODEL_DEEPSEEK_FLASH = "deepseek-v4-flash-0731"  # 备选：最快（1.25M，不支持图片）

# 四个模型都必须在网关可用，验证时可逐个 ping
GATEWAY_MODELS = (MODEL_MAIN, MODEL_HAIKU, MODEL_DEEPSEEK_PRO, MODEL_DEEPSEEK_FLASH)

# modelPicker：让官方 CLI 认这些非官方模型名。官方 CLI 会本地校验 --model 传进来的
# 名称，非官方名会报 unrecognized_model。settings.json 里的 modelPicker.options 会把这
# 些名字加进"可接受模型"清单，behavesAs 指定它按哪个官方模型处理（capability/effort
# 默认值），但请求时仍把原始模型名（glm-5.3 等）发给网关。
#   glm-5.3 / deepseek-v4-pro-0813  → 按 opus 档处理
#   glm-5.3-flash / deepseek-v4-flash-0731 → 按 haiku 档处理
MODEL_PICKER = {
    "options": [
        {"model": MODEL_MAIN, "behavesAs": "claude-opus-4-8"},
        {"model": MODEL_HAIKU, "behavesAs": "claude-haiku-4-5"},
        {"model": MODEL_DEEPSEEK_PRO, "behavesAs": "claude-opus-4-8"},
        {"model": MODEL_DEEPSEEK_FLASH, "behavesAs": "claude-haiku-4-5"},
    ]
}


def build_env_vars(opus=MODEL_MAIN, sonnet=MODEL_MAIN, haiku=MODEL_HAIKU):
    """生成要写入用户环境变量的完整映射。

    绝不设置 ANTHROPIC_MODEL（官方 CLI 本地校验模型名 → unrecognized_model），
    模型映射只走 ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL 三个槽位。
    """
    return {
        "ANTHROPIC_BASE_URL": GATEWAY,
        "ANTHROPIC_AUTH_TOKEN": API_KEY,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": opus,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": sonnet,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": haiku,
        "CLAUDE_DANGEROUS_MODE": "1",  # 所有会话强制跳过权限（bypassPermissions 的保险）
    }


def build_settings_env(opus=MODEL_MAIN, sonnet=MODEL_MAIN, haiku=MODEL_HAIKU):
    """写入 ~/.claude/settings.json 的 env（不含 CLAUDE_DANGEROUS_MODE）"""
    env = build_env_vars(opus, sonnet, haiku)
    env.pop("CLAUDE_DANGEROUS_MODE", None)
    return env

# 权限白名单：全部工具放行 + 所有 MCP 服务器放行
PERMISSIONS_ALLOW = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Agent", "Skill",
    "NotebookEdit", "mcp__*",
]

# --------------------------------------------------------------- 小工具 ----

def ok(msg):   print(f"  [OK] {msg}")
def warn(msg): print(f"  [!!] {msg}")
def err(msg):  print(f"  [XX] {msg}")
def step(n, title): print(f"\n=== 第 {n} 步：{title} ===")


def run(cmd, **kw):
    """shell 执行，返回 CompletedProcess"""
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def http_get(url, timeout=90, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "setup-yume/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1.5)
    raise last


def download(url, dest: Path, timeout=600):
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "setup-yume/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last_pct = 0, -1
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                if pct != last_pct:
                    print(f"\r  下载中 {pct}% ({done // 1048576}MB)", end="", flush=True)
                    last_pct = pct
    print()
    tmp.rename(dest)


def refresh_path():
    """MSI/setx 改了注册表后，把 HKLM+HKCU 的 Path 合并进当前进程（不用重开终端）"""
    paths = []
    for hive, sub in ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, r"Environment")):
        try:
            with winreg.OpenKey(hive, sub) as k:
                v, _ = winreg.QueryValueEx(k, "Path")
                paths.append(v)
        except OSError:
            pass
    old = os.environ.get("Path", "")
    os.environ["Path"] = ";".join(dict.fromkeys(p.strip() for p in paths + [old] if p.strip()))
    # npm 全局 bin（claude.cmd 在这里）
    npm_bin = Path(os.environ.get("APPDATA", "")) / "npm"
    if npm_bin.is_dir() and str(npm_bin) not in os.environ["Path"]:
        os.environ["Path"] = str(npm_bin) + ";" + os.environ["Path"]


def arch():
    m = platform.machine().upper()
    return "arm64" if m in ("ARM64", "AARCH64") else "x64"


# ------------------------------------------------------------ 第 1 步：Node ----

def node_major():
    r = run("node -v")
    m = re.search(r"v(\d+)", r.stdout or "")
    return int(m.group(1)) if m else 0


def ensure_node():
    if node_major() >= 22:
        ok(f"Node 已满足要求（{run('node -v').stdout.strip()}），跳过安装")
        return
    if node_major() > 0:
        warn(f"当前 Node 版本过低（需 >=22，实际 {run('node -v').stdout.strip()}），将安装 22 LTS 覆盖")

    a = arch()
    print("  正在获取 Node.js 22 LTS 最新版列表 ...")
    html = http_get(f"{NODE_DIST}/latest-v22.x/").decode()
    names = sorted(set(re.findall(rf"node-v22\.[0-9.]+-{a}\.msi", html)))
    if not names:
        sys.exit("  找不到 Node 22 MSI，请手动到 https://nodejs.org/ 下载安装")
    msi_name = names[-1]
    print(f"  目标：{msi_name}")

    dest = Path(os.environ.get("TEMP", ".")) / msi_name
    if not dest.exists():
        download(f"{NODE_DIST}/latest-v22.x/{msi_name}", dest)
    else:
        ok("MSI 已存在，跳过下载")

    # msiexec 需要管理员：用 PowerShell -Verb RunAs 单独弹 UAC，不影响脚本其余部分
    print("  安装中（会弹一次 UAC 确认框）...")
    ps = f"Start-Process msiexec -ArgumentList '/i \"{dest}\" /qn /norestart' -Verb RunAs -Wait"
    r = run(["powershell", "-NoProfile", "-Command", ps])
    refresh_path()
    if node_major() >= 22:
        ok(f"Node 安装成功：{run('node -v').stdout.strip()}")
    else:
        err(f"Node 安装似乎失败（{r.stderr.strip() or 'UAC 被取消？'}），请手动安装后重跑本脚本（--skip-node 可跳过）")


# ------------------------------------------------------- 第 2 步：官方 CLI ----

def cli_installed():
    """npm 全局已装 @anthropic-ai/claude-code 则返回版本号，否则 False"""
    refresh_path()
    r = run("npm ls -g @anthropic-ai/claude-code --depth=0")
    m = re.search(r"@anthropic-ai/claude-code@(\S+)", r.stdout or "")
    return m.group(1) if m else False


def install_cli(registry):
    old = cli_installed()
    if old:
        # npm 语义：@latest 已满足则不重装；除非 --force-cli
        r = run(f'npm install -g @anthropic-ai/claude-code@latest --registry={registry}')
        refresh_path()
        v = run("claude --version").stdout.strip()
        (ok if v else err)(f"Claude Code CLI：{v or '安装异常'}（原 {old}，npm 检查是否需升级）")
        return
    r = run(f'npm install -g @anthropic-ai/claude-code@latest --registry={registry}')
    refresh_path()
    v = run("claude --version").stdout.strip()
    if v:
        ok(f"Claude Code CLI：{v}")
    else:
        err(f"npm 安装失败：{r.stderr.strip()[-400:]}\n  可换镜像重试：--registry https://registry.npmmirror.com")


# ---------------------------------------------------------- 第 3 步：yume ----

def yume_installed():
    """检测 yume 是否已装。返回安装信息 dict 或 False。

    注意：Tauri NSIS 装到 Program Files 时写 HKLM 卸载表（本机实测如此），
    per-user 装到 %LOCALAPPDATA%\\Programs 时写 HKCU。两个都查。
    """
    # 1) exe 直接探测（两种安装位置）
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "yume" / "yume.exe"
    la = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "yume" / "yume.exe"
    for exe in (pf, la):
        if exe.exists():
            return {"path": exe, "hive": "ProgramFiles" if exe == pf else "LocalAppData"}
    # 2) 注册表卸载表（HKCU + HKLM 都查）
    for hive, hname in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for sub_path in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                         r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                base = winreg.OpenKey(hive, sub_path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(base, i); i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(base, sub) as k:
                        name, _ = winreg.QueryValueEx(k, "DisplayName")
                        if "yume" in (name or "").lower():
                            info = {"hive": hname}
                            for attr in ("DisplayVersion", "InstallLocation"):
                                try:
                                    info[attr] = winreg.QueryValueEx(k, attr)[0]
                                except OSError:
                                    pass
                            return info
                except OSError:
                    continue
    return False


def install_yume(version, interactive=False, force=False):
    installed = yume_installed()
    if installed and not force:
        ver = installed.get("DisplayVersion", "?")
        loc = installed.get("InstallLocation") or installed.get("path", "?")
        ok(f"yume 已安装（v{ver}，{loc}），跳过安装。如需重装/升级用 --force-yume")
        return
    ver = version.lstrip("v")
    url = f"https://github.com/{YUME_REPO}/releases/download/v{ver}/yume_{ver}_{arch()}-setup.exe"
    dest = Path(os.environ.get("TEMP", ".")) / f"yume_{ver}_{arch()}-setup.exe"
    print(f"  下载 {url}")
    download(url, dest)

    # 已有旧版本时 NSIS 会先卸载再装，/S 静默升级不弹 UAC（per-user 时）
    print("  静默安装中 ...")
    args = [str(dest)] if interactive else [str(dest), "/S"]
    r = subprocess.run(args, capture_output=True, text=True)
    now = yume_installed()
    if now:
        new_ver = now.get("DisplayVersion", "?")
        loc = now.get("InstallLocation") or now.get("path", "?")
        ok(f"yume 安装完成（v{new_ver}，{loc}）")
    elif interactive:
        warn("安装器已退出，请自行确认安装结果")
    else:
        warn(f"静默安装返回码 {r.returncode}，建议加 --interactive 用图形界面装一次")


# ------------------------------------------------- 第 4 步：环境变量 + 配置 ----

def set_env_vars(opus=MODEL_MAIN, sonnet=MODEL_MAIN, haiku=MODEL_HAIKU):
    # 先清掉会致命的 ANTHROPIC_MODEL（官方 CLI 本地校验模型名 → unrecognized_model）
    run('reg delete "HKCU\\Environment" /v ANTHROPIC_MODEL /f')
    for k, v in build_env_vars(opus, sonnet, haiku).items():
        # setx 输出是 GBK，别靠输出文本判断，returncode==0 即成功
        r = run(f'setx {k} "{v}"')
        if r.returncode == 0:
            # 从注册表读回确认
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                    got, _ = winreg.QueryValueEx(key, k)
                (ok if got == v else warn)(f"setx {k}={v}" + ("" if got == v else f"（读回不一致：{got}）"))
            except OSError:
                warn(f"setx {k} 返回 0 但注册表读不到")
        else:
            warn(f"setx {k} 失败（exit={r.returncode}）")


def write_settings(opus=MODEL_MAIN, sonnet=MODEL_MAIN, haiku=MODEL_HAIKU):
    p = Path.home() / ".claude" / "settings.json"
    p.parent.mkdir(exist_ok=True)
    old = {}
    if p.exists():
        backup = p.with_name(f"settings.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.write_bytes(p.read_bytes())
        ok(f"已备份原配置 → {backup}")
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            warn("原 settings.json 解析失败，将重建（备份已保存）")

    env = dict(old.get("env", {}))
    env.pop("ANTHROPIC_MODEL", None)  # 同上，绝不保留
    env.update(build_settings_env(opus, sonnet, haiku))

    perms = dict(old.get("permissions", {}))
    allow = list(dict.fromkeys(list(perms.get("allow", [])) + PERMISSIONS_ALLOW))

    new = dict(old)
    new.update({
        "model": opus,
        "language": "chinese",
        "defaultMode": "bypassPermissions",
        "autoApprove": True,
        "alwaysSkipPermissionPrompt": True,
        "env": env,
        "permissions": {"allow": allow, "deny": perms.get("deny", [])},
        # 让官方 CLI 认 glm/deepseek 这些非官方模型名（否则 unrecognized_model）
        "modelPicker": MODEL_PICKER,
    })
    p.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok(f"已写入 {p}")
    ok(f"模型映射：opus={opus}，sonnet={sonnet}，haiku={haiku}，网关={GATEWAY}")
    ok(f"modelPicker 已注册 4 个模型：{', '.join(m['model'] for m in MODEL_PICKER['options'])}")


# ------------------------------------------------------------ 第 5 步：验证 ----

def test_gateway():
    """逐模型 ping 网关，确认配置里用到的每个模型都真实可用。"""
    headers = {"content-type": "application/json",
               "authorization": f"Bearer {API_KEY}",
               "x-api-key": API_KEY,
               "anthropic-version": "2023-06-01",
               # 裸 Python urllib 的指纹会被 Cloudflare 1010 拦掉，用浏览器 UA 绕过
               "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/133.0.0.0 Safari/537.36"}
    for m in GATEWAY_MODELS:
        body = json.dumps({
            "model": m,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": "pong"}],
        }).encode()
        req = urllib.request.Request(GATEWAY + "/v1/messages", data=body,
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            text = "".join(b.get("text", "") for b in data.get("content", []))
            if text.strip():
                ok(f"{m} 可用，回复：{text.strip()[:24]}")
            else:
                warn(f"{m} 有响应但无文本（thinking 吞光 max_tokens，非故障）")
        except Exception as e:
            warn(f"{m} 测试失败：{e}")


def verify():
    refresh_path()
    for cmd, name in (("node -v", "Node"), ("claude --version", "Claude Code CLI")):
        v = run(cmd).stdout.strip()
        (ok if v else warn)(f"{name}：{v or '未检测到'}")


# ------------------------------------------------------------------ main ----

def main():
    ap = argparse.ArgumentParser(description="一键部署 yume + 官方 Claude Code + cf-ai-gw 配置")
    ap.add_argument("--proxy", help="代理地址，如 http://127.0.0.1:7890")
    ap.add_argument("--registry", default=NPM_REGISTRY_DEFAULT, help="npm registry")
    ap.add_argument("--yume-version", default=YUME_VERSION, help=f"yume 版本（默认 {YUME_VERSION}）")
    ap.add_argument("--opus-model", default=MODEL_MAIN, help=f"opus 槽位模型（默认 {MODEL_MAIN}；备选 {MODEL_DEEPSEEK_PRO} / {MODEL_DEEPSEEK_FLASH}）")
    ap.add_argument("--sonnet-model", default=MODEL_MAIN, help=f"sonnet 槽位模型（默认 {MODEL_MAIN}；备选 {MODEL_DEEPSEEK_PRO} / {MODEL_DEEPSEEK_FLASH}）")
    ap.add_argument("--haiku-model", default=MODEL_HAIKU, help=f"haiku 槽位模型（默认 {MODEL_HAIKU}；备选 {MODEL_DEEPSEEK_PRO} / {MODEL_DEEPSEEK_FLASH}）")
    ap.add_argument("--skip-node", action="store_true")
    ap.add_argument("--skip-cli", action="store_true")
    ap.add_argument("--skip-yume", action="store_true")
    ap.add_argument("--force-yume", action="store_true", help="已装 yume 也重装")
    ap.add_argument("--interactive", action="store_true", help="yume 安装器用图形界面")
    ap.add_argument("--skip-gateway-test", action="store_true")
    args = ap.parse_args()

    if args.proxy:
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = args.proxy

    opus, sonnet, haiku = args.opus_model, args.sonnet_model, args.haiku_model

    print("=" * 60)
    print(" yume 一键部署（cf-ai-gw 网关）")
    print("=" * 60)

    if not args.skip_node:
        step(1, "Node.js >= 22 检查 / 安装")
        ensure_node()
    if not args.skip_cli:
        step(2, "安装官方 Claude Code CLI")
        install_cli(args.registry)
    if not args.skip_yume:
        step(3, "安装 yume 桌面版")
        install_yume(args.yume_version, args.interactive, args.force_yume)
    step(4, "环境变量 + ~/.claude/settings.json")
    set_env_vars(opus, sonnet, haiku)
    write_settings(opus, sonnet, haiku)
    step(5, "验证")
    verify()
    if not args.skip_gateway_test:
        test_gateway()

    print("\n" + "=" * 60)
    print(" 全部完成。请务必：")
    print("  1. 重开终端（新环境变量才生效）")
    print("  2. 启动/重启 yume（旧进程不继承新环境变量，否则模型显示会不对）")
    print(f"  3. yume 内模型选 {opus}；后台 Agent 自动按 opus/sonnet/haiku 三槽映射")
    print(f"     备选模型：{MODEL_DEEPSEEK_PRO}、{MODEL_DEEPSEEK_FLASH}（用 --opus-model/--sonnet-model/--haiku-model 换上）")
    print("=" * 60)


if __name__ == "__main__":
    main()
