#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
通用 Claude / Codex 本地配置部署脚本
=====================================

用途：在任意目标机器上自动探测 Claude（含 Claude Code CLI / Claude Code Haha 桌面端）
与 Codex 的安装位置，并完成统一的接入配置（自定义网关、模型分档、子代理分工、
环境变量、密钥写入等）。分发到其他计算机可独立运行，支持参数化覆盖自动检测结果。

安装codex桌面版命令：   codex app 2>&1
配置文件：               C:\Users\Administrator\.codex\config.toml

特性：
  - 自动检测：PATH 查找 + 系统注册表(App Paths) + 常见默认路径 + npm 全局查询
  - 跨平台：Windows / macOS / Linux 路径、Shell、环境变量设置方式自适应
  - 回退：检测失败 -> 交互式选择 / 手动输入 / 受限目录模糊搜索
  - 安全：写配置前自动备份（记录清单），--rollback 回滚（恢复覆盖 + 删除新建），
          --dry-run 仅探测
  - 幂等：重复运行不破坏已有配置，settings.json 合并式写入保留用户字段

依赖：仅 Python 标准库（建议 Python >= 3.10）。npm 仅用于可选的全局路径探测。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 常量与默认配置
# --------------------------------------------------------------------------- #

APP_NAME = "deploy_ai_cli"
MANIFEST_NAME = ".deploy_backup_manifest.json"

# 网关地址：cfapi 云网关 / 本地 ws-proxy（proxy.py，端口 8787）都认识下面的
# gpt-* 假名。换网关直接改这一行（本地 proxy 就改成 http://127.0.0.1:8787/v1）。
DEFAULT_BASE_URL = "https://cfapi.1232333.xyz/v1"

# 网关 API Key 占位符：留空表示未启用。把真实密钥填到这里即可免参数/免交互部署。
# 安全提示：密钥会随源码明文传播，请勿将填好密钥的副本提交到公开仓库或随意转发。
DEFAULT_API_KEY = "sk-wa-f9cb7d4ba48f403797fc3f55b928ceac"


# --------------------------------------------------------------------------- #
# .env 覆盖：同目录存在 .env 时，其 BASE_URL / API_KEY 覆盖上面硬编码默认值。
# 优先级：命令行 --base-url/--api-key（最高）> 进程环境变量 > .env > 本文件硬编码。
# --------------------------------------------------------------------------- #
def _load_dotenv(path):
    d = {}
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
                    d[k] = v
    except OSError:
        pass
    return d


_DOTENV = _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
if os.environ.get("BASE_URL"):
    DEFAULT_BASE_URL = os.environ["BASE_URL"]
elif _DOTENV.get("BASE_URL"):
    DEFAULT_BASE_URL = _DOTENV["BASE_URL"]
if os.environ.get("API_KEY") or os.environ.get("CF_GATEWAY_KEY"):
    DEFAULT_API_KEY = os.environ.get("API_KEY") or os.environ.get("CF_GATEWAY_KEY")
elif _DOTENV.get("API_KEY"):
    DEFAULT_API_KEY = _DOTENV["API_KEY"]

# Codex 模型分档（gpt-* 假名，两网关通用）：
# { 档位名: (模型名, reasoning effort) }
DEFAULT_CODEX_PROFILES: Dict[str, Tuple[str, str]] = {
    "fast":   ("gpt-5.6-luna-fast", "low"),
    "sfast":  ("gpt-5.6-sol-fast", "low"),
    "mid":    ("gpt-5.6-sol", "medium"),
    "qwen":   ("qwen3.8-max", "medium"),
    "qwenp":  ("qwen3.7-plus", "low"),
    "hw":     ("hw-glm-5", "medium"),
    "code":   ("gpt-5.6-luna", "high"),
    "deep":   ("gpt-6-astra", "high"),
}
DEFAULT_CODEX_PRIMARY = "gpt-5.6-luna"

# Claude 子代理分工：{ 子代理名: (模型名, 职责描述) }
CLAUDE_AGENT_SPECS: Dict[str, Tuple[str, str]] = {
    "code-reviewer": ("gpt-5.6-sol",
                      "深度代码审查员。审查代码改动、排查 bug、检查安全风险与性能问题时主动使用。"),
    "fast-writer":   ("gpt-5.6-luna-fast",
                      "快速文档与修补助手。写注释、README、commit 说明、简单文案或小范围机械修改时主动使用。"),
    "architect":     ("gpt-6-astra",
                      "方案架构师。需要横向对比技术方案、评估架构取舍、制定实现计划时主动使用。"),
}

# Claude 主/辅模型环境变量映射（opus/sonnet 用假名主力，haiku 用 flash）
CLAUDE_MODEL_ENV: Dict[str, str] = {
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5.6-luna-fast",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gpt-5.6-luna",
    "ANTHROPIC_DEFAULT_OPUS_MODEL":   "gpt-6-astra",
}

# 官方 CLI 会本地校验模型名，非官方名（gpt-* 假名/glm/deepseek）须注册进
# modelPicker 才能通过。behavesAs 指定按哪个官方模型处理（capability/effort
# 默认值），请求仍发原始模型名给网关。
MODEL_PICKER: Dict = {
    "options": [
        {"model": "gpt-6-astra",          "behavesAs": "claude-opus-4-8"},
        {"model": "gpt-5.6-luna",         "behavesAs": "claude-opus-4-8"},
        {"model": "gpt-5.6-luna-fast",    "behavesAs": "claude-haiku-4-5"},
        {"model": "gpt-5.6-sol",          "behavesAs": "claude-sonnet-4-6"},
        {"model": "gpt-5.6-sol-fast",     "behavesAs": "claude-haiku-4-5"},
        {"model": "glm-5.3",              "behavesAs": "claude-opus-4-8"},
        {"model": "glm-5.3-flash",        "behavesAs": "claude-haiku-4-5"},
        {"model": "glm-5.2",              "behavesAs": "claude-sonnet-4-6"},
        {"model": "deepseek-v4-pro-0813", "behavesAs": "claude-opus-4-8"},
        {"model": "deepseek-v4-flash-0731", "behavesAs": "claude-haiku-4-5"},
        {"model": "kimi-k2.7-code",       "behavesAs": "claude-sonnet-4-6"},
        {"model": "kimi-k2.6",            "behavesAs": "claude-sonnet-4-6"},
        {"model": "glm-4.7-flash",        "behavesAs": "claude-haiku-4-5"},
    ]
}

# 权限全放行 + 跳过权限弹窗（用户要求所有工具最高权限、永不确认）
PERMISSIONS_ALLOW: List[str] = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebFetch", "WebSearch",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "Agent", "Skill",
    "NotebookEdit", "mcp__*",
]

