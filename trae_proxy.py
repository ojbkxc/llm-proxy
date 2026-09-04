# -*- coding: utf-8 -*-
"""
TRAE SOLO CN 官方模型本地代理 - 纯 Python 标准库 + cryptography

把 TRAE SOLO CN（字节）的官方模型通道转成本地 OpenAI 兼容 API：
  OpenAI 兼容    GET /v1/models、POST /v1/chat/completions
  OpenAI 新版    POST /v1/responses（Responses 协议，兼容 codex-cli 等）
  Anthropic 兼容 POST /v1/messages、POST /v1/messages/count_tokens
  健康检查       GET /health

上游（与 codearts_proxy 的直连 chat 不同：TRAE 无纯聊天端点可复用）：
  TRAE 的主 LLM 通道是服务端编排的 Cloud Agent（alaudalog 日志实测时序）：
    POST /api/agent/v3/create_agent_task   建回合，响应为 SSE（ModelConfig 等）
    POST /api/agent/v3/workflow/start      LLM token 流（SSE）
    [tool_call 出现 → 本代理发 interrupt 终止回合]
  本代理把一次 chat 请求映射为一个伪 agent 回合，SSE 事件翻译为 chat chunk：
    thought 事件(thought 字段)   → delta.content
    thought 事件(reasoning 字段) → delta.reasoning_content
    tool_call 事件               → delta.tool_calls + interrupt 终止
    token_usage 事件             → usage
    turn_completion / done       → finish

认证完全复用 TRAE SOLO CN 客户端登录态（算法移植自 trae-mate trae_auth.rs）：
  %APPDATA%/TRAE SOLO CN/User/globalStorage/storage.json
    → iCubeAuthInfo://icube.cloudide（base64 信封）
    → 信封 = HEADER(6B) + randomKey(32B) + AES-128-CBC 密文
    → secret = LEFT_SECRET ⊕ RIGHT_SECRET（64B 常量）
    → key/iv = SHA512( SHA512(randomKey) ++ secret )[0:16]/[16:32]
    → 明文 = SHA512(payload) + payload(JSON: token/userId/host/refreshToken...)
  token 过期时打开 TRAE 客户端让它自动刷新即可（当前 token 至 2026-09-13）。

已知限制（详见 trae_solo_analysis/TRAE_SOLO_CN_分析.md）：
  1. create_agent_task 真实报文 ~88KB（含完整上下文+工具定义），本文件的请求体
     是从日志还原的最小化占位模板——若上游 400/拒绝，需用 mitm（trae_mitm/）
     抓一次真实报文后替换 _build_create_body / _build_workflow_body。
  2. tool_call 即终止：云端工具（RunCommand/Read/Edit...）无法在本机执行，
     工具调用翻译为 OpenAI tool_calls 返回给客户端后立即 interrupt 回合。
  3. 服务端排队（request_wait_in_queue）时首 token 延迟可达分钟级（Free 账号）。
  4. 每条消息是独立"回合"，对话历史被拼接进单条 query（无服务端多轮状态）。

用法:  python trae_proxy.py    （默认端口 8790，环境变量 TRAE_PROXY_PORT 修改）
"""
import base64
import hashlib
import http.server
import json
import os
import platform
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PORT = int(os.environ.get("TRAE_PROXY_PORT", "8790"))
HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA", os.path.join(HOME, "AppData", "Roaming"))

# TRAE SOLO CN 客户端用户数据
STORAGE_JSON = os.path.join(APPDATA, "TRAE SOLO CN", "User", "globalStorage", "storage.json")
WORKSPACE = "d:\\GitHub"  # 客户端日志中的 workspace（占位）

# 上游网关与编排端点（alaudalog 实测）
GATEWAY = "https://trae-api-cn.mchost.guru"
EP_CREATE = GATEWAY + "/api/agent/v3/create_agent_task"
EP_WORKFLOW = GATEWAY + "/api/agent/v3/workflow/start"
EP_INTERRUPT = GATEWAY + "/api/agent/v3/interrupt"

# ── 凭证信封常量（trae-mate trae_auth.rs 逐字节移植）────────────────────────
ENVELOPE_HEADER = bytes([116, 99, 5, 16, 0, 0])
LEFT_SECRET = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251, 124, 227, 57, 130,
    155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203, 84, 123, 148, 50, 166, 194, 35, 61,
    238, 76, 149, 11, 66, 250, 195, 78, 8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109,
    139, 209, 37,
])
RIGHT_SECRET = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95, 96, 81, 127, 169, 25,
    181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239, 160, 224, 59, 77, 174, 42, 245, 176, 200,
    235, 187, 60, 131, 83, 153, 97, 23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33,
    12, 125,
])

