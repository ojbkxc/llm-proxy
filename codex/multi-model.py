#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
multi-model.py — 多模型协作编排器（真·多模型共用 + 工具 + 审查迭代）

不依赖 codex 的 spawn_agent 子代理工具（CLI 0.153.x 不注入该工具，已实测），
直接通过网关并行调用多个真实模型，并自带工具层，
让不同模型组成一个「能读代码、能写文件、能跑命令、能互相审查迭代」的团队。

角色分工（五个 gpt-* 假名，网关层转发到真实模型）:
    指挥官   gpt-6-astra       → glm-5.3                 总指挥 / 拆解 / 汇总
    分析     gpt-5.6-sol       → deepseek-v4-pro-0813    深度分析 / 设计 / 审查
    写码     gpt-5.6-luna      → kimi-k2.7-code          实现 / 编码 / 落地文件
    快速     gpt-5.6-sol-fast  → deepseek-v4-flash-0731  快速杂活 / 整理
    快答     gpt-5.6-luna-fast → glm-5.3-flash           快速问答

用法:
    python multi-model.py ask <模型> "问题"            单模型问答
    python multi-model.py team "任务" [--workdir DIR]  多模型团队流水线(并行写文件+审查迭代)
    python multi-model.py list                         列出可用模型
    python multi-model.py                              数字交互菜单

依赖: 仅 Python 标准库。网关地址与 Key 优先读同目录 deploy_ai_cli.py
      （DEFAULT_BASE_URL / DEFAULT_API_KEY），改那一处本脚本即跟着换网关；
      兜底取 CF_GATEWAY_KEY / CUSTOM_API_KEY / ANTHROPIC_AUTH_TOKEN / --api-key。
