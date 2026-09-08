"""multi-model MCP stdio server.

Phase D (T1+T2) 实现：
- §0 启动与 stdout 隔离（Windows UTF-8 + 协议帧专用通道）
- §1 JSON-RPC 2.0 协议层
- §2 TOOL_SPECS（8 个 MCP 工具定义）
- §4 Job 注册表（线程化异步任务）
- §5 dispatch_raw 分发层骨架（tools/call 留 T7）
- §6 main() 入口与 --self-check

红线：
- 零 pip 依赖，仅用 Python 标准库
- Windows 下 reconfigure stdin/stdout/stderr 为 UTF-8
- 不动 proxy.py
- 敏感信息不日志
"""

import sys
import os
import json
import time
import argparse
import threading
import secrets
import functools
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List


# =============================================================================
# §0 启动与 stdout 隔离
# =============================================================================

def _setup_utf8():
    """Windows 下 reconfigure stdin/stdout/stderr 为 UTF-8。

    errors="replace" 保证不会因为个别非法字节抛 UnicodeDecodeError 而崩溃。
    非 Windows 平台直接 no-op（POSIX 默认即 UTF-8 友好）。
    """
    if sys.platform == "win32":
        for s in (sys.stdin, sys.stdout, sys.stderr):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                # 极少数情况下 stream 不支持 reconfigure（已被包装），忽略
                pass


# 保存原始 stdout，专用于写协议帧（绝不被替换）
# 在 main() 中赋值为 sys.stdout 的原始句柄
_PROTO_OUT = None  # type: Optional[Any]

# multi-model 模块延迟加载缓存（首次 tools/call 时加载，避免无谓启动开销）
_MM_CACHE = None  # type: Optional[Any]


class _StderrProxy:
    """所有 write/flush 转发到 stderr。

    用途：main() 把进程级 sys.stdout 替换成 _StderrProxy(sys.stderr)，
    这样后台线程里不慎 print() 也不会污染协议帧通道（_PROTO_OUT）。
    协议帧始终通过 write_frame() 写到 _PROTO_OUT。
    """

    def __init__(self, stderr):
        self._err = stderr

    def write(self, s):
        return self._err.write(s)

    def flush(self):
        self._err.flush()

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False

    def isatty(self):
        return False


# =============================================================================
# §1 协议层（JSON-RPC 2.0）
# =============================================================================

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def parse_message(raw):
    """解析一行 JSON-RPC。

    返回 (is_request, id, method, params)。
    - 通知（无 id）→ (False, None, method, params)
    - 请求（有 id，可为 int/str）→ (True, id, method, params)

    解析失败抛异常（由调用方捕获转 PARSE_ERROR）。
    params 缺省时返回空 dict，便于后续统一 .get()。
    """
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("JSON-RPC 顶层必须是对象")
    method = obj.get("method")
    params = obj.get("params") or {}
    if not isinstance(params, dict):
        # JSON-RPC 允许 params 为数组，本服务统一用 dict；非 dict 视为非法
        raise ValueError("params 必须是对象")
    msg_id = obj.get("id")  # None = notification
    is_request = msg_id is not None
    return (is_request, msg_id, method, params)


def resp_ok(msg_id, result):
    """构造成功响应。"""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def resp_err(msg_id, code, message, data=None):
    """构造错误响应。data 可选，用于附加诊断信息（注意不要塞敏感信息）。"""
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def write_frame(obj):
    """写协议帧到 _PROTO_OUT（原始 stdout），不经过被替换的 sys.stdout。

    ensure_ascii=False：中文原样输出，配合 UTF-8 reconfigure。
    每帧一行（newline-delimited JSON）。
    """
    if _PROTO_OUT is None:
        # 理论上不会走到这里，main() 会先赋值；防御性兜底
        sys.stderr.write("[write_frame] _PROTO_OUT not initialized\n")
        return
    _PROTO_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _PROTO_OUT.flush()


# =============================================================================
# §2 工具定义 TOOL_SPECS
# =============================================================================