# ── 模型清单（batch_get_detail_param 实测 + alaudalog 日志确认）──────────────
# config_name → 服务端 model_name（__dev 后缀由服务端 ModelConfig 事件确认）
MODELS = {
    "DeepSeek-V4-Flash-Official": {"model_name": "DeepSeek-V4-Flash-Official__dev", "display": "DeepSeek-V4-Flash", "ctx": 168000},
    "DeepSeek-V4-Flash":          {"model_name": "DeepSeek-V4-Flash__dev",          "display": "DeepSeek-V4-Flash", "ctx": 168000},
    "DeepSeek-V4-Pro-Official":   {"model_name": "DeepSeek-V4-Pro-Official__dev",   "display": "DeepSeek-V4-Pro",   "ctx": 168000},
    "DeepSeek-V4-Pro":            {"model_name": "DeepSeek-V4-Pro__dev",            "display": "DeepSeek-V4-Pro",   "ctx": 168000},
    "glm-5.2":                    {"model_name": "glm-5.2__dev",                    "display": "GLM-5.2",           "ctx": 200000},
    "glm-5.3":                    {"model_name": "glm-5.3__dev",                    "display": "GLM-5.3",           "ctx": 200000},
    "kimi-k2.6":                  {"model_name": "kimi-k2.6__dev",                  "display": "Kimi-K2.6",         "ctx": 200000},
    "Doubao-Seed-2.1-Pro":        {"model_name": "Doubao-Seed-2.1-Pro__dev",        "display": "Seed-2.1-Pro",      "ctx": 200000},
    "Doubao-Seed-2.1-Turbo":      {"model_name": "Doubao-Seed-2.1-Turbo__dev",      "display": "Seed-2.1-Turbo",    "ctx": 200000},
}
# 小写别名 → 官方 config_name
MODEL_ALIAS = {m.lower(): m for m in MODELS}
MODEL_ALIAS.update({
    "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
    "seed-2.1-pro": "Doubao-Seed-2.1-Pro",
    "seed-2.1-turbo": "Doubao-Seed-2.1-Turbo",
})
DEFAULT_MODEL = "DeepSeek-V4-Flash-Official"

# 日志（同目录；TRAE_PROXY_LOG 空字符串关闭）
LOG_FILE = os.environ.get("TRAE_PROXY_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "trae_proxy.log"))
_log_lock = threading.Lock()

# 直连（不走系统代理；mchost.guru CN 网关可直连，同 trae-mate 结论）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(msg):
    if not LOG_FILE:
        return
    try:
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), msg))
    except Exception:
        pass


def _rand():
    return secrets.token_hex(12)


# ── 凭证：storage.json 信封解密（trae-mate trae_auth.rs 移植）────────────────
_session_lock = threading.Lock()
_session_cache = {"at": 0.0, "data": None}


def _decrypt_auth_info(encoded: str) -> dict:
    """解密 iCubeAuthInfo base64 信封，返回 payload JSON。"""
    envelope = base64.b64decode(encoded)
    if len(envelope) <= 38 or envelope[:6] != ENVELOPE_HEADER:
        raise RuntimeError("Invalid TRAE credential envelope")
    random_key = envelope[6:38]
    secret = bytes(l ^ r for l, r in zip(LEFT_SECRET, RIGHT_SECRET))
    derived = hashlib.sha512(hashlib.sha512(random_key).digest() + secret).digest()
    key, iv = derived[:16], derived[16:32]
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(envelope[38:]) + dec.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16:
        raise RuntimeError("AES decrypt failed (bad padding)")
    plain = padded[:-pad]
    if len(plain) < 64:
        raise RuntimeError("decrypted payload too short")
    digest, payload = plain[:64], plain[64:]
    if hashlib.sha512(payload).digest() != digest:
        raise RuntimeError("TRAE credential integrity check failed")
    return json.loads(payload.decode("utf-8"))


def _decode_jwt_exp(token: str):
    """解析 JWT exp（不校验签名），返回 epoch 秒或 None。"""
    try:
        p64 = token.split(".")[1].replace("-", "+").replace("_", "/")
        p64 += "=" * (-len(p64) % 4)
        payload = json.loads(base64.b64decode(p64).decode("utf-8"))
        return int(payload.get("exp") or 0) or None
    except Exception:
        return None


def _read_session() -> dict:
    """读 TRAE 登录态：{token, user_id, device_id, machine_id, exp}。缓存 60s。"""
    now = time.time()
    with _session_lock:
        if _session_cache["data"] and now - _session_cache["at"] < 60:
            return _session_cache["data"]
    if not os.path.exists(STORAGE_JSON):
        raise RuntimeError("找不到 %s，请先安装并登录 TRAE SOLO CN" % STORAGE_JSON)
    with open(STORAGE_JSON, "r", encoding="utf-8") as f:
        storage = json.load(f)
    encoded = storage.get("iCubeAuthInfo://icube.cloudide")
    if not encoded:
        raise RuntimeError("storage.json 无 iCubeAuthInfo（未登录？）")
    info = _decrypt_auth_info(encoded)
    token = info.get("token") or ""
    if not token:
        raise RuntimeError("TRAE 登录 token 为空")
    # device_id 取 iCubeAuthInfo://icube-dc:<id> 键后缀（alaudalog 实测 user_unique_id）
    device_id = ""
    for k in storage:
        if k.startswith("iCubeAuthInfo://icube-dc:"):
            device_id = k[len("iCubeAuthInfo://icube-dc:"):]
            break
    sess = {
        "token": token,
        "user_id": info.get("userId") or "",
        "device_id": device_id,
        "machine_id": storage.get("telemetry.machineId") or "",
        "host": info.get("host") or GATEWAY,
        "exp": _decode_jwt_exp(token),
    }
    with _session_lock:
        _session_cache["at"] = now
        _session_cache["data"] = sess
    return sess