"""
import argparse
import json
import os
import re
import secrets
import shlex
import ssl
import subprocess
import threading
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# Windows 控制台默认 GBK，遇到生僻字会抛 UnicodeEncodeError；统一重配为 UTF-8
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

UA = "codex-cli"

# ============ 配置加载：环境变量 > .env > 硬编码默认 ============
# .env 是唯一配置文件（KEY=VALUE，标准格式），放在本脚本同目录。
# 有 .env 用 .env；没有则用下面硬编码默认（主网关 ws-proxy + 备选 cfapi）。
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


def _norm_url(u):
    u = (u or "").strip().rstrip("/")
    if not u:
        return u
    return u if u.endswith("/v1") else u + "/v1"


_DOTENV = _load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE_URL = _norm_url(os.environ.get("BASE_URL") or _DOTENV.get("BASE_URL")
                     or "http://127.0.0.1:8787/v1")
DEFAULT_API_KEY = (os.environ.get("API_KEY") or os.environ.get("CF_GATEWAY_KEY")
                   or _DOTENV.get("API_KEY")
                   or "sk-wa-f9cb7d4ba48f403797fc3f55b928ceac")
FALLBACK_URL = _norm_url(os.environ.get("BASE_FALLBACK") or _DOTENV.get("BASE_FALLBACK")
                         or "https://cfapi.1232333.xyz/v1")
FALLBACK_KEY = (os.environ.get("BASE_FALLBACK_KEY") or _DOTENV.get("BASE_FALLBACK_KEY")
                or DEFAULT_API_KEY)

GATEWAYS = [
    {"name": "ws-proxy", "base_url": BASE_URL,
     "api_key": DEFAULT_API_KEY,
     "models": {"gpt-6-astra": "gpt-6-astra", "gpt-5.6-luna": "gpt-5.6-luna",
                "gpt-5.6-luna-fast": "gpt-5.6-luna-fast", "gpt-5.6-sol": "gpt-5.6-sol",
                "gpt-5.6-sol-fast": "gpt-5.6-sol-fast",
                "qwen3.8-max": "qwen3.8-max", "qwen3.7-plus": "qwen3.7-plus",
                "hw-glm-5": "hw-glm-5"}},
    {"name": "fallback", "base_url": FALLBACK_URL,
     "api_key": FALLBACK_KEY,
     "models": {"gpt-6-astra": "gpt-6-astra", "gpt-5.6-luna": "gpt-5.6-luna",
                "gpt-5.6-luna-fast": "gpt-5.6-luna-fast", "gpt-5.6-sol": "gpt-5.6-sol",
                "gpt-5.6-sol-fast": "gpt-5.6-sol-fast"}},
]

# 网关排障状态（circuit breaker）：按网关名记录 (连续失败数, 冷却到时间点)
_GW_STATE = {}
_GW_LOCK = threading.Lock()

# 多模型共用：全部硬编码在此，单机单人使用，不读任何外部注册表。
ROLES = {
    "指挥官": {"model": "gpt-6-astra",       "temp": 0.4, "fallback": "gpt-5.6-luna"},
    "分析":   {"model": "gpt-5.6-sol",       "temp": 0.3, "fallback": "gpt-5.6-luna"},
    "写码":   {"model": "gpt-5.6-luna",      "temp": 0.2, "fallback": "gpt-5.6-sol"},
    "快速":   {"model": "gpt-5.6-sol-fast",  "temp": 0.5, "fallback": "gpt-5.6-luna-fast"},
    "快答":   {"model": "gpt-5.6-luna-fast", "temp": 0.5, "fallback": "gpt-5.6-sol-fast"},
}

ALIASES = {
    # gpt-* 假名（主用）
    "astra": "gpt-6-astra", "sol": "gpt-5.6-sol", "luna": "gpt-5.6-luna",
    "sol-fast": "gpt-5.6-sol-fast", "luna-fast": "gpt-5.6-luna-fast",
    # 真名兼容（直接写真实模型名也能用）
    "glm": "glm-5.3", "deep": "deepseek-v4-pro-0813", "kimi": "kimi-k2.7-code",
    "dfast": "deepseek-v4-flash-0731", "gfast": "glm-5.3-flash",
}




WORKDIR = os.getcwd()
ALLOW_RUN = True

# 线程局部工作目录：team/auto 在各自线程内设置，避免并发任务串目录。
# 未设置时回退到模块级 WORKDIR（CLI 单线程直接运行时的默认值）。
_tls = threading.local()


def _get_workdir():
    wd = getattr(_tls, "workdir", None)
    return wd if wd else WORKDIR


def _set_workdir(wd):
    _tls.workdir = wd

# ---------------------------------------------------------------------------
# TeamState 持久化：阶段边界原子落盘，支持 --resume 续跑
# ---------------------------------------------------------------------------
STATE_DIR = ".multi-model"
STATE_SCHEMA_V = 1


class StateError(Exception):
    """状态文件损坏/版本不符/缺字段时抛出。"""
    pass


def _gen_task_id(task_type):
    """生成形如 team-20260907-143012-a1b2 的任务 ID。"""
    return f"{task_type}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _state_path(workdir, task_id):
    """状态文件路径：<workdir>/.multi-model/state-<task_id>.json"""
    return os.path.join(workdir, STATE_DIR, f"state-{task_id}.json")


def _save_state_atomic(workdir, state):
    """原子写状态：先写 .tmp 再 os.replace，避免半写文件被 --resume 读到。"""
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    d = os.path.join(workdir, STATE_DIR)
    os.makedirs(d, exist_ok=True)
    final_path = _state_path(workdir, state["task_id"])
    tmp_path = final_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, final_path)


def _load_state(workdir, task_id):
    """读状态文件并校验；任一校验失败抛 StateError 带明确文案。"""
    path = _state_path(workdir, task_id)
    if not os.path.isfile(path):
        raise StateError(f"状态文件不存在: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except json.JSONDecodeError as e:
        raise StateError(f"状态文件 JSON 解析失败: {e}")
    if state.get("schema_v") != STATE_SCHEMA_V:
        raise StateError(
            f"状态文件 schema_v={state.get('schema_v')} 与当前版本 "
            f"{STATE_SCHEMA_V} 不符，可能由不兼容版本写入")
    for k in ("task_id", "task", "phase", "status"):
        if k not in state:
            raise StateError(f"状态文件缺关键字段: {k}")
    return state


def _new_state(task, workdir, allow_risky=False):
    """构造一份全新的流水线状态 dict。"""
    now = datetime.now().isoformat(timespec="seconds")
    return {
        "schema_v": STATE_SCHEMA_V,
        "task_id": _gen_task_id("team"),
        "task": task,
        "status": "running",
        "phase": 1,
        "round": 0,
        "allow_risky": bool(allow_risky),
        "token_start": _daily_token_total(),
        "workdir": workdir,
        "design": {},
        "impl": {},
        "reviews": [],
        "fixes": [],
        "final": {},
        "stage_errors": [],
        "created_at": now,
        "updated_at": now,
    }


def _ssl_ctx():
    ctx = ssl.create_default_context()
    return ctx


def api_key():
    """网关 API Key。优先级：环境变量(CF_GATEWAY_KEY 等) > .env(API_KEY) > 硬编码。"""
    for k in ("CF_GATEWAY_KEY", "CUSTOM_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v
    return DEFAULT_API_KEY


# ---------------------------------------------------------------------------
# 工具层：函数定义 + 执行器
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "列出指定目录下的文件和子目录（相对工作目录）。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径，'.' 表示根"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "description": "最多读的行数，默认 200"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "写入/创建文本文件（会覆盖）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "完整文件内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在文本文件中搜索关键字或正则，返回匹配的行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "目录或文件，默认 '.'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "执行一条 shell 命令并返回输出（用于跑测试/构建等）。",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
    },
]


def _safe_path(path):
    """把相对路径解析到当前线程工作目录内，防止目录穿越。"""
    base = os.path.realpath(_get_workdir())
    p = os.path.realpath(os.path.join(base, path))
    if os.path.commonpath([base, p]) != base:
        raise PermissionError(f"路径越出工作目录: {path}")
    return p


def _tool_list_dir(path):
    p = _safe_path(path)
    if not os.path.isdir(p):
        return f"不是目录: {path}"
    out = []
    for name in sorted(os.listdir(p)):
        full = os.path.join(p, name)
        tag = "[D]" if os.path.isdir(full) else "[F]"
        try:
            size = os.path.getsize(full) if os.path.isfile(full) else ""
        except OSError:
            size = ""
        out.append(f"{tag} {name} {size}".rstrip())
    return "\n".join(out) if out else "(空目录)"


def _tool_read_file(path, limit=200):
    p = _safe_path(path)
    if not os.path.isfile(p):
        return f"文件不存在: {path}"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取失败: {e}"
    n = int(limit) if limit else len(lines)
    if len(lines) > n:
        return "".join(lines[:n]) + f"\n...[截断，共 {len(lines)} 行]"
    return "".join(lines)


def _tool_write_file(path, content):
    p = _safe_path(path)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {path}（{len(content)} 字符）"


def _tool_search(pattern, path="."):
    p = _safe_path(path)
    files = []
    if os.path.isfile(p):
        files = [p]
    else:
        for root, dirs, names in os.walk(p):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv")]
            for n in names:
                files.append(os.path.join(root, n))
    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    hits = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(f, _get_workdir())}:{i}: {line.rstrip()[:160]}")
                        if len(hits) >= 60:
                            return "\n".join(hits) + "\n...[截断]"
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(hits) if hits else "无匹配"


# 危险命令黑名单：命中即拦截（除非 --allow-risky 显式放行）
# 列表用于文档化/测试枚举；实际匹配逻辑在 _match_dangerous 内用 token 化实现，
# 以正确处理 flag 拆分归一（rm -rf / rm -r -f / rm -fr 等价）并避免误报。
DANGEROUS_PATTERNS = [
    {"name": "rm -rf", "type": "token", "pattern": "rm + -r + -f (flag 归一)"},
    {"name": "del /s", "type": "token", "pattern": "del /s"},
    {"name": "rd /s", "type": "token", "pattern": "rd /s"},
    {"name": "Remove-Item -Recurse -Force", "type": "token",
     "pattern": "Remove-Item -Recurse -Force / -rf"},
    {"name": "format", "type": "regex", "pattern": r"^format\s+[a-z]:"},
    {"name": "mkfs", "type": "token", "pattern": "mkfs*"},
    {"name": "dd if=", "type": "token", "pattern": "dd + if="},
    {"name": "diskpart", "type": "token", "pattern": "diskpart"},
    {"name": "shutdown", "type": "token", "pattern": "shutdown"},
    {"name": "fork bomb", "type": "regex",
     "pattern": r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"},
]


def _match_dangerous(cmd):
    """命中危险命令返回模式名，未命中返回 None。大小写不敏感。

    token 化优先 shlex.split（POSIX），失败退空格分割（Windows cmd）。
    误报权衡：format 单词不拦、format c: 才拦；Remove-Item 单词不拦、
    带 -Recurse -Force 才拦；dir/ls/python/pytest 等畅通。
    """
    if not cmd or not cmd.strip():
        return None
    raw = cmd.strip()
    lower = raw.lower()
    try:
        toks = [t.lower() for t in shlex.split(raw, posix=True)]
    except ValueError:
        toks = lower.split()
    if not toks:
        return None
    first = toks[0]

    # rm -rf / rm -fr / rm -r -f / rm -f -r（flag 归一：-rf 等价于 -r + -f）
    if first == "rm" and len(toks) >= 2:
        flags = set()
        for t in toks[1:]:
            if t.startswith("-") and "/" not in t:
                for ch in t[1:]:
                    if ch in ("r", "f"):
                        flags.add(ch)
            else:
                break  # 遇到非 flag 参数停止收集
        if "r" in flags and "f" in flags:
            return "rm -rf"

    # Remove-Item -Recurse -Force（含 -rf 缩写）
    if first == "remove-item" and len(toks) >= 2:
        rest = toks[1:]
        has_recurse = any(t in ("-recurse", "-r") for t in rest)
        has_force = any(t in ("-force", "-f") for t in rest)
        has_rf = any(t == "-rf" for t in rest)
        if (has_recurse and has_force) or has_rf:
            return "Remove-Item -Recurse -Force"

    # del /s / rd /s（Windows cmd）
    if first in ("del", "rd") and len(toks) >= 2 and "/s" in toks[1:]:
        return "del /s" if first == "del" else "rd /s"

    # format c: / format /...（format 单词不拦，必须有盘符或 / 开头参数）
    if first == "format" and len(toks) >= 2:
        if re.match(r"^[a-z]:", toks[1]) or toks[1].startswith("/"):
            return "format"

    # mkfs（token 前缀，如 mkfs.ext4）
    if first.startswith("mkfs"):
        return "mkfs"

    # dd if=
    if first == "dd" and "if=" in lower:
        return "dd if="

    # diskpart（精确首词）
    if first == "diskpart":
        return "diskpart"

    # shutdown（首词精确）
    if first == "shutdown":
        return "shutdown"

    # fork 炸弹 :(){ :|:& };:
    if re.search(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", lower):
        return "fork bomb"

    return None


def _tool_run_command(cmd, allow_risky=False):
    if not ALLOW_RUN:
        return "[已禁用] run_command 被 --no-run 关闭"
    hit = _match_dangerous(cmd)
    if hit and not allow_risky:
        return (f"[已拦截] 命中危险命令黑名单({hit})。"
                "如确需执行，用 --allow-risky 显式放行")
    prefix = f"[风险放行] 命中危险命令({hit})\n" if hit else ""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           cwd=_get_workdir(), timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        if len(out) > 3000:
            out = out[:3000] + "\n...[截断]"
        return prefix + (f"exit={r.returncode}\n{out}" if out.strip()
                         else f"exit={r.returncode} (无输出)")
    except subprocess.TimeoutExpired:
        return prefix + "命令超时(300s)"
    except Exception as e:
        return prefix + f"执行失败: {e}"


def execute_tool(name, args, allow_risky=False):
    args = args or {}
    try:
        if name == "list_dir":
            return _tool_list_dir(args.get("path", "."))
        if name == "read_file":
            return _tool_read_file(args.get("path", ""), args.get("limit", 200))
        if name == "write_file":
            return _tool_write_file(args.get("path", ""), args.get("content", ""))
        if name == "search":
            return _tool_search(args.get("pattern", ""), args.get("path", "."))
        if name == "run_command":
            return _tool_run_command(args.get("cmd", ""), allow_risky=allow_risky)
        return f"未知工具: {name}"
    except Exception as e:
        return f"工具执行出错: {e}"


# ---------------------------------------------------------------------------
# 模型调用
# ---------------------------------------------------------------------------
# ---- 失败审计与熔断 ----
FAILURE_WINDOW = 60          # 熔断统计窗口（秒）
FAILURE_THRESHOLD = 3        # 窗口内连续失败达到此值 → 冷却该网关
COOLDOWN_SEC = 30            # 冷却时长（秒）
RETRY_DELAYS = (0.5, 1.0, 2.0)   # 降级重试前的退避（递增，防打爆上游）


def _failure_log_path():
    return os.path.join(_get_workdir(), ".multi-model", "failures.jsonl")


def _audit_failure(kind, model, gateway, error, elapsed):
    """追加一条失败记录（尽力而为，失败静默）。"""
    try:
        p = _failure_log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind,
               "model": model, "gateway": gateway, "error": str(error)[:200],
               "elapsed": round(elapsed, 2)}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _gw_available(gw_name):
    """熔断检查：冷却期内返回 False。"""
    with _GW_LOCK:
        fails, cooldown_until = _GW_STATE.get(gw_name, (0, 0.0))
        if cooldown_until > time.time():
            return False
        return True


def _gw_record(gw_name, failure):
    """熔断计数：failure=True 记一次失败，连续 FAILURE_THRESHOLD 次 → 进入 COOLDOWN_SEC 冷却；
    failure=False（成功）→ 清零。"""
    with _GW_LOCK:
        if not failure:
            _GW_STATE[gw_name] = (0, 0.0)
            return
        fails, _ = _GW_STATE.get(gw_name, (0, 0.0))
        fails += 1
        if fails >= FAILURE_THRESHOLD:
            _GW_STATE[gw_name] = (0, time.time() + COOLDOWN_SEC)
        else:
            _GW_STATE[gw_name] = (fails, 0.0)


# ---- 每日 token 累计（预算闸门用）----
# 每次 call() 成功后把 usage 累加进「当日」用量文件（按天分桶，跨进程/跨任务累计）。
# team() 以「今天已累计的 token」为口径判断是否超每日上限；默认无上限。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DAILY_LOCK = threading.Lock()


def _daily_token_path():
    return os.path.join(_SCRIPT_DIR, ".multi-model", "token-usage.json")


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _read_daily_usage():
    """读当日累计 (in, out)；日期不是今天或文件损坏 → (0, 0)。"""
    today = _today()
    try:
        with open(_daily_token_path(), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == today:
            return int(d.get("in") or 0), int(d.get("out") or 0)
    except Exception:
        pass
    return 0, 0


def _write_daily_usage(in_, out_):
    p = _daily_token_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": _today(), "in": int(in_), "out": int(out_)}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def _accumulate_usage(usage):
    if not isinstance(usage, dict):
        return
    inc_in = int(usage.get("prompt_tokens") or 0)
    inc_out = int(usage.get("completion_tokens") or 0)
    with _DAILY_LOCK:
        d_in, d_out = _read_daily_usage()
        _write_daily_usage(d_in + inc_in, d_out + inc_out)


def _daily_token_total():
    with _DAILY_LOCK:
        d_in, d_out = _read_daily_usage()
    return d_in + d_out


def call(model, messages, temperature=0.4, max_tokens=4000, timeout=600, tools=None, tool_choice=None):
    """调 /v1/chat/completions，返回完整 message dict。

    降级链（全硬编码）：
      1) 角色级 fallback 换模型（如 写码 luna → sol）；
      2) 换网关（GATEWAYS 顺序：主网关 → 备选）；
      3) 429/5xx/超时才降级；4xx 客户端错误直接抛（换模型救不了）；
      4) 每次降级重试前退避 sleep（0.5s/1s/2s 递增），失败记 failures.jsonl；
      5) 单网关 60s 内连续失败 3 次 → 熔断 30s，期间直接跳过该网关。
    """
    fallback_chain = []
    for role in ROLES.values():
        if role.get("model") == model and role.get("fallback"):
            fallback_chain = [role["fallback"]]
            break
    candidates = [model] + fallback_chain
    last_err = None
    attempt = 0
    for cand in candidates:
        for gw in GATEWAYS:
            if not _gw_available(gw["name"]):
                continue  # 熔断中，跳过
            real_model = gw["models"].get(cand, cand)
            body = {
                "model": real_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if tools:
                body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
            key = gw.get("api_key") or api_key()
            req = urllib.request.Request(
                gw["base_url"] + "/chat/completions", data=json.dumps(body).encode(), method="POST",
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "User-Agent": UA,
                },
            )
            t0 = time.time()
            try:
                resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())
                data = json.loads(resp.read().decode())
                _gw_record(gw["name"], failure=False)  # 成功 → 清零熔断计数
                _accumulate_usage(data.get("usage"))
                return data["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                last_err = e
                _audit_failure("http", cand, gw["name"], e, time.time() - t0)
                if e.code in (408, 429) or 500 <= e.code < 600:
                    _gw_record(gw["name"], failure=True)
                    if attempt < len(RETRY_DELAYS):
                        time.sleep(RETRY_DELAYS[attempt])
                    attempt += 1
                    continue  # 可降级错误 → 下一个候选/网关
                raise  # 4xx 客户端错误不降级
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last_err = sys.exc_info()[1] or last_err
                _audit_failure("network", cand, gw["name"], last_err, time.time() - t0)
                _gw_record(gw["name"], failure=True)
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                attempt += 1
                continue
    if last_err is None:
        raise RuntimeError("模型调用失败（无可用候选）")
    raise last_err


def msg_text(message):
    c = (message.get("content") or "").strip()
    r = (message.get("reasoning_content") or "").strip()
    return c or r or ""


def ask(model, prompt, system=None):
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    return msg_text(call(model, msgs))


# 强制 JSON 提交工具：网关支持 tools 时，模型用 submit_json 提交结构化结果，
# 避免自由文本里混入解释/思考导致 json.loads 失败。
_SUBMIT_JSON_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_json",
        "description": "提交 JSON 结果",
        "parameters": {
            "type": "object",
            "properties": {
                "result": {
                    "type": "string",
                    "description": "JSON 格式的结果字符串",
                },
            },
            "required": ["result"],
        },
    },
}


def extract_json(raw):
    """从模型输出里提取并解析 JSON（仿 OMA structured-output 的容错顺序）。

    依次尝试：整体即合法 JSON → ```json 围栏 → 裸 ``` 围栏 → 首 { 到末 } → 首 [ 到末 ]。
    全部失败抛 ValueError。
    """
    s = (raw or "").strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r"```json\s*([\s\S]*?)```", s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    m = re.search(r"```\s*([\s\S]*?)```", s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    for a, b in ((s.find("{"), s.rfind("}")), (s.find("["), s.rfind("]"))):
        if a != -1 and b > a:
            try:
                return json.loads(s[a:b + 1])
            except Exception:
                pass
    raise ValueError(f"无法从输出提取 JSON，开头: {s[:80]!r}")


def call_json(model, system, user, temperature=0.3, timeout=600, use_tools=True):
    """调模型取 JSON。use_tools=True 时优先用 submit_json 工具强制结构化输出，
    任一异常（网关不支持 tools / 无 tool_calls / json 解析失败）回退 extract_json 截取法。"""
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    if use_tools:
        try:
            m = call(model, msgs, temperature=temperature, timeout=timeout,
                     tools=[_SUBMIT_JSON_TOOL],
                     tool_choice={"type": "function", "function": {"name": "submit_json"}})
            tcs = m.get("tool_calls") or []
            if tcs:
                args_obj = json.loads(tcs[0]["function"].get("arguments", ""))
                return json.loads(args_obj["result"])
            # 无 tool_calls → 落入回退
        except Exception:
            # 网关不支持 tools / 无 tool_calls / json 解析失败 → 回退
            pass
    content = msg_text(call(model, msgs, temperature=temperature, timeout=timeout))
    return extract_json(content)


# ---------------------------------------------------------------------------
# 循环检测：滑动窗口记录工具调用签名，连续重复即判定卡死（防空转烧 token）
# ---------------------------------------------------------------------------
LOOP_MAX_REPEATS = 3
LOOP_WINDOW = 4


def _sort_keys(value):
    """递归排序 dict 键，让 {b:1,a:2} 与 {a:2,b:1} 得到相同 JSON。"""
    if isinstance(value, dict):
        return {k: _sort_keys(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_sort_keys(v) for v in value]
    return value


def _tool_call_signature(tcs):
    """一组工具调用的确定性签名（按名排序 + 参数键排序）。"""
    items = []
    for tc in tcs:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        try:
            args = _sort_keys(json.loads(fn.get("arguments") or "{}"))
        except Exception:
            args = fn.get("arguments") or ""
        items.append((name, args))
    items.sort(key=lambda x: (x[0], json.dumps(x[1], ensure_ascii=False, sort_keys=True)))
    return json.dumps(items, ensure_ascii=False)


def _consecutive_repeats(buf):
    """缓冲区尾部连续相同项的数量；空/无重复返回 0。"""
    if not buf:
        return 0
    last = buf[-1]
    n = 0
    for x in reversed(buf):
        if x == last:
            n += 1
        else:
            break
    return n


def agent_loop(model, system, user, tools=None, temperature=0.3, max_iter=20, allow_risky=False):
    """带工具的 agent 循环：模型自主调用工具直到给出最终回答。返回 (文本, 工具轨迹)。

    内置循环检测：同一工具调用签名连续重复 LOOP_MAX_REPEATS 次即提前停止，防空转烧 token。
    """
    msgs = [{"role": "system", "content": system}]
    if user:
        msgs.append({"role": "user", "content": user})
    trace = []
    tool_sigs = []
    for _ in range(max_iter):
        m = call(model, msgs, temperature=temperature, tools=tools, timeout=600)
        tcs = m.get("tool_calls") or []
        if not tcs:
            return msg_text(m), trace
        sig = _tool_call_signature(tcs)
        tool_sigs.append(sig)
        if len(tool_sigs) > LOOP_WINDOW:
            tool_sigs.pop(0)
        if _consecutive_repeats(tool_sigs) >= LOOP_MAX_REPEATS:
            return (f"(检测到重复工具调用已连续 {LOOP_MAX_REPEATS} 次，判定卡死，停止)", trace)
        # 记录 assistant 的 tool_calls 消息
        msgs.append({"role": "assistant", "content": m.get("content") or "",
                     "tool_calls": tcs})
        for tc in tcs:
            fn = tc["function"]
            name, args = fn["name"], json.loads(fn.get("arguments") or "{}")
            result = execute_tool(name, args, allow_risky=allow_risky)
            trace.append((name, args, result[:200]))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": result})
    return "(达到最大工具调用轮数，已停止)", trace


def spawn_agent(agent, task, context=None, max_tokens=4000):
    """子代理委派：把子任务交给指定子模型（自定义 API，与 call 同链路）。

    agent 支持角色名（指挥官/分析/写码/快速/快答）或别名（astra/sol/luna/...），
    也支持直接写模型名。返回 (模型名, 回答文本)；委派失败时回答以 [子代理失败] 开头。
    """
    model = None
    if agent in ROLES:
        model = ROLES[agent]["model"]
    else:
        model = ALIASES.get(agent)
        if model is None and agent in {r["model"] for r in ROLES.values()}:
            model = agent
    if model is None:
        known = " / ".join(list(ROLES.keys()) + list(ALIASES.keys()))
        return (agent, f"[子代理失败] 未知子代理 {agent}，可用: {known}")

    prompt = f"子任务：{task}"
    if context:
        prompt += f"\n\n背景材料：\n{context}"
    system = f"你是被委派的子代理（模型 {model}）。只完成分配给你的子任务，直接给出结果，不要复述任务。"
    try:
        answer = ask(model, prompt, system=system)
        return (model, answer)
    except Exception as e:
        return (model, f"[子代理失败] {e}")


def spawn_many(delegations, max_workers=4):
    """并行委派多个子代理。delegations = [(agent, task, context_or_None), ...]

    返回 [(agent, model, 回答), ...]，顺序与输入一致；单个失败不影响其他。
    """
    def _run(item):
        agent, task, ctx = (item + (None,))[:3]
        model, answer = spawn_agent(agent, task, context=ctx)
        return (agent, model, answer)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(delegations)))) as ex:
        futs = [ex.submit(_run, d) for d in delegations]
        return [f.result() for f in futs]


# ---------------------------------------------------------------------------
# 交接摘要：阶段间传递结构化上下文，替代 [:800]/[:1200]/[:600] 粗暴截断
# ---------------------------------------------------------------------------
HANDOFF_MAX_CHARS = 4000


def _trunc(s, limit):
    """截断到 limit 字符，超限加 ...[截断]"""
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= limit else s[:limit] + "...[截断]"


def _handoff_design(state):
    """从 design 提取：关键决策(plan 前 1500 字) + 产物文件清单 + 遗留问题(acceptance 前 500 字)。"""
    d = state.get("design") or {}
    parts = []
    plan = d.get("plan") or ""
    parts.append("【关键决策】\n" + _trunc(plan, 1500))
    files = d.get("files") or []
    if files:
        lines = []
        for f in files:
            if isinstance(f, dict):
                lines.append(f"  - {f.get('path', '?')}: {f.get('purpose', '')}")
            else:
                lines.append(f"  - {f}")
        parts.append("【产物文件清单】\n" + "\n".join(lines))
    else:
        parts.append("【产物文件清单】\n(无)")
    acc = d.get("acceptance") or ""
    if acc:
        parts.append("【遗留问题/验收标准】\n" + _trunc(acc, 500))
    return _trunc("\n\n".join(parts), HANDOFF_MAX_CHARS)


def _handoff_impl(state):
    """从 impl 提取：已写文件清单 + 实现摘要前 2000 字。"""
    im = state.get("impl") or {}
    parts = []
    files = im.get("files") or []
    if files:
        parts.append("【已写文件清单】\n" + "\n".join(f"  - {f}" for f in files))
    else:
        parts.append("【已写文件清单】\n(无)")
    summary = im.get("summary") or ""
    parts.append("【实现摘要】\n" + _trunc(summary, 2000))
    return _trunc("\n\n".join(parts), HANDOFF_MAX_CHARS)


def _handoff_review(state, round_num):
    """从 reviews[-1] 提取：verdict + blocking 列表 + minor 列表。"""
    reviews = state.get("reviews") or []
    if not reviews:
        return f"【第 {round_num} 轮审查】\n(无审查记录)"
    r = reviews[-1]
    parts = [f"【第 {r.get('round', round_num)} 轮审查】"]
    parts.append(f"结论: {r.get('verdict', '?')}")
    if r.get("anomalous"):
        parts.append("(结构化异常: 审查解析失败，按 anomalous fix 处理)")
    blocking = r.get("blocking") or []
    if blocking:
        parts.append("阻塞问题:")
        for it in blocking:
            if isinstance(it, dict):
                parts.append(f"  - [{it.get('file', '?')}] {it.get('issue', '')}")
            else:
                parts.append(f"  - {it}")
    minor = r.get("minor") or []
    if minor:
        parts.append("次要问题:")
        for it in minor:
            if isinstance(it, dict):
                parts.append(f"  - [{it.get('file', '?')}] {it.get('issue', '')}")
            else:
                parts.append(f"  - {it}")
    return _trunc("\n".join(parts), HANDOFF_MAX_CHARS)


# ---------------------------------------------------------------------------
# 基础命令
# ---------------------------------------------------------------------------
def parallel(prompt, models=None, max_workers=4):
    models = models or [r["model"] for r in ROLES.values()]
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(models)))) as ex:
        futs = {ex.submit(ask, m, prompt): m for m in models}
        for f in as_completed(futs):
            m = futs[f]
            try:
                results[m] = f.result()
            except Exception as e:
                results[m] = f"[错误] {e}"
    return results


def orchestrate(task):
    print(f"\n[1/3] 指挥官 {ROLES['指挥官']['model']} 拆解任务...")
    plan_prompt = (
        "你是多模型团队的总指挥。把下面的任务拆成 3~5 个可并行、互不依赖的子任务，"
        "每个子任务指定一个角色（分析/写码/快速/快答，任选其一）。"
        "只输出 JSON 数组，不要任何解释。格式：\n"
        '[{"role":"分析","task":"..."},{"role":"写码","task":"..."}]\n\n'
        f"任务：{task}"
    )
    try:
        plan = call_json(ROLES["指挥官"]["model"], "你是总指挥，只输出 JSON。", plan_prompt)
    except Exception as e:
        print(f"[错误] 拆解失败: {e}")
        return
    subs = [s for s in plan if s.get("task") and s.get("role") in ROLES]
    if not subs:
        print("[错误] 拆解结果为空或格式不对")
        return
    print(f"  拆解为 {len(subs)} 个子任务:")
    for i, s in enumerate(subs, 1):
        print(f"    {i}. [{s['role']}] {s['task'][:60]}")

    print(f"\n[2/3] 并行分发给 {len({s['role'] for s in subs})} 个角色执行...")
    outputs = {}

    def run_sub(item):
        role, sub_task = item["role"], item["task"]
        model = ROLES[role]["model"]
        sys_msg = f"你是团队里的「{role}」角色（模型 {model}）。只完成分配给你的子任务，输出简洁。"
        t0 = time.time()
        try:
            c = ask(model, sub_task, system=sys_msg)
            return role, model, sub_task, c, round(time.time() - t0, 1), None
        except Exception as e:
            return role, model, sub_task, "", 0, str(e)

    with ThreadPoolExecutor(max_workers=len(subs)) as ex:
        futs = [ex.submit(run_sub, s) for s in subs]
        for f in as_completed(futs):
            role, model, sub_task, content, dt, err = f.result()
            outputs[(role, sub_task)] = (model, content, dt, err)
            tag = "✓" if not err else "✗"
            print(f"  {tag} [{role}] {model} ({dt}s) {sub_task[:40]}"
                  + (f"  错误: {err}" if err else ""))

    print(f"\n[3/3] 指挥官 {ROLES['指挥官']['model']} 汇总...")
    report = []
    for s in subs:
        model, content, dt, err = outputs[(s["role"], s["task"])]
        body = content if (not err and content) else ("[错误] " + err)
        report.append(f"【{s['role']} / {model}】{s['task']}\n{body[:1200]}")
    summary_prompt = (
        "你是总指挥。以下是各子任务执行结果，请汇总成一份连贯的最终交付。\n"
        "要求：直接输出最终交付内容（中文），不要输出或复述你的思考过程，不要用英文思考，"
        "不要重复原始材料，简洁扼要。结构：结论 / 关键点 / 各模型分工。\n\n"
        + "\n\n".join(report)
    )
    try:
        final = ask(ROLES["指挥官"]["model"], summary_prompt,
                    system="你是总指挥。直接输出最终交付内容，不要输出你的思考过程。")
        if not final or final.startswith("The user") or "thinking" in final[:60].lower():
            final = "\n\n".join(report)
    except Exception as e:
        final = f"[汇总失败] {e}\n\n子任务原始结果：\n\n" + "\n\n".join(report)
    failed = [s for s in subs if outputs[(s["role"], s["task"])][3]]
    if failed:
        final += "\n\n[结构化异常] " + "、".join(
            f"[{s['role']}] {s['task'][:40]}" for s in failed) + " 执行失败"
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60)


# ---------------------------------------------------------------------------
# team 流水线：分析 → 并行写码 → 审查 → 迭代回改 → 汇总
# ---------------------------------------------------------------------------
REVIEW_RETRIES = 2

# 并行写码工人池：把不同文件/模块分配给不同模型并行写
WRITERS = ["写码", "快速", "快答"]


def _plan_parallel_write(file_paths, design):
    """让指挥官把文件清单按模块分组，每组指定一个写码角色。
    返回 [{"writer": 角色, "files": [路径...]}, ...]；失败返回 None。"""
    plan_prompt = (
        "你是总指挥。下面是一份设计稿里的文件清单，请把它们按模块/依赖关系分组，"
        "让不同工程师并行写。每个分组指定一个写码角色（写码/快速/快答 任选其一），"
        "把文件平均分到各组，同一组内文件相关性强。只输出 JSON 数组：\n"
        '[{"writer":"写码","files":["src/a.py","src/b.py"]},'
        '{"writer":"快速","files":["src/c.py"]}]\n\n'
        f"文件清单：{json.dumps(file_paths, ensure_ascii=False)}\n"
        f"设计稿：{json.dumps(design, ensure_ascii=False)}"
    )
    try:
        plan = call_json(ROLES["指挥官"]["model"], "你是总指挥，只输出 JSON。", plan_prompt)
    except Exception:
        return None
    groups = []
    for g in plan:
        if isinstance(g, dict) and g.get("writer") in WRITERS and g.get("files"):
            groups.append({"writer": g["writer"], "files": list(g["files"])})
    return groups or None


def _round_robin_split(file_paths):
    """指挥官分组失败时的降级：把文件轮流分配给写码工人池。"""
    groups = [{"writer": w, "files": []} for w in WRITERS]
    for i, p in enumerate(file_paths):
        groups[i % len(WRITERS)]["files"].append(p)
    return [g for g in groups if g["files"]]


_WRITTEN_LOCK = threading.Lock()
_WRITTEN_FILES = set()


def _written_snapshot():
    with _WRITTEN_LOCK:
        return sorted(_WRITTEN_FILES)


def _mark_written(paths):
    with _WRITTEN_LOCK:
        for p in paths:
            if p:
                _WRITTEN_FILES.add(p)


def _clear_written():
    with _WRITTEN_LOCK:
        _WRITTEN_FILES.clear()


def _parallel_implement(file_paths, design, allow_risky, wd):
    """把设计稿里的文件清单分配给多个写码模型并行写文件。
    返回 (impl_files, impl_summary)。

    并行写手通过一份共享「已写文件清单」互相可见，写之前先确认不与他人冲突
    （针对设计/接口边界，降级为提示性告知，不强制阻塞）。
    """
    groups = _plan_parallel_write(file_paths, design) or _round_robin_split(file_paths)
    print(f"  按 {len(groups)} 组并行写码: " +
          ", ".join(f"{g['writer']}({len(g['files'])}个文件)" for g in groups))
    _clear_written()
    results = []

    def run_group(g):
        _set_workdir(wd)  # 写文件线程独立设置工作目录，避免串目录
        writer = g["writer"]
        paths = g["files"]
        model = ROLES[writer]["model"]
        prompt = (
            f"你是「{writer}」工程师（模型 {model}）。根据下面的设计稿，用 write_file 工具"
            f"把你负责的文件完整写到磁盘上。\n\n"
            f"你负责的文件（只写这些，不要写其他文件）：\n{json.dumps(paths, ensure_ascii=False)}\n\n"
            f"设计稿：{json.dumps(design, ensure_ascii=False)}\n\n"
            f"要求：每个文件用一次 write_file 调用，content 是该文件的完整内容；"
            f"先按需用 list_dir/read_file 了解现状，写完做自我检查。\n"
            f"其他工程师并行负责的文件：{json.dumps([p for p in file_paths if p not in paths], ensure_ascii=False)}"
        )
        txt, trace = agent_loop(
            model,
            f"你是{writer}工程师，用 write_file 工具落地文件，只写分配给你的文件。",
            prompt,
            tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "write_file", "search")],
            temperature=0.2,
            allow_risky=allow_risky,
        )
        written = [a.get("path") for n, a, _ in trace if n == "write_file"]
        _mark_written(written)
        return writer, model, written, txt, trace

    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        futs = [ex.submit(run_group, g) for g in groups]
        for f in as_completed(futs):
            writer, model, written, txt, trace = f.result()
            results.append((writer, model, written, txt))
            for name, args, res in trace:
                if name == "write_file":
                    print(f"    ✎ [{writer}/{model}] {args.get('path')}  → {res}")

    impl_files = []
    for _, _, written, _ in results:
        impl_files.extend(written)
    summary = "\n".join(
        f"[{w}/{m}] 写 {len(written)} 个文件: {', '.join(written) or '无'}"
        for w, m, written, _ in results)
    return impl_files, summary


def team(task, max_rounds=3, state=None, allow_risky=None, workdir=None, max_token_budget=None):
    """真团队流水线：带工具落地文件 + 审查迭代 + 状态持久化。

    新增参数：
      state            — 传入则 resume（按 state['phase'] 续跑，已完成阶段零模型调用）
      allow_risky      — True 时放行命中危险黑名单的 run_command
      max_token_budget — 每日 token 上限（input+output 合计，跨任务跨进程累计）；超限
                         立即停止，未完成阶段标记 budget_exceeded。None = 无上限。
    返回 state dict（既有 CLI 调用不依赖返回值）。
    """
    if workdir:
        _set_workdir(os.path.abspath(workdir))
    wd = _get_workdir()
    allow_risky = bool(allow_risky)
    if state is None:
        state = _new_state(task, wd, allow_risky)
    else:
        task = state["task"]  # resume 模式以 state 内 task 为准
    ar = state["allow_risky"]
    print(f"\n工作目录: {wd}")
    print(f"任务: {task}")
    print(f"task_id: {state['task_id']}  phase: {state['phase']}")

    # 每日预算闸门：判断「今天累计已用 token」是否超过上限（默认无上限）。
    if max_token_budget is None:
        max_token_budget = state.get("max_token_budget")

    def _over_budget():
        if not max_token_budget:
            return False
        return _daily_token_total() > int(max_token_budget)

    def _stop_budget():
        state["budget_exceeded"] = True
        state["status"] = "budget_exceeded"
        _save_state_atomic(wd, state)
        print(f"\n[预算] 今日累计 token 已超上限 {max_token_budget}，停止后续阶段。")
        return state

    design = state.get("design") or {}
    impl_txt = (state.get("impl") or {}).get("summary") or ""

    # 阶段1：分析出设计稿
    if state["phase"] <= 1:
        if _over_budget():
            return _stop_budget()
        print("\n[1/5] 分析 " + ROLES["分析"]["model"] + " 产出设计稿...")
        design_prompt = (
            "你是软件架构师。先查看当前项目结构（用 list_dir/read_file 工具），"
            "然后针对下面的任务输出一份 JSON 设计稿，不要写代码，只做设计。\n"
            f"任务：{task}\n\n"
            "JSON 格式（严格）：\n"
            '{"files":[{"path":"相对路径","purpose":"这个文件做什么"}],'
            '"plan":"实现步骤要点","acceptance":"验收标准(如何证明完成)"}'
        )
        design_txt, _ = agent_loop(
            ROLES["分析"]["model"],
            "你是架构师，先探索项目再用 JSON 输出设计稿。只输出 JSON，不要输出解释。",
            design_prompt,
            tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "search")],
            allow_risky=ar,
        )
        try:
            design = extract_json(design_txt)
        except Exception as e:
            print(f"[警告] 设计稿解析失败({e})，退化为纯文本设计。")
            design = {"files": [], "plan": design_txt, "acceptance": ""}
        print(f"  设计稿: {json.dumps(design, ensure_ascii=False)[:500]}")
        state["design"] = design
        state["phase"] = 2
        _save_state_atomic(wd, state)
    else:
        print("\n[1/5] 设计稿已有(resume)，跳过。")
    if _over_budget():
        return _stop_budget()

    # 阶段2：写码落地（把不同文件并行分给不同写码模型）
    if state["phase"] <= 2:
        if _over_budget():
            return _stop_budget()
        print("\n[2/5] 写码：多模型并行分文件落地...")
        file_paths = [f.get("path") for f in design.get("files", []) if isinstance(f, dict)]
        if file_paths:
            impl_files, impl_txt = _parallel_implement(file_paths, design, ar, wd)
        else:
            # 设计稿没给文件清单：退回单一写码模型自主规划
            impl_prompt = (
                "你是资深工程师。根据下面的设计稿，用 write_file 工具把代码真正写到磁盘上。"
                "先用 list_dir/read_file 了解现状，再逐个写文件。完成后简要说明写了哪些文件、如何验证。\n\n"
                f"设计稿：{json.dumps(design, ensure_ascii=False)}"
            )
            impl_txt, impl_trace = agent_loop(
                ROLES["写码"]["model"],
                "你是写码工程师，用工具把代码落地到工作目录，写完后做自我检查。",
                impl_prompt,
                tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "write_file", "search")],
                temperature=0.2,
                allow_risky=ar,
            )
            impl_files = [args.get("path") for name, args, _ in impl_trace if name == "write_file"]
            for name, args, res in impl_trace:
                if name == "write_file":
                    print(f"    ✎ {args.get('path')}  → {res}")
        print(f"  写码完成，共 {len(impl_files)} 个文件")
        state["impl"] = {"files": impl_files, "summary": impl_txt}
        state["phase"] = 3
        _save_state_atomic(wd, state)
    else:
        print("\n[2/5] 实现已有(resume)，跳过。")

    # 阶段3+4：审查 + 迭代回改
    if state["phase"] <= 3:
        if _over_budget():
            return _stop_budget()
        start_round = len(state.get("reviews") or [])
        for rnd in range(start_round + 1, max_rounds + 1):
            if _over_budget():
                return _stop_budget()
            state["round"] = rnd
            print(f"\n[3/5] 审查 {ROLES['分析']['model']} 读真实代码给意见（第 {rnd} 轮）...")
            review_prompt = (
                "你是代码审查专家。用 read_file/list_dir 工具读取刚才实际写出来的代码，"
                "逐条列出问题。只输出 JSON：\n"
                '{"blocking":[{"file":"...","issue":"..."}],'
                '"minor":[{"file":"...","issue":"..."}],'
                '"verdict":"pass" 或 "fix"}'
                "若无阻塞问题，verdict 为 pass。"
            )
            # 审查解析重试 REVIEW_RETRIES 次；仍失败 → anomalous fix（禁止误判 pass）
            issues = None
            for attempt in range(1, REVIEW_RETRIES + 1):
                review_txt, _ = agent_loop(
                    ROLES["分析"]["model"],
                    "你是代码审查专家，只输出 JSON。",
                    review_prompt,
                    tools=[t for t in TOOLS if t["function"]["name"] in ("list_dir", "read_file", "search")],
                    allow_risky=ar,
                )
                try:
                    issues = extract_json(review_txt)
                    break  # 解析成功
                except Exception as e:
                    if attempt < REVIEW_RETRIES:
                        print(f"  [审查解析失败 第{attempt}次({e})，重试...]")
                    else:
                        print(f"  [警告] 审查意见解析失败({e})，按 anomalous fix 处理（禁止误判 pass）。")
            if issues is None:
                # 解析全部失败 → anomalous fix，绝不判 pass
                issues = {"blocking": [], "minor": [], "verdict": "fix", "anomalous": True}
            verdict = issues.get("verdict", "fix")
            blocking = issues.get("blocking", [])
            minor = issues.get("minor", [])
            print(f"  结论: {verdict}  (阻塞 {len(blocking)} 条 / 次要 {len(minor)} 条)")
            for it in blocking + minor:
                if isinstance(it, dict):
                    print(f"    · [{it.get('file', '?')}] {str(it.get('issue', ''))[:80]}")
            state["reviews"].append({
                "round": rnd, "verdict": verdict,
                "blocking": blocking, "minor": minor,
                "anomalous": bool(issues.get("anomalous")),
            })
            _save_state_atomic(wd, state)
            if verdict == "pass" or not blocking:
                break
            if rnd < max_rounds:
                if _over_budget():
                    return _stop_budget()
                print(f"\n[4/5] 回改 {ROLES['写码']['model']} 按审查意见修复...")
                fix_prompt = (
                    "根据下面的审查意见，用 read_file 读代码、write_file 修复问题。"
                    "只修阻塞问题，次要问题一并处理。完成后说明改了什么。\n\n"
                    f"审查意见：{json.dumps(issues, ensure_ascii=False)}"
                )
                _, fix_trace = agent_loop(
                    ROLES["写码"]["model"],
                    "你是工程师，按审查意见修复代码。",
                    fix_prompt,
                    tools=[t for t in TOOLS if t["function"]["name"] in ("read_file", "write_file", "list_dir", "search")],
                    temperature=0.2,
                    allow_risky=ar,
                )
                for name, args, res in fix_trace:
                    if name == "write_file":
                        print(f"    ✎ {args.get('path')}  → {res}")
                state["fixes"].append({
                    "round": rnd,
                    "files": [args.get("path") for name, args, _ in fix_trace if name == "write_file"],
                })
                _save_state_atomic(wd, state)
        state["phase"] = 5
        _save_state_atomic(wd, state)
    else:
        print("\n[3-4/5] 审查迭代已有(resume)，跳过。")
    if _over_budget():
        return _stop_budget()

    # 阶段5：汇总（用交接摘要替代 [:800]/[:1200]/[:600] 粗暴截断）
    if _over_budget():
        return _stop_budget()
    print("\n[5/5] 汇总 " + ROLES["指挥官"]["model"] + " 输出最终交付...")
    files_done = ", ".join(
        f.get("path", "") for f in design.get("files", []) if isinstance(f, dict))
    final_prompt = (
        "你是总指挥。团队已完成以下任务，请汇总最终交付：完成内容、文件清单、"
        "验证方法、剩余风险。简洁，直接输出，不要输出思考过程。\n\n"
        f"任务：{task}\n{_handoff_design(state)}\n\n{_handoff_impl(state)}\n\n"
        f"{_handoff_review(state, state.get('round', 0))}"
    )
    try:
        final = ask(ROLES["指挥官"]["model"], final_prompt,
                    system="你是总指挥，直接输出最终交付，不要输出思考过程。")
        if not final or final.startswith("The user") or "thinking" in final[:60].lower():
            final = (f"任务已完成。涉及文件：{files_done or '见工作目录'}\n\n"
                     f"写码说明：\n{impl_txt[:1500]}")
    except Exception as e:
        final = f"[汇总失败] {e}\n\n写码说明：\n{impl_txt[:1500]}"
    # 汇总末尾追加结构化异常标记
    anomalous_rounds = [r["round"] for r in state.get("reviews", []) if r.get("anomalous")]
    if anomalous_rounds:
        final += "\n\n[结构化异常] 第 " + ",".join(str(r) for r in anomalous_rounds) + " 轮审查未完成"
    state["final"] = {"summary": final}
    state["status"] = "done"
    _save_state_atomic(wd, state)
    print("\n" + "=" * 60)
    print(final)
    print("=" * 60)
    return state


def list_models():
    """列出 GATEWAYS 里硬编码注册的模型（不触网，避免 BASE_URL 旧引用）。"""
    print("网关可用模型（硬编码注册表）:")
    for gw in GATEWAYS:
        print(f"  [{gw['name']}] {gw['base_url']}")
        for alias, real in sorted(gw["models"].items()):
            tag = "" if alias == real else f"  (→ 上游 {real})"
            print(f"    - {alias}{tag}")


def menu():
    while True:
        print("\n" + "=" * 56)
        print("  多模型协作编排器 (multi-model)")
        print("=" * 56)
        gw_line = f"  主网关: {GATEWAYS[0]['base_url']}"
        if len(GATEWAYS) > 1:
            gw_line += f"  备选: {GATEWAYS[1]['base_url']}"
        print(gw_line)
        print("  1. 多模型团队模式（推荐）")
        print("  2. 单模型模式")
        print("  0. 退出")
        print("=" * 56)
        try:
            s = input("  请选择 [1/2/0，回车=1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n退出")
            return
        if s == "" or s == "1":
            _menu_team()
        elif s == "2":
            _menu_ask()
        elif s == "0":
            print("再见")
            return
        else:
            print("  无效选择，请重试")


def _menu_team():
    """多模型团队模式：输入任务，交给多模型并行写文件 + 审查迭代。"""
    print("\n  [多模型团队模式] 输入任务，让多个模型分工写代码并审查迭代。")
    print("  输入 0 返回主菜单。")
    try:
        task = input("  任务: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if task == "0":
        return
    if not task:
        print("  任务为空，返回主菜单")
        return
    try:
        team(task)
    except KeyboardInterrupt:
        print("\n  已取消")
    except Exception as e:
        print(f"\n  出错: {e}")


def _menu_ask():
    """单模型模式：选模型问答。按数字选模型，回车默认第一个，0 返回主菜单。"""
    model_names = list(ROLES.keys())
    while True:
        print("\n  [单模型模式] 选择一个模型:")
        for i, name in enumerate(model_names, 1):
            m = ROLES[name]["model"]
            print(f"    {i}. {name} ({m})")
        print("    0. 返回主菜单")
        try:
            s = input("  选择模型 [1-%d，回车=%s，0=返回]: " % (len(model_names), model_names[0])).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if s == "0":
            return
        if s == "":
            chosen = model_names[0]
        elif s.isdigit() and 1 <= int(s) <= len(model_names):
            chosen = model_names[int(s) - 1]
        else:
            print("  无效选择，请重试")
            continue
        model = ROLES[chosen]["model"]
        try:
            q = input(f"  问题({chosen}/{model}): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if q == "0":
            return
        if not q:
            print("  问题为空")
            continue
        try:
            print("\n" + ask(model, q))
        except Exception as e:
            print(f"\n  出错: {e}")


def main():
    global WORKDIR, ALLOW_RUN
    ap = argparse.ArgumentParser(description="多模型协作编排器")
    ap.add_argument("--api-key", default=None, help="覆盖 API Key")
    ap.add_argument("--workdir", default=None, help="工作目录（团队读写文件限定在此）")
    ap.add_argument("--no-run", action="store_true", help="禁用 run_command 工具")
    ap.add_argument("--allow-risky", action="store_true", default=True,
                    help="放行命中危险命令黑名单的 run_command（默认全通过；用 --no-allow-risky 拦截）")
    ap.add_argument("--no-allow-risky", action="store_false", dest="allow_risky",
                    help="拦截命中危险命令黑名单的 run_command")
    sub = ap.add_subparsers(dest="action")

    p = sub.add_parser("ask", help="单模型问答")
    p.add_argument("model")
    p.add_argument("prompt")

    p = sub.add_parser("team", help="多模型团队流水线(并行写文件+审查迭代)")
    p.add_argument("task", nargs="?", default=None, help="任务文本（与 --resume 互斥）")
    p.add_argument("--rounds", type=int, default=3, help="审查迭代最大轮数")
    p.add_argument("--resume", default=None, metavar="TASK_ID",
                   help="从已持久化的状态续跑（与 task 位置参数互斥）")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="每日 token 上限（input+output 合计，跨任务累计），默认无上限")

    sub.add_parser("list", help="列出可用模型")

    args = ap.parse_args()

    if args.api_key:
        os.environ["CF_GATEWAY_KEY"] = args.api_key
    if args.workdir:
        WORKDIR = os.path.abspath(args.workdir)
    if args.no_run:
        ALLOW_RUN = False

    if not args.action:
        menu()
        return

    if args.action == "ask":
        print(ask(ALIASES.get(args.model, args.model), args.prompt))
    elif args.action == "team":
        if args.resume:
            try:
                st = _load_state(WORKDIR, args.resume)
            except StateError as e:
                print(f"[错误] 恢复状态失败: {e}")
                sys.exit(2)
            team(task=st["task"], max_rounds=args.rounds, state=st,
                 allow_risky=st.get("allow_risky", False))
        else:
            if not args.task:
                print("[错误] team 需要任务文本，或用 --resume TASK_ID 续跑")
                sys.exit(2)
            team(args.task, max_rounds=args.rounds, allow_risky=args.allow_risky,
                 max_token_budget=args.max_tokens)
    elif args.action == "list":
        list_models()


if __name__ == "__main__":
    main()