TOOL_SPECS = [
    {
        "name": "multi_ask",
        "description": "用指定模型问答一次。model 支持假名或别名：astra(旗舰推理)/sol(深度分析)/luna(写码主力)/sol-fast/luna-fast 或 glm/deep/kimi 等真名别名。适合单问题快速获取某模型的回答；团队协作类任务请用 multi_team_start。",
        "inputSchema": {"type": "object", "properties": {
            "model": {"type": "string", "description": "模型名或别名"},
            "prompt": {"type": "string", "description": "问题"},
            "system": {"type": "string", "description": "可选系统提示"}
        }, "required": ["model", "prompt"]}
    },
    {
        "name": "multi_list_models",
        "description": "列出当前网关可用的全部模型 ID。用于在发起任务前确认 gpt-* 假名/真名是否可用。",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "multi_team_start",
        "description": "启动五阶段多模型团队流水线（分析设计→写码落地→审查→迭代回改→汇总），真实写文件到 workdir。立即返回 task_id，用 multi_task_status 轮询进度、multi_task_result 取最终交付。适合『实现/修改代码并落盘』的工程任务。",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string", "description": "任务描述，写清目标文件与验收标准"},
            "workdir": {"type": "string", "description": "工作目录，默认当前"},
            "rounds": {"type": "integer", "minimum": 1, "maximum": 5, "description": "审查迭代上限，默认 3"}
        }, "required": ["task"]}
    },
    {
        "name": "multi_orchestrate_start",
        "description": "指挥官模型拆解任务为 3~5 个子任务 → 多角色模型并行执行 → 指挥官汇总。不写文件（纯文本交付）。立即返回 task_id，用 status/result 查询。",
        "inputSchema": {"type": "object", "properties": {
            "task": {"type": "string"},
            "workdir": {"type": "string"}
        }, "required": ["task"]}
    },
    {
        "name": "multi_parallel_start",
        "description": "同一问题并行发给全部角色模型，对比回答差异。立即返回 task_id。",
        "inputSchema": {"type": "object", "properties": {
            "prompt": {"type": "string"},
            "models": {"type": "array", "items": {"type": "string"}, "description": "可选，默认五角色"}
        }, "required": ["prompt"]}
    },
    {
        "name": "multi_task_status",
        "description": "查询 start 类任务进度：task_type、当前阶段（team 为 1~5 对应 设计/写码/审查/回改/汇总）、已完成文件清单、阶段性摘要。完成后状态变 done，可调 multi_task_result。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}
        }, "required": ["task_id"]}
    },
    {
        "name": "multi_task_result",
        "description": "取 start 类任务的最终结果：team 返回最终交付+文件清单+审查结论+错误；parallel 返回各模型回答；orchestrate 返回汇总报告。任务未完成时返回提示。",
        "inputSchema": {"type": "object", "properties": {
            "task_id": {"type": "string"}
        }, "required": ["task_id"]}
    },
    {
        "name": "spawn_agent",
        "description": "子代理委派：把一段子任务交给另一个模型独立完成，返回该模型的完整回答。Codex 主模型可多次调用本工具，把不同子任务派给不同子模型（如把深度分析交给 sol、把写码交给 luna、把文案交给 luna-fast），再把结果汇总。agent 支持角色名(指挥官/分析/写码/快速/快答)或模型别名(astra/sol/luna/sol-fast/luna-fast)。",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string", "description": "子代理名：指挥官/分析/写码/快速/快答 或 astra/sol/luna/sol-fast/luna-fast"},
            "task": {"type": "string", "description": "委派给子代理的子任务描述，写清输入、期望输出格式"},
            "context": {"type": "string", "description": "可选：给子代理的背景材料（如现有代码、错误日志、已有结论）"}
        }, "required": ["agent", "task"]}
    },
    {
        "name": "spawn_agent_async",
        "description": "子代理委派（异步，长任务用）：立即返回 task_id，用 multi_task_status 轮询、multi_task_result 取子代理完整回答。适合超过 55 秒的委派任务；短任务用 spawn_agent。",
        "inputSchema": {"type": "object", "properties": {
            "agent": {"type": "string", "description": "子代理名：指挥官/分析/写码/快速/快答 或 astra/sol/luna/sol-fast/luna-fast"},
            "task": {"type": "string", "description": "委派给子代理的子任务描述"},
            "context": {"type": "string", "description": "可选：给子代理的背景材料"}
        }, "required": ["agent", "task"]}
    },
    {
        "name": "multi_ping",
        "description": "探活：确认 multi-model MCP server 与网关配置可用，返回网关地址与角色清单。排障首选。",
        "inputSchema": {"type": "object", "properties": {}}
    },
]