# ── 上游请求头（mitm dump 实测 27 头模板的子集）──────────────────────────────
def build_headers(sess: dict, body_len: int) -> dict:
    trace = secrets.token_hex(16)
    h = {
        "content-type": "application/json",
        "authorization": "Cloud-IDE-JWT " + sess["token"],
        "x-ide-token": sess["token"],
        "x-app-id": "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8",
        "x-app-version": "default",
        "x-app-version-code": "20260820",
        "x-ide-version": "0.1.60",
        "x-ide-version-code": "20260820",
        "x-ide-version-type": "stable",
        "x-custom-trace-id": trace,
        "x-flow-traceparent": "04-%s-%s-01" % (trace, secrets.token_hex(8)),
        "x-device-id": sess["device_id"],
        "x-machine-id": sess["machine_id"],
        "x-device-type": "windows",
        "x-os-version": platform.platform()[:64],
        "x-device-cpu": "Intel",
        "x-user-region": "CN",
        "request-traffic-type": "prod",
        "user-agent": "TraeClient/TTNet",
        "x-lgw-req-sdk-type": "3",
        "package-type": "stable_cn",
        "x-lscbd-aid": "787976",
        "x-ckg-user-id": sess["user_id"],
        "x-request-id": "req_" + str(uuid.uuid4()),
        "content-length": str(body_len),
    }
    return h


def _post(url: str, sess: dict, body: dict, timeout: int = 60):
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    for k, v in build_headers(sess, len(payload)).items():
        req.add_header(k, v)
    return _opener.open(req, timeout=timeout)


# ── 上游：伪 agent 回合（编排协议）───────────────────────────────────────────
class UpstreamError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _pseudo_object_id() -> str:
    """24-hex（MongoDB ObjectId 风格）会话 id。"""
    return "%08x%s" % (int(time.time()), secrets.token_hex(8))


def _common_params(sess: dict) -> str:
    """客户端 common_params（alaudalog 日志还原的精简版）。"""
    cp = {
        "icube_uid": sess["user_id"], "user_id": sess["user_id"], "biz_user_id": sess["user_id"],
        "user_is_login": True, "user_unique_id": sess["device_id"], "device_id": sess["device_id"],
        "machine_id": sess["machine_id"], "arch": platform.machine() or "x64", "system": "win32",
        "scope": "marscode", "tenant": "marscode", "region": "CN", "aiRegion": "CN",
        "quality": "stable", "app_version": "0.1.60", "vscode_version": "1.107.1",
        "os_name": "windows", "os_version": platform.platform()[:64],
        "platform": "electron", "identity": "0", "identity_str": "Free",
        "language": "zh-cn", "app_language": "zh-cn", "chat_mode": 1,
        "product_code": "SOLO_Lite", "agent_runtime_implementation": "ai-agent",
    }
    return json.dumps(cp, ensure_ascii=False)


def build_create_body(sess: dict, session_id: str, query_text: str, cfg: dict) -> dict:
    """
    create_agent_task 请求体。

    ⚠️ 占位模板：真实报文 ~88KB（完整上下文+工具定义+模型信息），结构从
    alaudalog 日志的 SendMessageRequest / start_chat 还原。若上游 400，
    用 trae_mitm 抓真实报文替换本函数与 build_workflow_body。
    """
    return {
        "session_id": session_id,
        "conversation_id": session_id,
        "user_id": sess["user_id"],
        "device_id": sess["device_id"],
        "content": [],
        "model_name": cfg["name"],
        "config_name": cfg["name"],
        "agent_type": "solo_agent_lite",
        "agent_id": "solo_agent_lite",
        "query": json.dumps([{"type": "text", "data": {"content": query_text}}], ensure_ascii=False),
        "user_input": query_text,
        "workspace_folders": [WORKSPACE],
        "scene_location": 2,
        "ide_version": "0.1.60",
        "app_version": "0.1.60",
        "custom_model": {
            "provider": "", "is_preset": True,
            "config_name": cfg["name"], "config_source": 1, "model_name": cfg["name"],
            "use_remote_service": True, "multimodal": False,
            "prompt_max_tokens": 936000, "reasoning_effort_level": "high",
        },
        "common_params": _common_params(sess),
    }