# --------------------------------------------------------------------------- #
# 平台判定
# --------------------------------------------------------------------------- #

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _setup_stdout_utf8() -> None:
    """Windows 控制台默认 GBK，强制 stdout/stderr 使用 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _now_ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("已写入 %s", path)


# --------------------------------------------------------------------------- #
# 备份与回滚清单
# --------------------------------------------------------------------------- #

def _manifest_path() -> Path:
    return Path(__file__).resolve().parent / MANIFEST_NAME


def _load_manifest() -> Dict:
    mp = _manifest_path()
    if mp.exists():
        try:
            data = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"batches": []}


def _save_manifest(manifest: Dict) -> None:
    try:
        _write_text(_manifest_path(), json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:
        log.debug("写回滚清单失败（不影响主流程）: %s", e)


def record_backup(original: str, backup: str) -> None:
    """把一次备份记录进回滚清单（失败不影响主流程）。"""
    try:
        manifest = _load_manifest()
        batches = manifest.setdefault("batches", [])
        if not batches:
            batches.append({"ts": _now_ts(), "backed_up": {}, "created": []})
        batches[-1].setdefault("backed_up", {})[original] = backup
        _save_manifest(manifest)
    except Exception as e:
        log.debug("记录备份清单失败（不影响主流程）: %s", e)


def record_created(path: str) -> None:
    """记录新建文件到回滚清单，供 --rollback 删除。"""
    try:
        manifest = _load_manifest()
        batches = manifest.setdefault("batches", [])
        if not batches:
            batches.append({"ts": _now_ts(), "backed_up": {}, "created": []})
        created = batches[-1].setdefault("created", [])
        if path not in created:
            created.append(path)
        _save_manifest(manifest)
    except Exception as e:
        log.debug("记录新建清单失败（不影响主流程）: %s", e)


def _backup(path: Path) -> Optional[Path]:
    """备份文件，返回备份路径；不存在则返回 None。"""
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak.{_now_ts()}")
        shutil.copy2(path, bak)
        log.info("已备份 %s -> %s", path, bak)
        record_backup(str(path), str(bak))
        return bak
    return None


def _deploy_write(path: Path, content: str) -> None:
    """部署写文件统一入口：存在则备份，新建则记录，便于完整回滚。"""
    existed = path.exists()
    _backup(path)
    _write_text(path, content)
    if not existed:
        record_created(str(path))


def rollback() -> bool:
    manifest = _load_manifest()
    batches = manifest.get("batches", [])
    if not batches:
        log.info("没有可回滚的备份记录")
        return False
    latest = batches[-1]
    backed = latest.get("backed_up", {})
    log.info("回滚最近一批备份（恢复 %s 项 / 清理 %s 项）...",
             len(backed), len(latest.get("created", [])))
    for orig, bak in backed.items():
        bak_p = Path(bak)
        orig_p = Path(orig)
        if bak_p.exists():
            shutil.copy2(bak_p, orig_p)
            log.info("已恢复 %s", orig_p)
        elif orig_p.exists():
            log.warning("备份缺失，保留现有文件 %s", orig_p)
    for c in latest.get("created", []):
        cp = Path(c)
        if cp.exists():
            cp.unlink()
            log.info("已删除新建文件 %s", c)
    batches.pop()
    _save_manifest(manifest)
    return True


# --------------------------------------------------------------------------- #
# 检测：通用
# --------------------------------------------------------------------------- #

def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _win_app_paths(name: str) -> Optional[str]:
    """Windows 注册表 App Paths 查询。"""
    if not IS_WINDOWS:
        return None
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\App Paths\{name}.exe")
            val, _ = winreg.QueryValueEx(key, None)
            if val:
                return val
        except OSError:
            continue
    return None


def _npm_global_root() -> Optional[str]:
    """通过 npm 查询全局安装根目录（可选探测，npm 缺失时返回 None）。"""
    try:
        r = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, text=True, timeout=25,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _filter_existing(cands: List[Tuple[Path, str]]) -> List[Tuple[Path, str]]:
    """去重并仅保留实际存在的路径。"""
    seen, out = set(), []
    for p, src in cands:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.exists():
            out.append((p, src))
    return out


def _fuzzy_find(name: str, roots: List[Path], max_depth: int = 4) -> List[Tuple[Path, str]]:
    """受限目录模糊搜索（回退用），按目录深度截断，避免全盘扫描。"""
    hits: List[Tuple[Path, str]] = []
    needle = name.lower()
    for root in roots:
        if not root.exists():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                depth = dirpath.count(os.sep) - str(root).count(os.sep)
                if depth > max_depth:
                    dirnames[:] = []
                    continue
                for fn in filenames:
                    if needle in fn.lower():
                        hits.append((Path(dirpath) / fn, "模糊搜索"))
        except Exception:
            continue
    return hits


# --------------------------------------------------------------------------- #
# 检测：Codex
# --------------------------------------------------------------------------- #

def _codex_candidates() -> List[Tuple[Path, str]]:
    home = Path.home()
    cands: List[Tuple[Path, str]] = []
    w = _which("codex")
    if w:
        cands.append((Path(w), "PATH"))
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        localapp = os.environ.get("LOCALAPPDATA", "")
        prog = os.environ.get("ProgramFiles", "")
        progdata = os.environ.get("ProgramData", "")
        cands += [
            (Path(appdata) / "npm" / "codex.cmd", "npm 用户级全局"),
            (Path(progdata) / "npm" / "codex.cmd", "npm 机器级全局"),
            (Path(localapp) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe", "独立安装程序"),
            (Path(prog) / "OpenAI" / "Codex" / "bin" / "codex.exe", "ProgramFiles 独立安装"),
        ]
        reg = _win_app_paths("codex")
        if reg:
            cands.append((Path(reg), "注册表 App Paths"))
        nr = _npm_global_root()
        if nr:
            cands.append((Path(nr) / "@openai" / "codex" / "bin" / "codex.js", "npm root"))
    else:
        cands += [
            (home / ".npm-global" / "bin" / "codex", "npm 用户前缀"),
            (home / ".local" / "bin" / "codex", "~/.local/bin"),
            (Path("/usr/local/bin/codex"), "系统 /usr/local"),
            (Path("/usr/bin/codex"), "系统 /usr"),
            (Path("/opt/homebrew/bin/codex"), "Homebrew (macOS ARM)"),
        ]
        nr = _npm_global_root()
        if nr:
            cands += [
                (Path(nr) / "bin" / "codex", "npm root bin"),
                (Path(nr) / "@openai" / "codex" / "bin" / "codex.js", "npm root js"),
            ]
    return _filter_existing(cands)


def detect_codex() -> List[Tuple[Path, str]]:
    return _codex_candidates()


# --------------------------------------------------------------------------- #
# 检测：Claude
# --------------------------------------------------------------------------- #

def _claude_candidates() -> List[Tuple[Path, str]]:
    home = Path.home()
    cands: List[Tuple[Path, str]] = []
    w = _which("claude")
    if w:
        cands.append((Path(w), "PATH"))
    if IS_WINDOWS:
        cands += [
            (home / ".local" / "bin" / "claude.exe", "~/.local/bin"),
            (home / ".claude" / "local" / "claude.exe", "原生安装 ~/.claude/local"),
        ]
        nr = _npm_global_root()
        if nr:
            cands.append((Path(nr) / "@anthropic-ai" / "claude-code" / "cli.js", "npm root"))
    else:
        cands += [
            (home / ".claude" / "local" / "claude", "原生安装 ~/.claude/local"),
            (home / ".local" / "bin" / "claude", "~/.local/bin"),
            (Path("/usr/local/bin/claude"), "系统 /usr/local"),
            (Path("/opt/homebrew/bin/claude"), "Homebrew (macOS ARM)"),
        ]
        nr = _npm_global_root()
        if nr:
            cands.append((Path(nr) / "@anthropic-ai" / "claude-code" / "cli.js", "npm root"))
    return _filter_existing(cands)


def _cc_haha_candidates() -> List[Tuple[Path, str]]:
    """检测 Claude Code Haha 桌面端。"""
    cands: List[Tuple[Path, str]] = []
    home = Path.home()
    if IS_WINDOWS:
        bases = [
            Path(os.environ.get("ProgramFiles", "C:/Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
        ]
        for b in bases:
            if not str(b):
                continue
            cands.append((b / "Claude Code Haha" / "Claude Code Haha.exe", "Program Files"))
            if b.exists():
                cands += [(p, "模糊搜索") for p in b.glob("*Haha*/*.exe")]
                cands += [(p, "模糊搜索") for p in b.glob("*haha*/*.exe")]
    elif IS_MACOS:
        cands.append((Path("/Applications/Claude Code Haha.app"), "/Applications"))
        cands += [(p, "模糊搜索") for p in Path("/Applications").glob("*Haha*.app")]
    else:
        cands += [
            (Path("/opt/Claude Code Haha/claude-code-haha"), "/opt"),
            (home / ".local" / "bin" / "claude-code-haha", "~/.local/bin"),
        ]
    return _filter_existing(cands)


def detect_claude() -> Tuple[List[Tuple[Path, str]], List[Tuple[Path, str]]]:
    return _claude_candidates(), _cc_haha_candidates()


# --------------------------------------------------------------------------- #
# 版本探测
# --------------------------------------------------------------------------- #

def _run_version(binpath: Path, extra_args: Optional[List[str]] = None) -> str:
    args = [str(binpath)] + (extra_args or [])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=40)
        out = (r.stdout or r.stderr).strip().splitlines()
        return out[0] if out else "unknown"
    except Exception as e:
        return f"unknown ({type(e).__name__})"


# --------------------------------------------------------------------------- #
# 交互式解析（回退）
# --------------------------------------------------------------------------- #

def _interactive_pick(tool: str, candidates: List[Tuple[Path, str]]) -> Optional[Path]:
    all_cands = candidates or []
    print(f"\n[!] 未能唯一确定「{tool}」的安装位置，请选择或手动输入：")
    for i, (p, src) in enumerate(all_cands, 1):
        print(f"  {i}. {p}   [{src}]")
    print("  0. 手动输入绝对路径")
    print("     （直接回车放弃，跳过该工具的路径探测）")
    try:
        choice = input("> 请输入序号： ").strip()
    except EOFError:
        return None
    if choice == "":
        return None
    if choice == "0":
        p = input("> 请输入绝对路径： ").strip()
        return Path(p).expanduser() if p else None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(all_cands):
            return all_cands[idx][0]
    except ValueError:
        pass
    return None


# --------------------------------------------------------------------------- #
# 环境变量设置（跨平台）
# --------------------------------------------------------------------------- #

def set_user_env_var(name: str, value: str) -> bool:
    """以用户级持久化方式设置环境变量。返回是否成功。"""
    if IS_WINDOWS:
        enc = base64.b64encode(value.encode("utf-16-le")).decode("ascii")
        ps = (
            f'$v=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String("{enc}"));'
            f'[Environment]::SetEnvironmentVariable("{name}",$v,"User")'
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            log.info("已设置[用户级]环境变量 %s", name)
            return True
        log.warning("设置环境变量 %s 失败：%s", name, (r.stderr or "").strip())
        return False

    rc_files = _shell_rc_files()
    written = False
    for rc in rc_files:
        is_fish = "fish" in str(rc).lower()
        line = f'set -gx {name} "{value}"' if is_fish else f'export {name}="{value}"'
        content = rc.read_text(encoding="utf-8", errors="ignore") if rc.exists() else ""
        if is_fish:
            pattern = rf'^\s*set\s+(?:-gx|-x)\s+{re.escape(name)}\b.*$'
        else:
            pattern = rf'^\s*export\s+{re.escape(name)}=.*$'
        new_content, subs = re.subn(pattern, line, content, flags=re.MULTILINE)
        if subs > 0:
            content = new_content
        else:
            content = content.rstrip("\n") + "\n" + line + "\n"
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text(content, encoding="utf-8")
        written = True
        log.info("已写入 %s: %s", rc, line)
    return written


def _shell_rc_files() -> List[Path]:
    shell = os.environ.get("SHELL", "")
    home = Path.home()
    if IS_MACOS:
        return [home / ".zprofile", home / ".zshrc", home / ".bash_profile"]
    if IS_LINUX:
        if "zsh" in shell:
            return [home / ".zshrc", home / ".profile"]
        if "fish" in shell:
            return [home / ".config" / "fish" / "config.fish"]
        return [home / ".bashrc", home / ".profile"]
    return [home / ".profile"]


# --------------------------------------------------------------------------- #
# Codex 配置写入
# --------------------------------------------------------------------------- #

def _mcp_entry_paths() -> tuple:
    """返回 MCP server 的 (command, args) 注册路径。

    command = 当前 Python 解释器
    args = 同目录 mcp_server.py 的绝对路径
    """
    cmd = sys.executable
    mcp_script = str(Path(__file__).parent / "mcp_server.py")
    return (cmd, [mcp_script])


def _build_codex_config(base_url: str, trusted_projects: Optional[List[str]] = None) -> str:
    # 注意：base_url 必须带 /v1 后缀。codex 的 wire_api="responses" 会直接拼
    # base_url + "/responses"，缺 /v1 时请求打到不存在的端点，模型连接 404。
    if not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    lines = [
        'model_provider = "custom"',
        f'model = "{DEFAULT_CODEX_PRIMARY}"',
        'model_reasoning_effort = "high"',
        "disable_response_storage = true",
        "approval_policy = \"on-request\"",
        "# Windows：exec 模式对 sandbox_mode 不生效（0.153.x 已知问题），",
        "# 此处保留 workspace-write 供 TUI 模式使用；exec 需 --sandbox danger-full-access",
        "sandbox_mode = \"workspace-write\"",
        "",
        "# Windows 沙箱兼容：Git Bash (msys/cygwin) 在 codex 受限 token 沙箱下",
        "# 因缺少 SeCreateGlobalPrivilege 无法创建共享内存段（CreateFileMapping",
        "# error 5），导致所有 Git 工具崩溃。指定 PowerShell 作为执行 shell。",
        "[shell]",
        'windows_default = "powershell"',
        "",
        "[model_providers.custom]",
        'name = "cf"',
        'wire_api = "responses"',
        "requires_openai_auth = false",
        'env_key = "CF_GATEWAY_KEY"',
        f'base_url = "{base_url}"',
    ]
    # 信任项目：不加时 codex exec 在非 git 根目录会拒绝运行；
    # 加了 trust_level 后免 --skip-git-repo-check。
    for proj in (trusted_projects or []):
        p = str(Path(proj).expanduser()).replace("\\", "/").lower()
        lines += ["", f"[projects.'{p}']", 'trust_level = "trusted"']
    lines += [
        "",
        "# ===== 模型分档说明 =====",
        "# 0.153+ 使用文件式 profile（V2）：每个档位一个 <名称>.config.toml",
        "# 位于 codex 配置目录下，用法: codex --profile <名称>",
    ]
    return "\n".join(lines) + "\n"


def _write_codex_config(codex_home: Path, base_url: str, force: bool,
                        trusted_projects: Optional[List[str]] = None,
                        skip_mcp: bool = False) -> bool:
    cfg = codex_home / "config.toml"
    if cfg.exists() and not force:
        text = cfg.read_text(encoding="utf-8", errors="ignore")
        if "env_key" in text or "model_provider" in text:
            # 合并式追加信任项目（不覆盖已有 provider 配置）
            merged = _merge_trusted_projects(text, trusted_projects)
            merged = _merge_mcp_servers(merged, skip_mcp)  # MCP 服务器注册
            if merged != text:
                _deploy_write(cfg, merged)
                log.info("config.toml 已合并新增信任项目 / MCP 服务器")
                return True
            log.info("config.toml 已存在配置痕迹，跳过（如需覆盖请用 --force）")
            return False
    new_text = _build_codex_config(base_url, trusted_projects)
    new_text = _merge_mcp_servers(new_text, skip_mcp)  # MCP 服务器注册
    _deploy_write(cfg, new_text)
    return True


def _merge_trusted_projects(text: str, trusted_projects: Optional[List[str]]) -> str:
    """把 trusted_projects 合并进已有 config.toml：已存在的跳过，缺失的追加。"""
    if not trusted_projects:
        return text
    lines = text.rstrip("\n").split("\n")
    # 已存在的 [projects] 键形如 projects.'c:/github/aigx'；
    # strip("[]'\"") 会同时去掉首尾的 [ ' " 字符，得到 projects.'c:/github/aigx'
    # （尾部的 '] 一起被剥掉）。此处改用精确解析：提取引号内路径。
    import re as _re
    existing = set()
    for ln in lines:
        m = _re.match(r"\s*\[projects\.'([^']+)'\]", ln.strip())
        if m:
            existing.add(m.group(1).lower())
    appended = False
    for proj in trusted_projects:
        key = str(Path(proj).expanduser()).replace("\\", "/").lower()
        if key in existing:
            continue
        lines += ["", f"[projects.'{key}']", 'trust_level = "trusted"']
        appended = True
    if not appended:
        return text
    return "\n".join(lines) + "\n"


def _merge_mcp_servers(text: str, skip_mcp: bool = False) -> str:
    """把 [mcp_servers.multi_model] 段合并进 config.toml 文本。

    规则：
    - skip_mcp=True → 原样返回（不注册）
    - 段不存在 → 追加
    - 段存在且 command/args 与当前路径等价 → 幂等返回（零改动）
    - 段存在但 args 指向的 mcp_server.py 不存在（失效路径）→ 整段重写
    - 绝不写入 key/token/env

    返回合并后的文本。
    """
    if skip_mcp:
        return text

    cmd, args = _mcp_entry_paths()
    # TOML 里路径用双反斜杠
    cmd_toml = cmd.replace("\\", "\\\\")
    # TOML 数组：args = ["C:\\path\\mcp_server.py"]
    args_toml = "[" + ", ".join(f'"{a.replace(chr(92), chr(92)*2)}"' for a in args) + "]"

    mcp_section = f'''
[mcp_servers.multi_model]
command = "{cmd_toml}"
args = {args_toml}'''

    # 检查是否已存在 [mcp_servers.multi_model] 段
    # 用 DOTALL + 预查到下一个段头 \n[xxx] 或文本结尾，避免 args 数组里的 [ 截断匹配
    pattern = r'\[mcp_servers\.multi_model\].*?(?=\n\[[^\]]*\]|\Z)'
    match = re.search(pattern, text, re.DOTALL)
    if match is None:
        # 段不存在 → 追加
        if text and not text.endswith("\n"):
            text += "\n"
        return text + mcp_section + "\n"

    # 段已存在 → 检查是否需要更新
    existing = match.group(0)
    # 检查 mcp_server.py 文件是否存在
    mcp_script = args[0]
    if not os.path.isfile(mcp_script):
        # mcp_server.py 不存在，不注册（部署可能还没创建）
        return text

    # 检查现有段是否指向有效路径（用 TOML 转义形式比较，因为 existing 里存的是双反斜杠）
    mcp_script_toml = mcp_script.replace("\\", "\\\\")
    if cmd_toml in existing and mcp_script_toml in existing:
        # 幂等：路径匹配，零改动
        return text

    # 路径不匹配或失效 → 整段重写
    return text[:match.start()] + mcp_section.strip() + "\n" + text[match.end():]


def _write_codex_profiles(codex_home: Path, profiles: Dict[str, Tuple[str, str]]) -> int:
    n = 0
    for prof, (model, effort) in profiles.items():
        f = codex_home / f"{prof}.config.toml"
        if f.exists():
            log.info("profile 文件已存在，跳过 %s", f.name)
            continue
        _deploy_write(f, f'model = "{model}"\nmodel_reasoning_effort = "{effort}"\n')
        n += 1
    return n


def _write_codex_auth(codex_home: Path, api_key: Optional[str]) -> bool:
    auth = codex_home / "auth.json"
    if not api_key:
        return False
    data: Dict = {}
    if auth.exists():
        try:
            loaded = json.loads(auth.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    if data.get("OPENAI_API_KEY") == api_key:
        log.info("auth.json 已含相同密钥，跳过")
        return False
    _deploy_write(auth, json.dumps({**data, "OPENAI_API_KEY": api_key}, ensure_ascii=False, indent=2) + "\n")
    log.info("已写入 auth.json")
    return True


# --------------------------------------------------------------------------- #
# Codex Windows 沙箱体检（只读诊断，不修改系统）
# --------------------------------------------------------------------------- #

def _check_codex_windows_sandbox(codex_bin: Optional[Path], auto_fix: bool = False) -> bool:
    """Windows 下诊断 codex 受限 token 沙箱的已知问题，可选自动修复。

    背景（2026-09 在 Win10 LTSC + codex 0.153.2 实测）：
      1. msys/cygwin 程序（Git Bash 的 ls/bash 等）在受限 token 下缺
         SeCreateGlobalPrivilege，CreateFileMapping 报 error 5 直接崩溃；
      2. codex 沙箱用户（CodexSandboxOnline/Offline）对 C:\\Windows\\Temp
         无写权限时，同样触发 CreateFileMapping error 5；
      3. codex exec 在 Windows 上忽略 sandbox_mode 配置，只有
         --sandbox danger-full-access 可用。
    返回 True 表示沙箱可用（或已修复）。
    """
    if not IS_WINDOWS or not codex_bin:
        return True
    log.info("-" * 60)
    log.info("Codex Windows 沙箱体检：")

    # 1) 检查沙箱用户是否存在
    sandbox_users_ok = True
    for u in ("CodexSandboxOnline", "CodexSandboxOffline"):
        r = subprocess.run(["net", "user", u], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            sandbox_users_ok = False
    if sandbox_users_ok:
        log.info("  [OK] 沙箱用户 CodexSandboxOnline/Offline 存在")
    else:
        log.warning("  [!!] 沙箱用户缺失，首次运行 codex 时会自动创建；若创建失败请以管理员重装")

    # 2) 用 codex sandbox 跑一个原生 exe 验证（原生 exe 不经过 msys，能区分权限问题）
    test_cmd = [str(codex_bin), "sandbox",
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-NoProfile", "-Command", "Write-Output sandbox-ok"]
    try:
        r = subprocess.run(test_cmd, capture_output=True, text=True, timeout=60)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if "sandbox-ok" in out:
            log.info("  [OK] codex sandbox 可执行原生 PowerShell")
            log.info("  提示：Git Bash 工具（ls 等）在 codex 受限沙箱内会因缺")
            log.info("        SeCreateGlobalPrivilege 崩溃；已在 config.toml 配置 PowerShell shell 规避。")
            return True
        if "CreateFileMapping" in err or "error 5" in err:
            log.warning("  [!!] 沙箱内进程崩溃（CreateFileMapping error 5）")
            if auto_fix and _fix_sandbox_temp_acl():
                # 修复后重测一次
                r2 = subprocess.run(test_cmd, capture_output=True, text=True, timeout=60)
                if "sandbox-ok" in (r2.stdout or "").strip():
                    log.info("  [OK] 已自动修复 Temp ACL，codex sandbox 恢复可用")
                    return True
                log.warning("  [!!] 自动修复后仍失败：%s", (r2.stderr or "").strip()[:200])
            else:
                log.warning("      修复（管理员 PowerShell 执行）：")
                log.warning('        icacls C:\\Windows\\Temp /grant "CodexSandboxOnline:(OI)(CI)F"')
                log.warning('        icacls C:\\Windows\\Temp /grant "CodexSandboxOffline:(OI)(CI)F"')
        elif "blocked by policy" in err:
            log.warning("  [!!] 命令被 codex exec 策略拦截（Windows 已知问题）")
            log.warning("      建议：codex exec --sandbox danger-full-access \"...\"（无隔离，仅信任任务使用）")
        else:
            log.warning("  [!!] codex sandbox 测试未通过：%s", err[:200] or out[:200])
    except Exception as e:
        log.warning("  [!!] 无法运行 codex sandbox 测试：%s", e)
    return False


def _fix_sandbox_temp_acl() -> bool:
    """给 codex 沙箱用户授予 C:\\Windows\\Temp 写权限（修复 CreateFileMapping error 5）。

    需要 Administrator；失败时返回 False（调用方退回打印手动修复指引）。
    这是 codex 沙箱用户创建时本应自带的权限，损坏场景（如删除 ~/.codex 重装）需重新授予。
    """
    enc = base64.b64encode("y".encode("utf-16-le")).decode("ascii")
    ps_lines = []
    for u in ("CodexSandboxOnline", "CodexSandboxOffline"):
        ps_lines.append(
            f'$ErrorActionPreference="Continue"; '
            f'icacls C:\\Windows\\Temp /grant "{u}:(OI)(CI)F" /Q 2>&1 | Out-Null'
        )
    ps = "; ".join(ps_lines)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            log.info("  [fix] 已尝试授予沙箱用户 C:\\Windows\\Temp 写权限")
            return True
        log.warning("  [fix] 自动修复失败（可能非管理员）：%s", (r.stderr or "").strip()[:200])
    except Exception as e:
        log.warning("  [fix] 自动修复异常：%s", e)
    return False


def _write_codex_agents_md(codex_home: Path, scope: str = "global") -> bool:
    """写子代理委派分工说明（AGENTS.md）。

    scope=global → ~/.codex/AGENTS.md（所有项目生效）；
    scope=project → 当前工作目录/AGENTS.md（仅该仓库生效）。

    让 Codex 主模型在对话里主动调用 MCP 的 spawn_agent 工具，
    把不同子任务委派给不同模型（全部走自定义 API 网关）。
    幂等：文件已存在且内容一致时跳过；不一致时按部署流程备份重写。
    """
    content = """# 多模型子代理委派指南（自定义 API）