# 工具名 → spec 的快速索引（T7 分发用）
TOOL_NAME_INDEX: Dict[str, dict] = {t["name"]: t for t in TOOL_SPECS}


# =============================================================================
# §4 Job 注册表
# =============================================================================

@dataclass
class Job:
    """异步任务句柄。

    - thread: 后台执行线程（team/orchestrate/parallel 启动后立即返回 task_id）
    - result_cache: 线程结束前写入最终结果，multi_task_result 直接读
    - finished: 线程是否结束（线程自己在收尾时置 True，加锁）
    """
    task_id: str
    task_type: str  # "team" | "orchestrate" | "parallel"
    thread: Optional[threading.Thread] = None
    workdir: str = ""
    started_at: str = ""
    finished: bool = False
    result_cache: Optional[Any] = None  # 线程结束前写入


JOBS: Dict[str, Job] = {}
JOBS_MAX = 32
_JOBS_LOCK = threading.Lock()


def _gen_task_id(task_type):
    """生成 {type}-{yyyymmdd}-{HHMMSS}-{4位hex}。

    4 位 hex = secrets.token_hex(2) → 2 字节 = 4 个十六进制字符。
    同秒内冲突概率 ≈ 1/65536，足够单机使用。
    """
    return f"{task_type}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _register_job(task_id, task_type, workdir):
    """注册新 Job。

    超限逐出最旧 finished；全部 running → 拒绝（返回 False）。
    成功注册返回 True。

    线程安全：调用方通常在主线程注册，但加锁以防后续扩展。
    """
    with _JOBS_LOCK:
        if len(JOBS) >= JOBS_MAX:
            # 逐出最旧 finished
            finished = [(tid, j) for tid, j in JOBS.items() if j.finished]
            if not finished:
                return False  # 全部运行中，拒绝新任务
            finished.sort(key=lambda x: x[1].started_at)
            del JOBS[finished[0][0]]
        job = Job(
            task_id=task_id,
            task_type=task_type,
            workdir=workdir,
            started_at=datetime.now().isoformat(timespec="seconds"),
        )
        JOBS[task_id] = job
        return True


def _get_job(task_id) -> Optional[Job]:
    """线程安全读取 Job。"""
    with _JOBS_LOCK:
        return JOBS.get(task_id)


# =============================================================================
# §4.5 工具实现层（T7）
# =============================================================================

# 同步工具执行预算（秒）。超过则建议改用两段式（start + status 轮询）。
# 默认 55s：略低于 MCP 客户端常见的 60s 超时，留 5s 余量给协议往返。
SYNC_BUDGET_SEC = int(os.environ.get("MM_SYNC_BUDGET_SEC", "55"))


def _load_mm():
    """动态加载同目录 multi-model.py，返回模块对象。

    失败 → 打印错误到 stderr，返回 None（调用方决定如何处理）。
    用 importlib 而非直接 import，因为文件名含连字符。
    """
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    mm_path = os.path.join(here, "multi-model.py")
    if not os.path.isfile(mm_path):
        print(f"[mcp_server] 无法找到 multi-model.py: {mm_path}", file=sys.stderr)
        return None
    try:
        spec = importlib.util.spec_from_file_location("multi_model", mm_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"[mcp_server] 加载 multi-model.py 失败: {e}", file=sys.stderr)
        return None


# ---- 同步工具 ----