def build_workflow_body(sess: dict, session_id: str, task_id: str, query_text: str, cfg: dict) -> dict:
    """workflow/start 请求体（占位模板，同上警告）。"""
    return {
        "task_id": task_id,
        "chat_session_id": session_id,
        "user_id": sess["user_id"],
        "agent_id": "solo_agent_lite",
        "agent_type": "solo_agent_lite",
        "model_name": cfg["name"],
        "query": json.dumps([{"type": "text", "data": {"content": query_text}}], ensure_ascii=False),
        "workspace_folders": [WORKSPACE],
        "common_params": _common_params(sess),
    }


def _interrupt(sess: dict, session_id: str):
    """终止 agent 回合（用户取消时客户端也调此端点）。"""
    try:
        _post(EP_INTERRUPT, sess, {"chat_session_id": session_id}, timeout=10).close()
        _log("interrupt 已发送 session=%s" % session_id)
    except Exception as e:
        _log("interrupt 失败: %r" % e)


def _iter_sse(resp):
    """解析上游 SSE：产出 (event_name, data_text)。"""
    buf = b""
    event = ""
    data_parts = []
    while True:
        try:
            chunk = resp.read(4096)
        except Exception:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            s = line.decode("utf-8", "replace").rstrip("\r")
            if s == "":
                if data_parts:
                    yield event, "\n".join(data_parts)
                event, data_parts = "", []
            elif s.startswith(":"):
                continue
            elif s.startswith("event:"):
                event = s[6:].strip()
            elif s.startswith("data:"):
                data_parts.append(s[5:].strip())
    if data_parts:
        yield event, "\n".join(data_parts)


def _extract_task_id(create_text: str) -> str:
    """从 create_agent_task 响应提取 task_id（响应可能是 JSON 或 SSE）。"""
    candidates = [create_text]
    if "data:" in create_text:
        candidates = [ln[len("data:"):].strip() for ln in create_text.splitlines()
                      if ln.startswith("data:")] + candidates
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k in ("task_id", "taskId", "agent_run_id", "agentRunId"):
                    v = cur.get(k)
                    if isinstance(v, str) and v:
                        return v
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return ""