本机配置了多模型共用网关，你可以通过 MCP 工具 `spawn_agent` 把子任务委派给
其他模型完成。委派后由你负责汇总与最终交付。

## 可用子代理

| 子代理 | 模型 | 适合委派的任务 |
|--------|------|----------------|
| 指挥官 | gpt-6-astra | 方案设计、横向对比、任务拆解 |
| 分析   | gpt-5.6-sol | 代码审查、架构分析、排查疑难 bug |
| 写码   | gpt-5.6-luna | 实现函数、写文件、按规格落地代码 |
| 快速   | gpt-5.6-sol-fast | 小修补、快速整理、批量机械修改 |
| 快答   | gpt-5.6-luna-fast | 文档、注释、文案、commit 说明 |

也支持别名：astra / sol / luna / sol-fast / luna-fast。

## 委派原则

1. 子任务要自包含：写清输入、期望输出格式，必要时用 context 附背景材料。
2. 一次任务最多委派 2~4 个子代理，避免过度拆解；简单任务不要委派。
3. 委派返回后由你校验与汇总，不要直接复制子代理输出当最终答案。
4. 需要真正落地文件/跑命令的工程任务，优先用 `multi_team_start`（五阶段
   团队流水线），spawn_agent 只用于轻量问答式委派。
5. 网关异常时子代理会返回失败信息，此时你自己完成该子任务并告知用户。