def _tool_spawn_agent(mm, params):
    """子代理委派：把子任务交给指定子模型，走自定义 API（与 multi_ask 同链路）。"""
    agent = (params.get("agent") or "").strip()
    task = (params.get("task") or "").strip()
    if not agent or not task:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 agent 或 task"}]}

    # 角色名 → 模型，别名 → 模型
    model = None
    if agent in mm.ROLES:
        model = mm.ROLES[agent]["model"]
    else:
        model = mm.ALIASES.get(agent)
        if model is None and agent in {r["model"] for r in mm.ROLES.values()}:
            model = agent
    if model is None:
        known = " / ".join(list(mm.ROLES.keys()) + list(mm.ALIASES.keys()))
        return {"isError": True, "content": [{"type": "text", "text": f"未知子代理 {agent}，可用: {known}"}]}

    context = (params.get("context") or "").strip()
    prompt = f"子任务：{task}"
    if context:
        prompt += f"\n\n背景材料：\n{context}"
    system = f"你是被委派的子代理（模型 {model}）。只完成分配给你的子任务，直接给出结果，不要复述任务。"
    t0 = time.time()
    try:
        answer = mm.ask(model, prompt, system=system)
        elapsed = time.time() - t0
        if elapsed > SYNC_BUDGET_SEC:
            return {"isError": True, "content": [{"type": "text", "text": f"子代理执行超出预算({elapsed:.0f}s)，建议拆分任务或调大 MM_SYNC_BUDGET_SEC"}]}
        return {"content": [{"type": "text", "text": f"[子代理 {agent} → {model}]\n{answer}"}]}
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "Connection" in err_msg or "URLError" in err_msg:
            err_msg += "\n排查：①先运行 python allin.py 完成 SSO 认证；②检查 deploy_ai_cli.py 的 DEFAULT_BASE_URL；③确认 proxy.py 在运行"
        return {"isError": True, "content": [{"type": "text", "text": f"子代理调用失败: {err_msg}"}]}


def _tool_spawn_agent_async(mm, params):
    """异步子代理委派：立即返回 task_id。结果由 multi_task_result 提供。"""
    agent = (params.get("agent") or "").strip()
    task = (params.get("task") or "").strip()
    if not agent or not task:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 agent 或 task"}]}

    context = (params.get("context") or "").strip()
    workdir = mm.WORKDIR

    def _run_spawn():
        model, answer = mm.spawn_agent(agent, task, context=context)
        return {"agent": agent, "model": model, "answer": answer}

    ok, info = _spawn_job("spawn_agent", workdir, _run_spawn, ())
    if not ok:
        return {"isError": True, "content": [{"type": "text", "text": info}]}
    return {"content": [{"type": "text", "text": json.dumps({"task_id": info, "status": "queued", "task_type": "spawn_agent"}, ensure_ascii=False)}]}


def _tool_multi_ask(mm, params):
    """同步问答。超预算 → 返回 error dict（isError=True）。"""
    model = params.get("model")
    prompt = params.get("prompt")
    system = params.get("system")
    if not model or not prompt:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 model 或 prompt"}]}
    model = mm.ALIASES.get(model, model)  # 别名解析
    t0 = time.time()
    try:
        answer = mm.ask(model, prompt, system=system)
        elapsed = time.time() - t0
        if elapsed > SYNC_BUDGET_SEC:
            return {"isError": True, "content": [{"type": "text", "text": f"执行超出预算({elapsed:.0f}s > {SYNC_BUDGET_SEC}s)，建议改用 multi_team_start 两段式或调大 MM_SYNC_BUDGET_SEC"}]}
        return {"content": [{"type": "text", "text": answer}]}
    except Exception as e:
        err_msg = str(e)
        if "401" in err_msg or "Connection" in err_msg or "URLError" in err_msg:
            err_msg += "\n排查：①先运行 python allin.py 完成 SSO 认证；②检查 deploy_ai_cli.py 的 DEFAULT_BASE_URL；③确认 proxy.py 在运行"
        return {"isError": True, "content": [{"type": "text", "text": f"模型网关连接失败: {err_msg}"}]}


def _tool_multi_list_models(mm, params):
    """列出可用模型。

    注意：multi-model.py 的 list_models 直接 print 到 stdout，而本进程 stdout 已被
    替换为 _StderrProxy（→ stderr），故此处直接从 ROLES 构造文本，不调用 list_models。
    """
    try:
        lines = []
        for gw in mm.GATEWAYS:
            lines.append(f"[{gw['name']}] {gw['base_url']}")
            for alias, real in sorted(gw["models"].items()):
                tag = "" if alias == real else f"  (→ 上游 {real})"
                lines.append(f"  - {alias}{tag}")
        return {"content": [{"type": "text", "text": "可用模型（网关注册表）:\n" + "\n".join(lines)}]}
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"列出模型失败: {e}"}]}