def agent_turn_chunks(sess: dict, cfg: dict, query_text: str):
    """
    执行一个伪 agent 回合，产出内部 OpenAI chat.completion.chunk 字典流。
    流结束原因：turn_completion→stop；tool_call→tool_calls（并发 interrupt）。
    """
    session_id = _pseudo_object_id()

    # 1) create_agent_task（建回合）
    try:
        resp = _post(EP_CREATE, sess, build_create_body(sess, session_id, query_text, cfg), timeout=120)
        create_text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        _log("create_agent_task %s: %s" % (e.code, detail[:200]))
        raise UpstreamError("create_agent_task %s: %s（模板占位，如持续 400 需 mitm 抓真实报文）" % (e.code, detail))
    except Exception as e:
        raise UpstreamError("create_agent_task 连接失败: %s" % e)
    task_id = _extract_task_id(create_text)
    _log("create_agent_task ok session=%s task=%s" % (session_id, task_id or "?"))

    # 2) workflow/start（token 流）
    try:
        resp = _post(EP_WORKFLOW, sess, build_workflow_body(sess, session_id, task_id, query_text, cfg), timeout=600)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise UpstreamError("workflow/start %s: %s" % (e.code, detail))
    except Exception as e:
        raise UpstreamError("workflow/start 连接失败: %s" % e)

    ct = (resp.headers.get("Content-Type") or "").lower()
    chat_id = "chatcmpl-" + _rand()
    created = int(time.time())
    finish_reason = None
    usage = None

    def chunk(delta, fr=None):
        return {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                "model": cfg["name"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}

    yield chunk({"role": "assistant", "content": ""})

    if "event-stream" not in ct:
        # 非流式响应：整体 JSON（罕见，防御）
        data = resp.read().decode("utf-8", "replace")
        try:
            obj = json.loads(data)
            text = json.dumps(obj, ensure_ascii=False)
            yield chunk({"content": text})
        except Exception:
            yield chunk({"content": data[:2000]})
        yield chunk({}, "stop")
        return

    for event, data_text in _iter_sse(resp):
        if data_text == "[DONE]":
            break
        try:
            data = json.loads(data_text)
        except Exception:
            data = {"_raw": data_text}
        if not isinstance(data, dict):
            data = {"_raw": str(data)}
        name = event or data.get("event") or data.get("type") or ""

        # —— token 流：thought（正文）/ reasoning（推理）——
        if name in ("thought", "output", "message", "") and ("thought" in data or "reasoning" in data or "_raw" in data):
            reasoning = data.get("reasoning")
            thought = data.get("thought")
            if isinstance(reasoning, str) and reasoning:
                yield chunk({"reasoning_content": reasoning})
            if isinstance(thought, str) and thought:
                yield chunk({"content": thought})
            if "_raw" in data and not thought and not reasoning:
                yield chunk({"content": data["_raw"]})
            continue

        if name == "token_usage" or name == "compact_token_usage":
            usage = {
                "prompt_tokens": data.get("input_tokens") or data.get("prompt_tokens") or 0,
                "completion_tokens": data.get("output_tokens") or data.get("completion_tokens") or 0,
                "total_tokens": (data.get("input_tokens") or data.get("prompt_tokens") or 0)
                                + (data.get("output_tokens") or data.get("completion_tokens") or 0),
            }
            continue

        if name == "request_wait_in_queue":
            pos = data.get("position")
            _log("排队中 session=%s position=%s" % (session_id, pos))
            continue

        if name == "tool_call":
            tid = data.get("toolcall_id") or data.get("id") or ("call_" + secrets.token_hex(12))
            tname = data.get("tool_name") or data.get("name") or "unknown"
            args = data.get("arguments") or data.get("input") or {}
            args_text = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            yield chunk({"tool_calls": [{"index": 0, "id": tid, "type": "function",
                                         "function": {"name": tname, "arguments": args_text}}]})
            finish_reason = "tool_calls"
            _interrupt(sess, session_id)
            break

        if name == "turn_completion":
            finish_reason = "stop"
            break

        if name in ("done", "workflow_finish"):
            finish_reason = finish_reason or "stop"
            break

        if name in ("content_security", "error"):
            code = data.get("code") or data.get("error_code")
            msg = data.get("message") or data.get("error_message") or data_text[:200]
            raise UpstreamError("上游事件错误 %s(code=%s): %s" % (name, code, msg))

    if finish_reason is None:
        finish_reason = "stop"
    final = chunk({}, finish_reason)
    if usage:
        final["usage"] = usage
    yield final


# ── messages → query 文本（无服务端多轮状态，整段拼接）────────────────────────
def messages_to_query(msgs) -> str:
    parts = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")
        if isinstance(content, list):
            texts = []
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    texts.append(p["text"])
                elif isinstance(p, str):
                    texts.append(p)
            text = "\n".join(texts)
        elif isinstance(content, str):
            text = content
        else:
            text = json.dumps(content, ensure_ascii=False) if content is not None else ""
        tool_calls = m.get("tool_calls")
        if tool_calls:
            text += "\n" + json.dumps(tool_calls, ensure_ascii=False)
        if role == "system":
            parts.append("[System]\n" + text)
        elif role == "assistant":
            parts.append("[Assistant]\n" + text)
        elif role == "tool":
            parts.append("[Tool result]\n" + text)
        else:
            parts.append(text)
    return "\n\n".join(p for p in parts if p.strip()) or " "


# ── chunk 流聚合（非流式响应）───────────────────────────────────────────────
def collect_chunks(chunks, model_name):
    text_parts = []
    reasoning_parts = []
    tool_calls = {}
    usage = None
    finish = None
    for c in chunks:
        usage = c.get("usage") or usage
        choice = (c.get("choices") or [{}])[0]
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
        d = choice.get("delta") or {}
        if d.get("content"):
            text_parts.append(d["content"])
        if d.get("reasoning_content"):
            reasoning_parts.append(d["reasoning_content"])
        for tc in d.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]
    message = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [{
            "id": slot["id"] or ("call_" + _rand()),
            "type": "function",
            "function": {"name": slot["name"], "arguments": slot["args"] or "{}"},
        } for _, slot in sorted(tool_calls.items())]
    return {
        "id": "chatcmpl-" + _rand(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ── 协议转换：Responses / Anthropic → OpenAI chat ───────────────────────────
def convert_responses_to_openai(body: dict) -> dict:
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("max_output_tokens") is not None:
        oa["max_tokens"] = body["max_output_tokens"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oa["top_p"] = body["top_p"]

    msgs = []
    if isinstance(body.get("instructions"), str) and body["instructions"]:
        msgs.append({"role": "system", "content": body["instructions"]})
    inp = body.get("input")
    if isinstance(inp, str) and inp:
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "message":
                role = "assistant" if item.get("role") == "assistant" else "user"
                c = item.get("content")
                text = ""
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text") and part.get("text"):
                            text += part["text"]
                if text:
                    msgs.append({"role": role, "content": text})
            elif t == "function_call":
                tc = {"id": item.get("call_id", ""), "type": "function",
                      "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")}}
                if msgs and msgs[-1].get("role") == "assistant":
                    msgs[-1].setdefault("tool_calls", []).append(tc)
                else:
                    msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
            elif t == "function_call_output":
                o = item.get("output")
                out_text = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": out_text})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    return oa


def convert_anthropic_to_openai(body: dict) -> dict:
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]

    msgs = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        sys_text = "\n".join(p.get("text", "") for p in sys_
                             if isinstance(p, dict) and p.get("type") == "text")
        if sys_text.strip():
            msgs.append({"role": "system", "content": sys_text.strip()})

    for msg in body.get("messages") or []:
        role = msg.get("role")
        c = msg.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        if not isinstance(c, list):
            continue
        if role == "assistant":
            text_parts = []
            tool_calls = []
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
                elif part.get("type") == "tool_use":
                    tool_calls.append({
                        "id": part.get("id", ""), "type": "function",
                        "function": {"name": part.get("name", ""),
                                     "arguments": json.dumps(part.get("input") or {}, ensure_ascii=False)},
                    })
            amsg = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                amsg["tool_calls"] = tool_calls
            msgs.append(amsg)
        elif role == "user":
            for part in c:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    rc = part.get("content")
                    res = rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False)
                    msgs.append({"role": "tool", "tool_call_id": part.get("tool_use_id", ""), "content": res})
            texts = [p.get("text", "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            if any(t.strip() for t in texts):
                msgs.append({"role": "user", "content": "\n".join(texts)})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    return oa


def parse_anthropic_text(body: dict) -> list:
    texts = []
    sys_ = body.get("system")
    if isinstance(sys_, str):
        texts.append(sys_)
    elif isinstance(sys_, list):
        for p in sys_:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(p.get("text", ""))
    for msg in body.get("messages") or []:
        c = msg.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        texts.append(p.get("text", ""))
                    elif p.get("type") in ("tool_use", "tool_result"):
                        texts.append(json.dumps(p, ensure_ascii=False))
    return texts


def convert_openai_to_responses(openai_obj: dict, req_model: str) -> dict:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    output = []
    if message.get("reasoning_content"):
        output.append({"type": "reasoning", "id": "rs_" + _rand(),
                       "summary": [{"type": "summary_text", "text": message["reasoning_content"]}]})
    if message.get("content"):
        output.append({"type": "message", "id": "msg_" + _rand(), "role": "assistant",
                       "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for tc in message.get("tool_calls") or []:
        output.append({"type": "function_call", "id": "fc_" + _rand(), "call_id": tc.get("id", ""),
                       "name": (tc.get("function") or {}).get("name", ""),
                       "arguments": (tc.get("function") or {}).get("arguments", "{}"),
                       "status": "completed"})
    incomplete = choice.get("finish_reason") == "length"
    return {
        "id": "resp_" + _rand(), "object": "response", "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": req_model, "output": output,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
    }


def convert_openai_to_anthropic(openai_obj: dict, req_model: str) -> dict:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    content = []
    if message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        try:
            input_obj = json.loads((tc.get("function") or {}).get("arguments", "{}"))
        except Exception:
            input_obj = {}
        content.append({"type": "tool_use", "id": tc.get("id", "toolu_" + _rand()),
                        "name": (tc.get("function") or {}).get("name", ""), "input": input_obj})
    finish = choice.get("finish_reason")
    stop_reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(finish, finish or "end_turn")
    return {
        "id": "msg_" + _rand(), "type": "message", "role": "assistant",
        "model": req_model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


# ── 流式状态机：内部 chat chunk → Responses / Anthropic SSE ─────────────────
class _ResponsesStreamState:
    """内部 chat chunk → Responses 协议事件序列。"""

    def __init__(self, model: str):
        self.model = model
        self.resp_id = "resp_" + _rand()
        self.created = int(time.time())
        self.started = False
        self.finished = False
        self.finish_reason = None
        self.text = ""
        self.reasoning = ""
        self.tool_name = ""
        self.tool_id = ""
        self.tool_args = ""
        self.usage = None
        self.text_item_id = "msg_" + _rand()
        self.reason_item_id = "rs_" + _rand()
        self.reason_opened = False
        self.text_opened = False
        self.reason_index = None
        self.text_index = None
        self.next_index = 0

    def feed(self, chunk: dict):
        out = []
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}

        if not self.started:
            self.started = True
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))

        rc = delta.get("reasoning_content")
        if rc:
            self.reasoning += rc
            if not self.reason_opened:
                self.reason_opened = True
                self.reason_index = self.next_index
                self.next_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": self.reason_index,
                    "item": {"type": "reasoning", "id": self.reason_item_id, "summary": []},
                }, ensure_ascii=False)))
                out.append(("response.reasoning_summary_part.added", json.dumps({
                    "type": "response.reasoning_summary_part.added", "item_id": self.reason_item_id,
                    "output_index": self.reason_index, "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                }, ensure_ascii=False)))
            out.append(("response.reasoning_summary_text.delta", json.dumps({
                "type": "response.reasoning_summary_text.delta", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "delta": rc,
            }, ensure_ascii=False)))

        content = delta.get("content")
        if content:
            self.text += content
            if not self.text_opened:
                self.text_opened = True
                self.text_index = self.next_index
                self.next_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": self.text_index,
                    "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                             "status": "in_progress", "content": []},
                }, ensure_ascii=False)))
                out.append(("response.content_part.added", json.dumps({
                    "type": "response.content_part.added", "item_id": self.text_item_id,
                    "output_index": self.text_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }, ensure_ascii=False)))
            out.append(("response.output_text.delta", json.dumps({
                "type": "response.output_text.delta", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "delta": content,
            }, ensure_ascii=False)))

        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if tc.get("id"):
                self.tool_id = tc["id"]
            if fn.get("name"):
                self.tool_name = fn["name"]
            if fn.get("arguments"):
                self.tool_args += fn["arguments"]
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        if not self.started:
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))
        if self.reason_opened:
            out.append(("response.reasoning_summary_text.done", json.dumps({
                "type": "response.reasoning_summary_text.done", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "text": self.reasoning,
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": self.reason_index,
                "item": {"type": "reasoning", "id": self.reason_item_id,
                         "summary": [{"type": "summary_text", "text": self.reasoning}]},
            }, ensure_ascii=False)))
        if self.text_opened:
            out.append(("response.output_text.done", json.dumps({
                "type": "response.output_text.done", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "text": self.text,
            }, ensure_ascii=False)))
            out.append(("response.content_part.done", json.dumps({
                "type": "response.content_part.done", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0,
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": self.text_index,
                "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": self.text, "annotations": []}]},
            }, ensure_ascii=False)))
        items = []
        if self.reasoning:
            items.append({"type": "reasoning", "id": self.reason_item_id,
                          "summary": [{"type": "summary_text", "text": self.reasoning}]})
        if self.text:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": self.text, "annotations": []}]})
        if self.tool_name:
            items.append({"type": "function_call", "id": "fc_" + _rand(), "call_id": self.tool_id,
                          "name": self.tool_name, "arguments": self.tool_args, "status": "completed"})
        if not items:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": "", "annotations": []}]})
        usage = self.usage or {}
        out.append(("response.completed", json.dumps({
            "type": "response.completed",
            "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                         "status": "completed" if self.finish_reason != "length" else "incomplete",
                         "model": self.model, "output": items,
                         "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                   "output_tokens": usage.get("completion_tokens", 0),
                                   "total_tokens": usage.get("total_tokens", 0)},
                         "error": None,
                         "incomplete_details": {"reason": "max_output_tokens"}
                         if self.finish_reason == "length" else None},
        }, ensure_ascii=False)))
        return out


