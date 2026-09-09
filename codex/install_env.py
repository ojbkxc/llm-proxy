#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude / Codex 环境一键安装脚本（install_env.py）
=================================================

用途：在新电脑上自动准备部署 deploy_ai_cli.py 所需的全部环境：
  1. Node.js + npm（codex 的运行依赖）
  2. Python 3.10+（部署脚本本身的运行依赖）
  3. @openai/codex 最新版（npm 全局安装）
  4. Git（可选，git clone 项目用）

安装完成后自动衔接 deploy_ai_cli.py 完成配置部署（可用 --skip-deploy 跳过）。

特点：
  - 跨平台：Windows / macOS / Linux 自适应（包管理器自动选择）
  - 幂等：已安装的组件自动跳过，版本过旧时提示升级
  - Windows：优先 winget > choco > scoop > 官网安装包下载；Node 缺失时也可用 npm 自举
  - 仅标准库，无第三方依赖

用法：
  python install_env.py                 # 全自动：检查并安装缺失组件
  python install_env.py --dry-run       # 只检查环境，不安装任何东西
  python install_env.py --skip-codex    # 只装 Node/Python/Git，跳过 codex
  python install_env.py --verbose       # 输出详细日志

说明：
  codex 的安装命令是 `npm install -g @openai/codex`（npm 包名不含 cli），
  安装后可执行文件叫 `codex`。如果 npm 源慢，可先：
      npm config set registry https://registry.npmmirror.com

  Codex 桌面版（ChatGPT 风格 GUI）安装命令（手动版）：
      codex app 2>&1                      # 官方 CLI 子命令，缺失时自动打开安装器
      winget install --id 9PLM9XGG6VKS -s msstore   # 微软商店源安装 ChatGPT 桌面版（需 winget + 商店授权）
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

APP_NAME = "install_env"

# 组件最低版本要求
MIN_PYTHON = (3, 10)
MIN_NODE_MAJOR = 18          # codex 0.15x 要求 Node 18+
CODEX_NPM_PKG = "@openai/codex@latest"

# Windows 下载地址（winget/choco 都不可用时的兜底）
NODEJS_WIN_URL = "https://nodejs.org/dist/latest-v22.x/"
NODEJS_WIN_INSTALLER_HINT = "https://nodejs.org/zh-cn/download"
PYTHON_WIN_URL = "https://www.python.org/downloads/windows/"
GIT_WIN_URL = "https://git-scm.com/download/win"

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

log_target = print  # 简单直接：安装脚本用 print 即可


def log(msg: str) -> None:
    log_target(msg)


def log_ok(msg: str) -> None:
    log(f"[OK]   {msg}")


def log_warn(msg: str) -> None:
    log(f"[WARN] {msg}")


def log_err(msg: str) -> None:
    log(f"[ERR]  {msg}")


def log_step(msg: str) -> None:
    log("")
    log("=" * 60)
    log(msg)
    log("=" * 60)