def _tool_multi_ping(mm, params):
    """探活。返回网关地址与角色清单。"""
    try:
        roles = {name: info["model"] for name, info in mm.ROLES.items()}
        gateways = ", ".join(f"{g['name']}={g['base_url']}" for g in mm.GATEWAYS)
        return {"content": [{"type": "text", "text": f"pong\n网关: {gateways}\n角色: {json.dumps(roles, ensure_ascii=False)}"}]}
    except Exception as e:
        return {"isError": True, "content": [{"type": "text", "text": f"ping 失败: {e}"}]}


# ---- 异步 start 工具 ----

def _spawn_job(task_type, workdir, target, args_tuple):
    """启动后台线程执行 team/orchestrate/parallel。

    target: 可调用对象（mm.team / mm.orchestrate / mm.parallel 或 functools.partial 包装）
    args_tuple: 传给 target 的位置参数元组
    返回 (ok, task_id_or_error_msg)
    """
    task_id = _gen_task_id(task_type)
    if not _register_job(task_id, task_type, workdir):
        return (False, "任务数达上限（32），请等待现有任务完成或重启 server")

    job = _get_job(task_id)

    def _run():
        """后台线程执行体。

        线程内 sys.stdout 已被进程级替换为 _StderrProxy，print 进 stderr，
        不会污染协议帧通道（_PROTO_OUT）。
        """
        try:
            result = target(*args_tuple)
            with _JOBS_LOCK:
                j = JOBS.get(task_id)
                if j:
                    j.result_cache = result
                    j.finished = True
        except Exception as e:
            with _JOBS_LOCK:
                j = JOBS.get(task_id)
                if j:
                    j.result_cache = {"error": str(e)}
                    j.finished = True
            print(f"[job {task_id}] 异常: {e}", file=sys.stderr)

    thread = threading.Thread(target=_run, daemon=True, name=f"job-{task_id}")
    with _JOBS_LOCK:
        j = JOBS.get(task_id)
        if j:
            j.thread = thread
    thread.start()
    return (True, task_id)


def _tool_multi_team_start(mm, params):
    """启动五阶段团队流水线（异步）。

    mm.team 的新签名：team(task, max_rounds=3, state=None, allow_risky=None, workdir=None)
    用 functools.partial 绑定 kwargs，再交给 _spawn_job 在后台线程调用。
    """
    task = params.get("task")
    if not task:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 task"}]}
    workdir = params.get("workdir") or mm.WORKDIR
    rounds = params.get("rounds", 3)
    # 安全红线：MCP 不暴露 allow_risky，危险命令黑名单始终硬生效
    allow_risky = False

    target = functools.partial(mm.team, task, max_rounds=rounds, allow_risky=allow_risky, workdir=workdir)
    ok, info = _spawn_job("team", workdir, target, ())
    if not ok:
        return {"isError": True, "content": [{"type": "text", "text": info}]}
    return {"content": [{"type": "text", "text": json.dumps({"task_id": info, "status": "queued", "task_type": "team"}, ensure_ascii=False)}]}


def _tool_multi_orchestrate_start(mm, params):
    """启动指挥官拆解+并行执行+汇总（异步）。"""
    task = params.get("task")
    if not task:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 task"}]}
    workdir = params.get("workdir") or mm.WORKDIR
    ok, info = _spawn_job("orchestrate", workdir, mm.orchestrate, (task,))
    if not ok:
        return {"isError": True, "content": [{"type": "text", "text": info}]}
    return {"content": [{"type": "text", "text": json.dumps({"task_id": info, "status": "queued", "task_type": "orchestrate"}, ensure_ascii=False)}]}


def _tool_multi_parallel_start(mm, params):
    """同一问题并行发给多模型对比（异步）。"""
    prompt = params.get("prompt")
    if not prompt:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 prompt"}]}
    models = params.get("models")
    workdir = mm.WORKDIR
    ok, info = _spawn_job("parallel", workdir, mm.parallel, (prompt, models))
    if not ok:
        return {"isError": True, "content": [{"type": "text", "text": info}]}
    return {"content": [{"type": "text", "text": json.dumps({"task_id": info, "status": "queued", "task_type": "parallel"}, ensure_ascii=False)}]}


# ---- 查询工具 ----