class _AnthropicStreamState:
    """内部 chat chunk → Anthropic Messages SSE 事件序列。"""

    def __init__(self, model: str):
        self.model = model
        self.msg_id = "msg_" + _rand()
        self.started = False
        self.finished = False
        self.thinking_block = False
        self.text_block = False
        self.block_index = 0
        self.text = ""
        self.thinking = ""
        self.tool_block = None  # {index, id, name, args}
        self.usage = None

    def _start(self, out):
        if self.started:
            return
        self.started = True
        out.append(("message_start", json.dumps({
            "type": "message_start",
            "message": {"id": self.msg_id, "type": "message", "role": "assistant", "content": [],
                        "model": self.model, "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        }, ensure_ascii=False)))

    def _close_thinking(self, out):
        if self.thinking_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.thinking_block = False

    def _close_text(self, out):
        if self.text_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.text_block = False

    def feed(self, chunk: dict):
        out = []
        self._start(out)
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        delta = choice.get("delta") or {}
        rc = delta.get("reasoning_content")
        content = delta.get("content")
        if rc:
            if not self.thinking_block:
                self._close_text(out)
                self.thinking_block = True
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.block_index - 1,
                    "content_block": {"type": "thinking", "thinking": ""},
                }, ensure_ascii=False)))
            self.thinking += rc
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "thinking_delta", "thinking": rc},
            }, ensure_ascii=False)))
        if content:
            if not self.text_block:
                self._close_thinking(out)
                self.text_block = True
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.block_index - 1,
                    "content_block": {"type": "text", "text": ""},
                }, ensure_ascii=False)))
            self.text += content
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "text_delta", "text": content},
            }, ensure_ascii=False)))
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if self.tool_block is None:
                self._close_thinking(out)
                self._close_text(out)
                self.block_index += 1
                self.tool_block = {"index": self.block_index - 1,
                                   "id": tc.get("id", ""), "name": fn.get("name", ""), "args": ""}
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.tool_block["index"],
                    "content_block": {"type": "tool_use", "id": self.tool_block["id"],
                                      "name": self.tool_block["name"], "input": {}},
                }, ensure_ascii=False)))
            if fn.get("arguments"):
                self.tool_block["args"] += fn["arguments"]
                out.append(("content_block_delta", json.dumps({
                    "type": "content_block_delta", "index": self.tool_block["index"],
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                }, ensure_ascii=False)))
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        self._start(out)
        self._close_thinking(out)
        self._close_text(out)
        if self.tool_block:
            try:
                parsed = json.loads(self.tool_block["args"] or "{}")
            except Exception:
                parsed = {}
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.tool_block["index"]}, ensure_ascii=False)))
        usage = self.usage or {}
        stop_reason = "tool_use" if self.tool_block else "end_turn"
        out.append(("message_delta", json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)},
        }, ensure_ascii=False)))
        out.append(("message_stop", json.dumps({"type": "message_stop"}, ensure_ascii=False)))
        return out