## 工具调用示例

- 让分析代理审查改动：`spawn_agent(agent="分析", task="审查最新改动里的并发问题")`
- 让写码代理实现函数：`spawn_agent(agent="写码", task="实现 parse_config 函数",
  context="现有代码在 src/config.py")`
- 让快答代理写文档：`spawn_agent(agent="luna-fast", task="为 README 补充安装说明")`
"""
    if scope == "project":
        path = Path.cwd() / "AGENTS.md"
        if not (Path.cwd() / ".git").exists():
            log.warning("--agents-scope project 但当前目录不是 git 仓库根，仍写入 %s", path)
    else:
        path = codex_home / "AGENTS.md"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                log.info("AGENTS.md 已是最新（内容一致，跳过）")
                return False
        except Exception:
            pass
    _deploy_write(path, content)
    return True


def configure_codex(cfg: SimpleNamespace) -> Dict:
    result: Dict = {"configured": False}
    codex_home: Path = cfg.codex_home
    codex_home.mkdir(parents=True, exist_ok=True)

    result["config_toml"] = _write_codex_config(
        codex_home, cfg.base_url, cfg.force, getattr(cfg, "trusted_projects", None),
        skip_mcp=getattr(cfg, "skip_mcp", False))
    result["profiles_written"] = _write_codex_profiles(codex_home, cfg.profiles)
    result["auth_written"] = _write_codex_auth(codex_home, cfg.api_key)
    result["agents_md"] = _write_codex_agents_md(codex_home, scope=getattr(cfg, "agents_scope", "global"))

    if cfg.api_key:
        set_user_env_var("CF_GATEWAY_KEY", cfg.api_key)

    result["configured"] = True
    return result


# --------------------------------------------------------------------------- #
# Claude 配置写入
# --------------------------------------------------------------------------- #

def _write_claude_settings(claude_home: Path, base_url: str, api_key: Optional[str]) -> bool:
    """合并式写入 settings.json，保留用户已有字段。

    注意 base_url 不带 /v1 后缀——Claude SDK 会自己拼 /v1/messages，
    手动加 /v1 会变成 /v1/v1/messages 导致 404（setup_yume.py 同款教训）。
    """
    settings = claude_home / "settings.json"
    existing: Dict = {}
    if settings.exists():
        try:
            loaded = json.loads(settings.read_text(encoding="utf-8"))
        except Exception:
            log.warning("settings.json 解析失败，将视为空配置重建")
            loaded = {}
        if isinstance(loaded, dict):
            existing = loaded
        else:
            log.warning("settings.json 内容不是 JSON 对象，将视为空配置重建")

    env = dict(existing.get("env", {}) or {})
    env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")
    if base_url.rstrip("/").endswith("/v1"):
        env["ANTHROPIC_BASE_URL"] = base_url.rstrip("/")[:-3]  # 剥掉 /v1
    if api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
    for k, v in CLAUDE_MODEL_ENV.items():
        env.setdefault(k, v)
    env.setdefault("CLAUDE_DANGEROUS_MODE", "1")  # 所有会话强制跳过权限

    perms = dict(existing.get("permissions", {}) or {})
    allow = list(dict.fromkeys(
        list(perms.get("allow", []) or []) + PERMISSIONS_ALLOW))

    new_cfg = dict(existing)
    new_cfg["env"] = env
    new_cfg["model"] = existing.get("model", "glm-5.3")
    new_cfg.setdefault("language", "chinese")
    new_cfg["defaultMode"] = "bypassPermissions"
    new_cfg["autoApprove"] = True
    new_cfg["alwaysSkipPermissionPrompt"] = True
    new_cfg["permissions"] = {"allow": allow, "deny": perms.get("deny", []) or []}
    # 注册非官方模型名，否则官方 CLI 报 unrecognized_model
    new_cfg["modelPicker"] = MODEL_PICKER
    _deploy_write(settings, json.dumps(new_cfg, ensure_ascii=False, indent=2) + "\n")
    log.info("modelPicker 已注册 %d 个模型；权限全放行 + bypassPermissions",
             len(MODEL_PICKER["options"]))
    return True


_AGENT_PROMPTS = {
    "code-reviewer": (
        "你是资深代码审查员，负责审查代码改动的正确性、安全性与性能。\n"
        "\n任务边界：\n"
        "- 只做只读分析，禁止修改任何文件\n"
        "- 按「严重度（高/中/低）」输出问题清单，每条附 文件:行号 和具体修复建议\n"
        "- 重点关注：空引用与边界条件、并发竞态、资源泄漏、注入风险、被吞噬的异常\n"
        "- 结论必须来自实际读到的代码，不确定的问题标注「待验证」，不臆断\n"
        "- 输出使用简体中文，代码与标识符保持原样\n"
    ),
    "fast-writer": (
        "你是快速文档与修补助手，负责轻量任务：撰写/修改注释、README、提交说明、"
        "简单文案，以及机械性的小修改。\n"
        "\n任务边界：\n"
        "- 保持改动最小，不顺手重构无关内容\n"
        "- 文档用简体中文，技术术语与代码标识符保留英文原文\n"
        "- 遇到需要复杂逻辑判断的问题不擅自处理，建议转交主对话\n"
        "- 改动完成后简述改了哪些文件、为什么\n"
    ),
    "architect": (
        "你是方案架构师，负责技术方案设计与决策支持。\n"
        "\n任务边界：\n"
        "- 只输出方案与计划，不直接修改代码\n"
        "- 方案至少给出两个候选，横向对比优劣（复杂度/风险/可维护性），最后给出明确推荐\n"
        "- 评估影响面时，列出会触碰的文件与模块清单\n"
        "- 对模糊需求主动列出关键疑问，而不是假设\n"
        "- 输出使用简体中文\n"
    ),
}


def _agent_body(name: str, model: str, description: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"model: {model}\n"
        "---\n"
    )


def _write_claude_agents(claude_home: Path, agents: Dict[str, Tuple[str, str]]) -> int:
    n = 0
    for name, (model, description) in agents.items():
        f = claude_home / "agents" / f"{name}.md"
        if f.exists():
            text = f.read_text(encoding="utf-8", errors="ignore")
            if f"model: {model}" in text:
                log.info("子代理已存在且模型一致，跳过 %s", f.name)
                continue
        _deploy_write(f, _agent_body(name, model, description) + _AGENT_PROMPTS.get(name, ""))
        n += 1
    return n


def configure_claude(cfg: SimpleNamespace) -> Dict:
    result: Dict = {"configured": False}
    claude_home: Path = cfg.claude_home
    claude_home.mkdir(parents=True, exist_ok=True)

    result["settings_written"] = _write_claude_settings(claude_home, cfg.base_url, cfg.api_key)
    result["agents_written"] = _write_claude_agents(claude_home, cfg.agents)

    for k, v in CLAUDE_MODEL_ENV.items():
        set_user_env_var(k, v)
    if cfg.api_key:
        set_user_env_var("ANTHROPIC_AUTH_TOKEN", cfg.api_key)
    set_user_env_var("CLAUDE_DANGEROUS_MODE", "1")
    # 清掉会致命的 ANTHROPIC_MODEL（官方 CLI 本地校验 → unrecognized_model）
    if IS_WINDOWS:
        subprocess.run(["reg", "delete", r"HKCU\Environment", "/v", "ANTHROPIC_MODEL", "/f"],
                       capture_output=True, text=True, timeout=30)

    result["configured"] = True
    return result


# --------------------------------------------------------------------------- #
# 依赖检查
# --------------------------------------------------------------------------- #

def _check_deps() -> None:
    ver = sys.version_info
    log.info("Python 版本: %d.%d.%d", ver.major, ver.minor, ver.micro)
    if (ver.major, ver.minor) < (3, 10):
        log.warning("建议使用 Python 3.10+，当前版本较旧")
    log.info("npm: %s", _which("npm") or "未找到（可选）")
    log.info("git: %s", _which("git") or "未找到（可选）")


# --------------------------------------------------------------------------- #
# 密钥解析
# --------------------------------------------------------------------------- #

def _resolve_key(cfg: SimpleNamespace) -> Optional[str]:
    """获取网关密钥：参数 > 密钥文件 > 脚本内置 DEFAULT_API_KEY > 现有配置复用 > 交互。"""
    if cfg.api_key:
        return cfg.api_key
    if cfg.key_file:
        try:
            k = Path(cfg.key_file).read_text(encoding="utf-8").strip()
            if k:
                return k
        except Exception as e:
            log.warning("读取密钥文件失败: %s", e)
    if DEFAULT_API_KEY:
        log.info("使用脚本内置的 DEFAULT_API_KEY")
        return DEFAULT_API_KEY
    for label, path, getter in (
        ("codex auth.json", cfg.codex_home / "auth.json", lambda d: d.get("OPENAI_API_KEY")),
        ("claude settings.json", cfg.claude_home / "settings.json",
         lambda d: (d.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN")),
    ):
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    k = getter(loaded)
                    if k:
                        log.info("从现有 %s 复用了网关密钥", label)
                        return k
            except Exception:
                pass
    if cfg.non_interactive:
        log.error("未提供密钥且无法自动复用，--non-interactive 模式下终止")
        return None
    try:
        k = input("> 请输入网关 API Key（sk-...，直接回车跳过）： ").strip()
        return k or None
    except EOFError:
        return None


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #

def _resolve_paths(cfg: SimpleNamespace) -> None:
    """自动检测或通过交互/参数解析 codex / claude 的路径。"""
    if cfg.codex_bin:
        binpath = Path(cfg.codex_bin).expanduser()
        if binpath.exists():
            cfg.resolved_codex = (binpath, "命令行覆盖")
            log.info("Codex 路径（命令行覆盖）: %s", binpath)
        else:
            log.error("--codex-bin 指定的路径不存在: %s", binpath)
    else:
        cands = detect_codex()
        if cands:
            cfg.resolved_codex = cands[0]
            log.info("检测到 Codex: %s [%s]", cands[0][0], cands[0][1])
            if len(cands) > 1:
                log.debug("其它候选: %s", [str(p) for p, _ in cands[1:]])
        else:
            log.warning("未能自动检测 Codex")
            if not cfg.non_interactive:
                fuzzy = _fuzzy_find("codex", [Path.home(), Path("/usr/local"), Path("/opt"), Path("/usr")])
                picked = _interactive_pick("Codex", cands + fuzzy)
                if picked:
                    cfg.resolved_codex = (picked, "交互指定")

    if cfg.claude_bin:
        binpath = Path(cfg.claude_bin).expanduser()
        if binpath.exists():
            cfg.resolved_claude = (binpath, "命令行覆盖")
            log.info("Claude 路径（命令行覆盖）: %s", binpath)
        else:
            log.error("--claude-bin 指定的路径不存在: %s", binpath)
    else:
        cli_cands, desktop_cands = detect_claude()
        if cli_cands:
            cfg.resolved_claude = cli_cands[0]
            log.info("检测到 Claude CLI: %s [%s]", cli_cands[0][0], cli_cands[0][1])
        else:
            log.info("未检测到独立 Claude CLI（可能通过桌面端使用）")
        if desktop_cands:
            cfg.resolved_cc_haha = desktop_cands[0]
            log.info("检测到 Claude Code Haha: %s [%s]", desktop_cands[0][0], desktop_cands[0][1])
        elif not cli_cands and not cfg.non_interactive:
            picked = _interactive_pick("Claude", desktop_cands + cli_cands)
            if picked:
                cfg.resolved_claude = (picked, "交互指定")


def _version_or_unknown(path: Optional[Path], args: Optional[List[str]] = None) -> str:
    if path is None:
        return "未检测到"
    return _run_version(path, args)


# --------------------------------------------------------------------------- #
# 参数与日志
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="通用 Claude / Codex 本地配置部署脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help="自定义网关地址（自动补 /v1 后缀，缺失时 codex responses 端点会 404）；换网关也可以直接改脚本里的 DEFAULT_BASE_URL")
    p.add_argument("--api-key", default=None,
                   help="网关 API Key（优先级最高；不提供则依次尝试 --key-file / 脚本内置 DEFAULT_API_KEY / 复用现有配置 / 交互询问）")
    p.add_argument("--key-file", default=None, help="从文件读取 API Key")
    p.add_argument("--codex-home", default=None, help="Codex 配置目录（默认 ~/.codex）")
    p.add_argument("--claude-home", default=None, help="Claude 配置目录（默认 ~/.claude）")
    p.add_argument("--codex-bin", default=None, help="覆盖 Codex 可执行文件路径")
    p.add_argument("--claude-bin", default=None, help="覆盖 Claude 可执行文件路径")
    p.add_argument("--models-file", default=None, help="JSON 文件，覆盖 Codex 模型分档")
    p.add_argument("--trust-project", action="append", default=None, metavar="DIR",
                   help="将目录标记为 codex 信任项目（写入 config.toml 的 [projects] trust_level，可重复传）")
    p.add_argument("--auto-fix", action="store_true",
                   help="Windows 下自动修复 codex 沙箱 Temp ACL（需管理员；默认只体检并打印修复指引）")
    p.add_argument("--skip-codex", action="store_true", help="跳过 Codex 配置")
    p.add_argument("--skip-claude", action="store_true", help="跳过 Claude 配置")
    p.add_argument("--skip-mcp", action="store_true", help="跳过 MCP 服务器注册（默认自动注册 multi_model MCP server）")
    p.add_argument("--agents-scope", choices=["global", "project"], default="global",
                   help="AGENTS.md 子代理指南作用域：global=~/.codex（默认），project=当前工作目录仓库根")
    p.add_argument("--dry-run", action="store_true", help="仅探测，不写任何配置")
    p.add_argument("--non-interactive", action="store_true", help="非交互模式，检测失败直接报错")
    p.add_argument("--force", action="store_true", help="覆盖已存在的 config.toml")
    p.add_argument("--rollback", action="store_true", help="回滚最近一次配置写入")
    p.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    p.add_argument("--log-file", default=None, help="日志输出文件路径")
    return p


def _setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        datefmt="%H:%M:%S",
    )


def main(argv: Optional[List[str]] = None) -> int:
    _setup_stdout_utf8()
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, args.log_file)

    if args.rollback:
        return 0 if rollback() else 1

    home = Path.home()
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else (home / ".codex")
    claude_home = Path(args.claude_home).expanduser() if args.claude_home else (home / ".claude")

    # 模型分档硬编码在 DEFAULT_CODEX_PROFILES（单人使用，不读外部注册表）
    profiles = DEFAULT_CODEX_PROFILES
    if args.models_file:
        try:
            raw = json.loads(Path(args.models_file).read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("顶层必须是 JSON 对象")
            profiles = {}
            for k, v in raw.items():
                if not isinstance(v, (list, tuple)) or len(v) < 2:
                    raise ValueError(f"档位 {k} 的值必须是 [模型名, effort]")
                profiles[str(k)] = (str(v[0]), str(v[1]))
        except Exception as e:
            log.error("解析 --models-file 失败: %s", e)
            return 1

    log.info("=" * 60)
    log.info("开始部署 Claude / Codex 本地配置")
    log.info("平台: %s | 主目录: %s", platform.system(), home)
    log.info("网关: %s", args.base_url)

    _check_deps()

    cfg = SimpleNamespace(
        base_url=args.base_url,
        api_key=args.api_key,
        key_file=args.key_file,
        codex_home=codex_home,
        claude_home=claude_home,
        codex_bin=args.codex_bin,
        claude_bin=args.claude_bin,
        profiles=profiles,
        agents=CLAUDE_AGENT_SPECS,
        trusted_projects=args.trust_project,
        non_interactive=args.non_interactive,
        force=args.force,
        skip_mcp=args.skip_mcp,
        agents_scope=args.agents_scope,
        resolved_codex=None,
        resolved_claude=None,
        resolved_cc_haha=None,
    )

    _resolve_paths(cfg)

    log.info("-" * 60)
    log.info("探测结果摘要：")
    codex_path = cfg.resolved_codex[0] if cfg.resolved_codex else None
    claude_path = cfg.resolved_claude[0] if cfg.resolved_claude else None
    log.info("  Codex 可执行: %s", _version_or_unknown(codex_path, ["--version"]))
    if cfg.resolved_codex:
        log.info("    (路径 %s [%s])", cfg.resolved_codex[0], cfg.resolved_codex[1])
    log.info("  Claude CLI: %s", _version_or_unknown(claude_path, ["--version"]))
    if cfg.resolved_cc_haha:
        log.info("  Claude Code Haha 桌面端: %s", cfg.resolved_cc_haha[0])
    log.info("  Codex 配置目录: %s", codex_home)
    log.info("  Claude 配置目录: %s", claude_home)

    if args.dry_run:
        log.info("--dry-run 模式，仅探测，不写配置。结束。")
        return 0

    api_key = _resolve_key(cfg)
    cfg.api_key = api_key

    if api_key is None and args.non_interactive and not (args.skip_codex and args.skip_claude):
        log.error("未提供网关密钥且无法从现有配置复用，--non-interactive 模式下终止。请用 --api-key 或 --key-file 提供。")
        return 2

    summary: Dict = {}
    if not args.skip_claude:
        summary["claude"] = configure_claude(cfg)
    if not args.skip_codex:
        summary["codex"] = configure_codex(cfg)
        summary["sandbox_ok"] = _check_codex_windows_sandbox(codex_path, auto_fix=args.auto_fix)

    log.info("=" * 60)
    log.info("部署完成。摘要：")
    for tool, r in summary.items():
        log.info("  %s: %s", tool, r)

    # MCP 服务器自检
    if not args.skip_mcp and not args.skip_codex:
        mcp_script = str(Path(__file__).parent / "mcp_server.py")
        if os.path.isfile(mcp_script):
            try:
                r = subprocess.run([sys.executable, mcp_script, "--self-check"],
                                   timeout=30, capture_output=True, text=True, encoding="utf-8", errors="replace")
                if r.returncode == 0:
                    log.info("  MCP 自检: 通过（%s）", r.stderr.strip().split("\n")[0] if r.stderr else "OK")
                else:
                    log.warning("  MCP 自检: 失败（退出码 %d），MCP 服务器可能无法启动", r.returncode)
            except Exception as e:
                log.warning("  MCP 自检: 异常 %s（不阻断部署）", e)
        else:
            log.warning("  mcp_server.py 不存在，跳过 MCP 自检")

    log.info("")
    log.info("后续提示：")
    log.info("  1. 环境变量已写入用户级，请「重开终端」使其生效")
    log.info("  2. Codex 切模型：codex --profile fast/sfast/mid/qwen/qwenp/hw/code/deep")
    log.info("  3. Claude 子代理：code-reviewer(gpt-5.6-sol) / fast-writer(gpt-5.6-luna-fast) / architect(gpt-6-astra)")
    log.info("  4. 撤销本次改动：python %s --rollback", Path(__file__).name)
    if IS_WINDOWS:
        log.info("  5. Windows 已知问题：codex exec 的 workspace-write 在部分版本不生效，")
        log.info("     需要 codex exec --sandbox danger-full-access \"...\"（无隔离，仅信任任务使用）")
    return 0
if __name__ == "__main__":
    main()