def _tool_multi_task_status(mm, params):
    """查询任务进度。

    team 类型：尝试读 .multi-model 状态文件，返回阶段/轮次/已写文件等详细进度。
    orchestrate/parallel 或状态文件不可读：返回基本 alive/finished 状态。
    """
    task_id = params.get("task_id")
    if not task_id:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 task_id"}]}
    job = _get_job(task_id)
    if job is None:
        with _JOBS_LOCK:
            known = list(JOBS.keys())
        return {"isError": True, "content": [{"type": "text", "text": f"任务句柄无效或已过期。当前可查询: {known}。若任务在 server 重启前启动，可用 `python multi-model.py team --resume {task_id}` 续跑"}]}

    # team 类型：尝试读状态文件获取详细进度
    if job.task_type == "team":
        try:
            state = mm._load_state(job.workdir, task_id)
            phase_names = {1: "设计", 2: "写码", 3: "审查", 5: "汇总"}
            status_info = {
                "task_id": task_id,
                "task_type": job.task_type,
                "status": state.get("status", "running"),
                "phase": state.get("phase", 0),
                "phase_name": phase_names.get(state.get("phase", 0), "?"),
                "round": state.get("round", 0),
                "files_written": (state.get("impl") or {}).get("files", []),
            }
            return {"content": [{"type": "text", "text": json.dumps(status_info, ensure_ascii=False)}]}
        except Exception:
            # 状态文件还没写（任务刚启动）或损坏 → 落到基本状态
            pass

    # orchestrate/parallel 或 team 状态文件不可读：返回基本状态
    alive = job.thread is not None and job.thread.is_alive()
    status = "running" if alive else ("done" if job.finished else "unknown")
    return {"content": [{"type": "text", "text": json.dumps({"task_id": task_id, "task_type": job.task_type, "status": status}, ensure_ascii=False)}]}


def _tool_multi_task_result(mm, params):
    """取任务最终结果。

    team 返回 state dict；parallel 返回 dict{model: answer}；orchestrate 返回文本。
    未完成 → 提示继续轮询 status。
    """
    task_id = params.get("task_id")
    if not task_id:
        return {"isError": True, "content": [{"type": "text", "text": "缺少必填参数 task_id"}]}
    job = _get_job(task_id)
    if job is None:
        with _JOBS_LOCK:
            known = list(JOBS.keys())
        return {"isError": True, "content": [{"type": "text", "text": f"任务句柄无效或已过期。当前可查询: {known}。若任务在 server 重启前启动，可用 `python multi-model.py team --resume {task_id}` 续跑"}]}

    alive = job.thread is not None and job.thread.is_alive()
    if alive:
        return {"content": [{"type": "text", "text": f"任务 {task_id} 尚未完成，请继续轮询 multi_task_status"}]}

    if not job.finished:
        return {"content": [{"type": "text", "text": f"任务 {task_id} 状态未知（可能异常终止）"}]}

    result = job.result_cache
    if result is None:
        return {"content": [{"type": "text", "text": f"任务 {task_id} 已完成但无结果缓存"}]}

    # team 返回 state dict；parallel 返回 dict{model: answer}；orchestrate 返回文本
    if isinstance(result, dict):
        text = json.dumps(result, ensure_ascii=False, default=str)
    else:
        text = str(result)
    return {"content": [{"type": "text", "text": text}]}


# =============================================================================
# §5 分发层骨架（本阶段只写骨架，工具实现留 T7）
# =============================================================================