# ── HTTP Handler ────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        _log("%s - %s" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/health":
                try:
                    s = _read_session()
                    exp = s.get("exp")
                    exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "unknown"
                    return self._json(200, {"ok": True, "user": s["user_id"],
                                            "tokenExp": exp_str,
                                            "models": sorted(MODELS)})
                except Exception as e:
                    return self._json(200, {"ok": False, "error": str(e)})
            if path in ("/v1/models", "/models"):
                now = int(time.time())
                return self._json(200, {
                    "object": "list",
                    "data": [{
                        "id": mid,
                        "object": "model",
                        "created": now,
                        "owned_by": "trae-solo-cn",
                        "metadata": {"display_name": m["display"], "model_name": m["model_name"],
                                     "context_window": m["ctx"]},
                    } for mid, m in sorted(MODELS.items())],
                })
            return self._json(404, {"error": {"message": "支持: GET /v1/models, GET /health, POST /v1/chat/completions, /v1/responses, /v1/messages"}})
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        if path == "/v1/messages":
            return self._handle_chat("anthropic")
        if path in ("/v1/responses", "/responses"):
            return self._handle_chat("responses")
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._handle_chat("openai")
        return self._json(404, {"error": {"message": "支持: POST /v1/chat/completions, /v1/responses, /v1/messages, /v1/messages/count_tokens"}})

    def _handle_count_tokens(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})
        total = sum(len(t) for t in parse_anthropic_text(req))
        return self._json(200, {"input_tokens": max(1, total // 3)})

    def _handle_chat(self, protocol: str):
        # 1) 读请求体
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req_body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})

        # 2) 协议请求转换（统一取 messages 数组）
        req_model_name = req_body.get("model") or ""
        if protocol == "responses":
            oa = convert_responses_to_openai(req_body)
        elif protocol == "anthropic":
            oa = convert_anthropic_to_openai(req_body)
        else:
            oa = dict(req_body)

        # 3) 模型解析（别名 → config_name）
        model = oa.get("model") or DEFAULT_MODEL
        real = MODEL_ALIAS.get(model, model)
        if real not in MODELS:
            return self._json(404, {"error": {"message": "模型 %s 不存在。可用: %s" % (model, ", ".join(sorted(MODELS)))}})
        cfg = {"name": real, **MODELS[real]}
        is_stream = bool(oa.get("stream"))

        # 4) 凭证
        try:
            sess = _read_session()
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

        # 5) 上游 agent 回合
        query = messages_to_query(oa.get("messages"))
        try:
            chunks = list(agent_turn_chunks(sess, cfg, query))
        except UpstreamError as e:
            return self._json(e.status, {"error": {"message": str(e)}})
        except Exception as e:
            return self._json(502, {"error": {"message": "上游异常: %s" % e}})

        _log("回合完成 model=%s stream=%s chunks=%d" % (real, is_stream, len(chunks)))

        # 6a) 流式：协议状态机输出 SSE
        if is_stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            st = None
            if protocol == "responses":
                st = _ResponsesStreamState(req_model_name)
            elif protocol == "anthropic":
                st = _AnthropicStreamState(req_model_name)

            def _write_sse(ev, payload):
                line_out = ("event: %s\n" % ev if ev else "") + "data: %s\n\n" % payload
                b_out = line_out.encode("utf-8")
                self.wfile.write(b"%x\r\n" % len(b_out) + b_out + b"\r\n")
                self.wfile.flush()

            try:
                final_sent = False
                for c in chunks:
                    if st is not None:
                        for ev, pl in st.feed(c):
                            _write_sse(ev, pl)
                    else:
                        _write_sse("", json.dumps(c, ensure_ascii=False))
                if st is not None:
                    for ev, pl in st.finish():
                        _write_sse(ev, pl)
                        if ev in ("response.completed", "message_stop"):
                            final_sent = True
                else:
                    _write_sse("", "[DONE]")
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                _log("客户端提前断开 stream model=%s" % real)
            return

        # 6b) 非流式：聚合 → 协议转换
        merged = collect_chunks(chunks, real)
        if protocol == "responses":
            return self._json(200, convert_openai_to_responses(merged, req_model_name))
        if protocol == "anthropic":
            return self._json(200, convert_openai_to_anthropic(merged, req_model_name))
        return self._json(200, merged)


def main():
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        sess = _read_session()
        exp = sess.get("exp")
        exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)) if exp else "unknown"
        print("TRAE 官方模型代理已启动: http://127.0.0.1:%d" % PORT)
        print("用户: %s  token 有效期: %s" % (sess["user_id"], exp_str))
    except Exception as e:
        print("TRAE 官方模型代理已启动: http://127.0.0.1:%d" % PORT)
        print("警告: 凭证暂不可用（%s）；启动 TRAE SOLO CN 登录后自动恢复" % e)
    print("模型: %s" % ", ".join(sorted(MODELS)))
    print("上游: agent 编排通道 %s" % GATEWAY)
    print("认证: 自动复用 TRAE SOLO CN 登录态（token 过期时打开客户端刷新）")
    _log("启动 port=%d models=%d" % (PORT, len(MODELS)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