def run(cmd, timeout: int = 600, check: bool = False,
        quiet: bool = False) -> Tuple[int, str]:
    """运行命令，返回 (returncode, 合并输出)。

    cmd 可为：
      - list[str]：直接执行（适合 node/git 等原生 exe）
      - str：shell=True 执行（适合 npm/codex 等 .CMD 脚本，
             Windows 上 subprocess 不走 shell 无法执行 .CMD）
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            shell=isinstance(cmd, str),
        )
        out = (r.stdout or "") + (r.stderr or "")
        if check and r.returncode != 0:
            return r.returncode, out
        if not quiet and out.strip():
            for line in out.strip().splitlines()[-5:]:
                log(f"        {line}")
        return r.returncode, out
    except FileNotFoundError:
        return 127, f"command not found: {cmd if isinstance(cmd, str) else cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout: {cmd}"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def parse_major(version_output: str) -> Optional[int]:
    m = re.search(r"v?(\d+)\.", version_output)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #

def check_python() -> Tuple[bool, str]:
    """当前解释器就是 Python。返回 (是否满足最低版本, 版本串)。"""
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    ok = (v.major, v.minor) >= MIN_PYTHON
    return ok, ver


def check_node() -> Tuple[bool, str, Optional[str]]:
    """返回 (是否可用, 版本串, npm 路径)。"""
    node = which("node")
    if not node:
        return False, "未安装", None
    rc, out = run(["node", "--version"], quiet=True)
    ver = out.strip().splitlines()[0] if out.strip() else "unknown"
    major = parse_major(ver)
    ok = rc == 0 and major is not None and major >= MIN_NODE_MAJOR
    npm = which("npm")
    return ok, ver, npm


def check_codex(npm: Optional[str]) -> Tuple[bool, str]:
    """检测 codex 是否可用。

    Windows 坑：npm 全局安装的可执行文件是 `codex.CMD`，shutil.which 能找到它，
    但 subprocess.run(["codex", ...]) 不走 shell 时无法执行 .CMD
    （WinError 2），会误报"未安装"。因此统一用 shell=True 执行。
    """
    if not which("codex"):
        return False, "未安装"
    rc, out = run("codex --version", quiet=True)
    ver = out.strip().splitlines()[0] if out.strip() else "unknown"
    return rc == 0, ver


def check_git() -> Tuple[bool, str]:
    if not which("git"):
        return False, "未安装"
    rc, out = run(["git", "--version"], quiet=True)
    return rc == 0, out.strip().splitlines()[0] if out.strip() else "unknown"


# --------------------------------------------------------------------------- #
# 安装：Node.js
# --------------------------------------------------------------------------- #

def install_node_windows() -> bool:
    """Windows 装 Node：winget > choco > scoop > 打开官网。"""
    if which("winget"):
        log("    尝试 winget 安装 OpenJS.NodeJS.LTS ...")
        rc, _ = run(["winget", "install", "--id", "OpenJS.NodeJS.LTS",
                     "--accept-source-agreements", "--accept-package-agreements",
                     "-e", "--silent"], timeout=900)
        if rc == 0:
            return True
        log_warn(f"    winget 安装失败（退出码 {rc}），尝试下一种方式")

    if which("choco"):
        log("    尝试 choco 安装 nodejs-lts ...")
        rc, _ = run(["choco", "install", "nodejs-lts", "-y"], timeout=900)
        if rc == 0:
            return True
        log_warn(f"    choco 安装失败（退出码 {rc}），尝试下一种方式")

    if which("scoop"):
        log("    尝试 scoop 安装 nodejs-lts ...")
        rc, _ = run(["scoop", "install", "nodejs-lts"], timeout=900)
        if rc == 0:
            return True
        log_warn(f"    scoop 安装失败（退出码 {rc}）")

    log_err("    自动安装 Node.js 失败。请手动安装：")
    log_err(f"      官网下载：{NODEJS_WIN_INSTALLER_HINT}")
    log_err("      （装 LTS 版本，一路下一步；装完重开终端再跑本脚本）")
    try:
        webbrowser.open(NODEJS_WIN_INSTALLER_HINT)
    except Exception:
        pass
    return False


def install_node_unix() -> bool:
    """macOS/Linux 装 Node：brew > apt > dnf > 提示 nvm。"""
    if IS_MACOS and which("brew"):
        log("    尝试 brew install node ...")
        rc, _ = run(["brew", "install", "node"], timeout=1200)
        if rc == 0:
            return True
        log_warn(f"    brew 安装失败（退出码 {rc}）")

    if IS_LINUX:
        if which("apt-get"):
            log("    尝试 apt 安装 nodejs npm（可能需要 sudo）...")
            rc, _ = run(["sudo", "apt-get", "update"], timeout=300)
            rc2, _ = run(["sudo", "apt-get", "install", "-y", "nodejs", "npm"],
                         timeout=900)
            if rc2 == 0:
                return True
            # apt 源里的 node 可能太老，提示 NodeSource
            log_warn("    apt 安装失败或版本过旧。推荐 NodeSource 安装 Node 22：")
            log_warn("      curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -")
            log_warn("      sudo apt-get install -y nodejs")
            return False
        if which("dnf"):
            log("    尝试 dnf 安装 nodejs npm ...")
            rc, _ = run(["sudo", "dnf", "install", "-y", "nodejs", "npm"], timeout=900)
            if rc == 0:
                return True
        log_warn("    未找到可用包管理器。推荐 nvm 安装：")
        log_warn('      curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash')
        log_warn("      重开终端后：nvm install --lts")
        return False

    log_err(f"    请从 https://nodejs.org/ 手动安装 Node {MIN_NODE_MAJOR}+")
    return False


def install_node() -> bool:
    log_step("安装 Node.js（codex 依赖）")
    ok, ver, _ = check_node()
    if ok:
        log_ok(f"Node.js 已安装且版本满足要求（{ver}），跳过")
        return True
    if not ok and ver != "未安装":
        log_warn(f"Node.js 版本过低（{ver}，要求 >= {MIN_NODE_MAJOR}），尝试升级")
    return install_node_windows() if IS_WINDOWS else install_node_unix()


# --------------------------------------------------------------------------- #
# 安装：Python（仅提示；当前解释器即 Python，多数情况无需处理）
# --------------------------------------------------------------------------- #

def ensure_python() -> bool:
    log_step("检查 Python")
    ok, ver = check_python()
    if ok:
        log_ok(f"Python {ver} 满足要求（>= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）")
        return True
    log_warn(f"当前 Python {ver} 低于 {MIN_PYTHON[0]}.{MIN_PYTHON[1]}，deploy_ai_cli 可能不兼容")
    if IS_WINDOWS:
        log_warn(f"    建议安装：{PYTHON_WIN_URL}（勾选 Add to PATH）")
    else:
        log_warn("    建议用系统包管理器或 pyenv 升级 Python 3.10+")
    return False  # 不阻塞：旧版本也可能跑得动


# --------------------------------------------------------------------------- #
# 安装：Codex
# --------------------------------------------------------------------------- #

def install_codex(npm: Optional[str]) -> bool:
    """安装/升级 codex（npm 全局包）。

    Windows 坑：npm 是 npm.CMD，subprocess 直接调用会 FileNotFoundError，
    必须走 shell 字符串形式执行。
    """
    log_step(f"安装 Codex（npm install -g {CODEX_NPM_PKG}）")
    if not npm:
        log_err("npm 不可用，无法安装 codex。请先确认 Node.js 安装成功并重开终端。")
        return False
    ok, ver = check_codex(npm)
    if ok:
        # 已装也顺带升级到最新（幂等，latest 已是最新则秒过）
        log_ok(f"codex 已安装（{ver}），检查更新 ...")
    npm_cmd = npm if " " not in npm else f'"{npm}"'
    rc, _ = run(f"{npm_cmd} install -g {CODEX_NPM_PKG}", timeout=900)
    if rc != 0:
        log_err(f"codex 安装失败（退出码 {rc}）")
        if IS_WINDOWS:
            log_err("    若报权限错误，请以管理员身份重跑本脚本。")
        return False
    ok2, ver2 = check_codex(npm)
    if ok2:
        log_ok(f"codex 就绪（{ver2}）")
        return True
    log_warn("codex 已安装但当前终端 PATH 未刷新，重开终端后即可使用。")
    return True


# --------------------------------------------------------------------------- #
# 安装：Git（可选）
# --------------------------------------------------------------------------- #

def install_git() -> bool:
    log_step("检查 Git（可选）")
    ok, ver = check_git()
    if ok:
        log_ok(f"Git 已安装（{ver}）")
        return True
    if IS_WINDOWS:
        if which("winget"):
            rc, _ = run(["winget", "install", "--id", "Git.Git", "-e", "--silent"],
                        timeout=900)
            if rc == 0:
                log_ok("Git 安装完成（重开终端生效）")
                return True
        log_warn(f"    建议安装：{GIT_WIN_URL}")
    elif IS_MACOS:
        log_warn("    首次 `git --version` 会触发 Xcode Command Line Tools 安装弹窗")
    else:
        log_warn("    sudo apt-get install -y git 或对应包管理器")
    return False  # 可选组件，不阻塞


# --------------------------------------------------------------------------- #
# 可选：Codex 桌面版（ChatGPT 风格界面）安装
# --------------------------------------------------------------------------- #

def maybe_install_codex_app() -> bool:
    """询问是否安装 Codex 桌面版（ChatGPT 风格 GUI）。

    按 1 或直接回车 → 执行 `codex app 2>&1` 安装/启动桌面版；
    按 0 → 跳过。
    """
    if not which("codex"):
        log_warn("未检测到 codex，无法安装桌面版")
        return False
    log_step("Codex 桌面版（ChatGPT 风格界面）")
    log("  执行 codex app 会下载并安装/启动官方桌面客户端。")
    try:
        s = input("  是否安装？[回车或 1 = 安装，0 = 跳过] > ").strip()
    except (EOFError, KeyboardInterrupt):
        s = ""  # 无输入 / Ctrl+C 一律按默认安装
    if s == "0":
        log_ok("已跳过 Codex 桌面版安装")
        return False
    log("  正在执行 codex app（首次会下载安装，可能较慢）...")
    rc, _ = run("codex app 2>&1", timeout=1200)
    if rc == 0:
        log_ok("Codex 桌面版已安装/启动")
        return True
    log_warn(f"codex app 返回码 {rc}（可能已取消或已装好）。")
    log_warn("  重开终端后手动验证：codex app 2>&1")
    return False


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def refresh_path_hint() -> None:
    log("")
    log("-" * 60)
    log("提示：本脚本刚安装的组件在「当前终端」可能不可见（PATH 未刷新）。")
    if IS_WINDOWS:
        log("     请关闭并重新打开终端（或 PowerShell）后再验证。")
    else:
        log("     请执行 `hash -r` 或重开终端后再验证。")


def main(argv: Optional[List[str]] = None) -> int:
    global log_target
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Claude / Codex 环境一键安装脚本（Node.js / Python / codex / Git）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查环境并打印计划，不安装任何东西")
    parser.add_argument("--skip-deploy", action="store_true",
                        help="环境装好后不自动运行 deploy_ai_cli.py")
    parser.add_argument("--skip-codex-app", action="store_true",
                        help="跳过 Codex 桌面版（ChatGPT）安装询问（默认询问）")
    parser.add_argument("--deploy-script", default=None,
                        help="deploy_ai_cli.py 的路径（默认与本脚本同目录）")
    parser.add_argument("--verbose", action="store_true", help="输出详细日志")
    args = parser.parse_args(argv)

    # Windows 控制台 GBK -> UTF-8（与 deploy_ai_cli.py 一致）
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    log_step(f"{APP_NAME} 环境安装器（{platform.system()}）")
    log(f"目标：Node {MIN_NODE_MAJOR}+ / Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ "
        f"/ codex({CODEX_NPM_PKG}) / Git(可选)")
    if args.dry_run:
        log("（--dry-run：仅检查，不安装）")

    results: dict = {}

    # 1. Python（当前解释器本身，只检查）
    results["python"] = ensure_python()

    # 2. Node.js
    if args.dry_run:
        ok, ver, _ = check_node()
        results["node"] = ok
        log(f"[dry-run] Node.js: {'OK ' + ver if ok else '需安装'}")
    else:
        results["node"] = install_node()

    # 3. Codex
    if args.dry_run:
        ok, ver = check_codex(None)
        results["codex"] = ok
        log(f"[dry-run] codex: {'OK ' + ver if ok else '需安装'}")
    else:
        _, _, npm = check_node()
        results["codex"] = install_codex(npm)

    # 4. Git（可选）
    if args.dry_run:
        ok, ver = check_git()
        results["git"] = ok
        log(f"[dry-run] git: {'OK ' + ver if ok else '未安装（可选）'}")
    else:
        results["git"] = install_git()

    # 汇总
    log_step("环境检查结果")
    for k, v in results.items():
        mark = "OK" if v else ("SKIP" if k == "git" else "FAIL")
        log(f"  {k:8s} {mark}")

    codex_ok = results.get("codex", False)
    if not args.dry_run and (results.get("node") or codex_ok):
        refresh_path_hint()

    # 5. 衔接配置部署
    if args.skip_deploy:
        log("\n（--skip-deploy：跳过 deploy_ai_cli.py 配置部署）")
    elif args.dry_run:
        log("\n（--dry-run：不执行配置部署）")
    elif codex_ok:
        deploy_py = Path(args.deploy_script) if args.deploy_script else (
            Path(__file__).resolve().parent / "deploy_ai_cli.py")
        if deploy_py.exists():
            log_step("执行配置部署：deploy_ai_cli.py")
            cmd = [sys.executable, str(deploy_py), "--non-interactive"]
            if IS_WINDOWS and os.name == "nt":
                # 管理员才带 --auto-fix；非管理员 deploy_ai_cli 会降级为提示
                try:
                    import ctypes
                    is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
                except Exception:
                    is_admin = False
                if is_admin:
                    cmd.append("--auto-fix")
            rc, _ = run(cmd, timeout=900)
            results["deploy"] = rc == 0
            log_ok("配置部署完成") if rc == 0 else log_err(f"配置部署失败（退出码 {rc}）")
        else:
            log_warn(f"未找到 {deploy_py}，跳过配置部署。请把两个脚本放同一目录。")

    failed_core = [k for k in ("python", "node", "codex") if not results.get(k)]
    if failed_core:
        log_err(f"\n以下核心组件未就绪：{', '.join(failed_core)}")
        log_err("请按上方提示手动处理，然后重跑本脚本。")
        return 1

    # 6. 询问是否安装 Codex 桌面版（ChatGPT 风格界面）
    #    默认回车/按 1 = 安装，按 0 = 跳过；--skip-codex-app 直接跳过询问。
    if not args.dry_run and not args.skip_codex_app and codex_ok:
        maybe_install_codex_app()

    log("\n环境就绪！重开终端后即可使用 codex / claude。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