def dispatch_raw(line, mm_module=None):
    """分发一行 JSON-RPC。

    返回响应 dict 或 None（通知无响应）。
    mm_module: multi-model 模块对象（T7 实现 _load_mm 后传入）。

    本阶段实现：
    - initialize
    - notifications/initialized（通知，无响应）
    - ping
    - tools/list
    - tools/call → 占位错误（T7 实现）

    畸形帧（非 JSON / 顶层非对象）→ PARSE_ERROR -32700，尽量回带 id。
    """
    try:
        is_req, msg_id, method, params = parse_message(line)
    except Exception:
        # 畸形帧：尽量抢救出 id 用于错误响应
        try:
            obj = json.loads(line)
            msg_id = obj.get("id") if isinstance(obj, dict) else None
        except Exception:
            msg_id = None
        return resp_err(msg_id, PARSE_ERROR, "Parse error")

    # method 缺失
    if method is None:
        return resp_err(msg_id, INVALID_REQUEST, "Missing method")

    # --- 协议握手 ---
    if method == "initialize":
        return resp_ok(msg_id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "multi-model-mcp", "version": "1.0.0"},
            "capabilities": {"tools": {}}
        })
    if method == "notifications/initialized":
        return None  # 通知无响应
    if method == "ping":
        return resp_ok(msg_id, {})

    # --- 工具协议 ---
    if method == "tools/list":
        return resp_ok(msg_id, {"tools": TOOL_SPECS})
    if method == "tools/call":
        tool_name = params.get("name")
        tool_args = params.get("arguments") or {}
        if not tool_name:
            return resp_err(msg_id, INVALID_PARAMS, "Missing required: name")
        if tool_name not in TOOL_NAME_INDEX:
            return resp_err(msg_id, INVALID_PARAMS, f"Unknown tool: {tool_name}")

        # 加载 multi-model 模块（延迟加载，首次调用时加载）
        global _MM_CACHE
        if _MM_CACHE is None:
            _MM_CACHE = _load_mm()
        if _MM_CACHE is None:
            return resp_err(msg_id, INTERNAL_ERROR, "无法加载 multi-model.py（同目录文件缺失或语法错误）")

        # 路由到具体工具
        tool_handlers = {
            "spawn_agent": _tool_spawn_agent,
            "spawn_agent_async": _tool_spawn_agent_async,
            "multi_ask": _tool_multi_ask,
            "multi_list_models": _tool_multi_list_models,
            "multi_ping": _tool_multi_ping,
            "multi_team_start": _tool_multi_team_start,
            "multi_orchestrate_start": _tool_multi_orchestrate_start,
            "multi_parallel_start": _tool_multi_parallel_start,
            "multi_task_status": _tool_multi_task_status,
            "multi_task_result": _tool_multi_task_result,
        }
        handler = tool_handlers.get(tool_name)
        if handler is None:
            return resp_err(msg_id, INTERNAL_ERROR, f"Tool handler not found: {tool_name}")

        try:
            result = handler(_MM_CACHE, tool_args)
            # result 是 {"content": [...]} 或 {"isError": True, "content": [...]}
            return resp_ok(msg_id, result)
        except Exception as e:
            return resp_err(msg_id, INTERNAL_ERROR, f"Tool execution failed: {e}")

    # --- 未知方法 ---
    return resp_err(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")


# =============================================================================
# §6 main() 入口
# =============================================================================

def _run_self_check() -> int:
    """部署自检：内部跑 initialize + tools/list，打印工具清单到 stderr 后退出。

    返回退出码（0=成功，1=失败）。
    自检输出走 stderr，不污染 stdout（虽然自检场景下 stdout 也无客户端在听）。
    """
    init_resp = dispatch_raw('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}')
    list_resp = dispatch_raw('{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
    if (
        init_resp
        and "result" in init_resp
        and list_resp
        and "result" in list_resp
    ):
        tools = list_resp["result"]["tools"]
        print(f"[self-check] OK: {len(tools)} tools", file=sys.stderr)
        for t in tools:
            desc = t.get("description", "")
            print(f"  - {t['name']}: {desc[:60]}", file=sys.stderr)
        return 0
    else:
        print("[self-check] FAILED", file=sys.stderr)
        return 1


def main():
    """stdio MCP server 主入口。"""
    global _PROTO_OUT

    # 1) Windows UTF-8
    _setup_utf8()

    # 2) 锁定协议帧专用通道（原始 stdout），再替换进程级 sys.stdout
    _PROTO_OUT = sys.stdout
    sys.stdout = _StderrProxy(sys.stderr)  # 进程级替换：后台线程 print 不污染协议帧

    # 3) 参数解析
    ap = argparse.ArgumentParser(description="multi-model MCP stdio server")
    ap.add_argument("--self-check", action="store_true", help="部署自检")
    args = ap.parse_args()

    if args.self_check:
        sys.exit(_run_self_check())

    # 4) 主循环：逐行读 stdin，分发，写帧到 _PROTO_OUT
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = dispatch_raw(line)
            if resp is not None:
                write_frame(resp)
        except Exception as e:
            # catch-all → -32603，不退出（保证单帧异常不杀掉 server）
            try:
                obj = json.loads(line)
                msg_id = obj.get("id") if isinstance(obj, dict) else None
            except Exception:
                msg_id = None
            write_frame(resp_err(msg_id, INTERNAL_ERROR, str(e)))


if __name__ == "__main__":
    main()