# -*- coding: utf-8 -*-
"""
CodeArts Agent (华为) 官方模型本地代理 - 纯 Python 标准库 + cryptography

把 CodeArts Agent 的官方模型通道（inferhub / snap-access）转成本地 API：
  OpenAI 兼容    GET /v1/models、POST /v1/chat/completions（SSE 直通）
  OpenAI 新版    POST /v1/responses（Responses 协议，兼容 codex-cli 等）
  Anthropic 兼容 POST /v1/messages、POST /v1/messages/count_tokens
  健康检查       GET /health

模型（实测可用）：
  GLM-5.2 / glm-5.2-sft-harmony / openpangu-2.0-pro / openpangu-2.0-flash

认证完全复用 CodeArts Agent 客户端的登录态：
  凭证（AK/SK/securitytoken）从客户端的 state.vscdb 解出
  （Electron os_crypt DPAPI + AES-256-GCM 双层加密）。
  token 过期时打开 CodeArts Agent 客户端让它自动刷新即可。

本代理不做任何内容拦截/脱敏/审计（用户明确：CodeArts 通道不需要管控层）。

用法:  python codearts_proxy.py    （默认端口 8788，环境变量 CODEARTS_PROXY_PORT 修改）
"""
import base64
import ctypes
import hashlib
import hmac
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# cryptography 仅用于解密本地凭证（AES-256-GCM）；没有它就无法取 AK/SK
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PORT = int(os.environ.get("CODEARTS_PROXY_PORT", "8788"))
HOME = os.path.expanduser("~")

# CodeArts Agent (Electron) 的用户数据
LOCAL_STATE = os.path.join(HOME, "AppData", "Roaming", "codearts-agent", "Local State")
STATE_VSCDB = os.path.join(HOME, "AppData", "Roaming", "codearts-agent", "User", "globalStorage", "state.vscdb")
SESSION_KEY = 'secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}'

# 上游（与内核 INFERHUB_BASE_URLS 一致，主用 .com 备用 .cn）
UPSTREAM = "https://snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions"

# 模型清单（内核日志实测：模型 id 与上下文窗口）
MODELS = {
    "GLM-5.2":              {"name": "GLM-5.2",           "ctx": 202752, "desc": "最新旗舰模型，专为长程任务打造"},
    "glm-5.2-sft-harmony":  {"name": "GLM-5.2-ArkTS-SPARK", "ctx": 202752, "desc": "基于GLM-5.2增训鸿蒙代码与开发知识"},
    "openpangu-2.0-pro":    {"name": "OpenPangu-2.0-Pro",  "ctx": 524288, "desc": "最新旗舰模型，复杂工程稳定交付"},
    "openpangu-2.0-flash":  {"name": "OpenPangu-2.0-Flash", "ctx": 524288, "desc": "均衡推理效果与性能"},
}
# 别名 → 真实 model id（想加别名只改这张表）
MODEL_ALIAS = {
    "glm-5.2": "GLM-5.2",
    "glm-5.2-harmony": "glm-5.2-sft-harmony",
    "pangu-pro": "openpangu-2.0-pro",
    "pangu-flash": "openpangu-2.0-flash",
}

# 日志（同目录；CODEARTS_PROXY_LOG 空字符串关闭）
LOG_FILE = os.environ.get("CODEARTS_PROXY_LOG", os.path.join(os.path.dirname(os.path.abspath(__file__)), "codearts_proxy.log"))

_write_queue = []
_write_lock = threading.Lock()
_write_event = threading.Event()
_writer_started = False
_writer_thread = None


def _writer_loop():
    while True:
        _write_event.wait()
        _write_event.clear()
        while True:
            with _write_lock:
                batch = _write_queue[:]
                _write_queue.clear()
            if not batch:
                break
            try:
                if LOG_FILE:
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write("\n".join(batch) + "\n")
            except Exception:
                pass


def start_writer():
    global _writer_started, _writer_thread
    with _write_lock:
        if _writer_started:
            return
        _writer_started = True
    _writer_thread = threading.Thread(target=_writer_loop, daemon=True, name="codearts-proxy-writer")
    _writer_thread.start()


def _log(msg):
    if not LOG_FILE:
        return
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()), msg)
    with _write_lock:
        _write_queue.append(line)
    _write_event.set()


# ── 凭证：DPAPI + AES-256-GCM 解 CodeArts Agent 的登录态 ──────────────────
_session_lock = threading.Lock()
_session_cache = {"at": 0.0, "data": None}


def _dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI（当前用户）解密，等价 Node 的 safeStorage.decryptString"""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("pb", ctypes.c_void_p)]
    class OUT_BLOB(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("pb", ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data, len(data))
    in_ = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    out = OUT_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(in_), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise RuntimeError("DPAPI 解密失败（需在与写入时相同的 Windows 用户下运行）")
    try:
        return bytes(ctypes.string_at(out.pb, out.cb))
    finally:
        ctypes.windll.kernel32.LocalFree(out.pb)


def _get_master_key() -> bytes:
    """从 Local State 取 os_crypt.encrypted_key → DPAPI 解出 32B AES key"""
    with open(LOCAL_STATE, encoding="utf-8") as f:
        ls = json.load(f)
    blob = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if blob[:5] != b"DPAPI":
        raise RuntimeError("os_crypt.encrypted_key 格式异常")
    key = _dpapi_unprotect(blob[5:])
    if len(key) != 32:
        raise RuntimeError("主密钥长度异常: %d" % len(key))
    return key


def _load_session_blob() -> bytes:
    """从 state.vscdb 读 HuaweiCloudSession 的 v10 加密 blob"""
    if not os.path.exists(STATE_VSCDB):
        raise RuntimeError("找不到 %s\n请先安装并登录 CodeArts Agent 客户端" % STATE_VSCDB)
    con = sqlite3.connect("file:%s?mode=ro" % STATE_VSCDB.replace("\\", "/"), uri=True, timeout=2)
    try:
        row = con.execute("SELECT value FROM ItemTable WHERE key=?", (SESSION_KEY,)).fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("state.vscdb 中未找到华为云会话，请先在 CodeArts Agent 里登录")
    d = json.loads(row[0])
    blob = bytes(d["data"])
    if blob[:3] != b"v10":
        raise RuntimeError("会话密文格式异常（期望 v10 前缀）")
    return blob


def _parse_exp(s):
    """expires_at: '2026-09-04T07:27:35.696Z' → epoch 秒（失败返回 0）"""
    from datetime import datetime, timezone
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0.0


def _read_session() -> dict:
    """解出 {ak, sk, securitytoken, exp, account}，10s 进程内缓存"""
    now = time.time()
    data = _session_cache["data"]
    if data and now - _session_cache["at"] < 10:
        return data
    with _session_lock:
        if _session_cache["data"] and time.time() - _session_cache["at"] < 10:
            return _session_cache["data"]
        key = _get_master_key()
        blob = _load_session_blob()
        pt = AESGCM(key).decrypt(blob[3:15], blob[15:], None)  # nonce + ct + tag
        j = json.loads(pt.decode("utf-8"))
        exp = _parse_exp(j.get("expires_at", ""))
        if exp and exp < now:
            raise RuntimeError("凭证已过期（%s），请打开 CodeArts Agent 客户端让它自动刷新后重试" % j.get("expires_at"))
        session = {
            "ak": j["accessKey"],
            "sk": j["secretKey"],
            "securitytoken": j.get("securitytoken", ""),
            "exp": exp,
            "expires_at": j.get("expires_at", ""),
            "account": (j.get("account") or {}).get("label", ""),
        }
        _session_cache.update({"at": time.time(), "data": session})
        return session


# ── AKSK 签名（复刻 agentkernel 的 AKSKSigner.sign，华为云 SDK-HMAC-SHA256）──

_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _url_encode(s: str) -> str:
    """复刻内核 noEscape 表：字母数字 - . _ ~ 不转义，其余 %XX 大写（按 UTF-8 字节）"""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        if b < 128 and c in _SAFE:
            out.append(c)
        else:
            out.append("%%%02X" % b)
    return "".join(out)


EMPTY_BODY_SHA256 = hashlib.sha256(b"").hexdigest()


def sign_request(method: str, url: str, headers: dict, ak: str, sk: str) -> dict:
    """
    对给定头集合做 SDK-HMAC-SHA256 签名，返回补齐后的完整头 dict。
    注意：
      - canonicalURI 每段 urlEncode 且末尾必加 /
      - payloadHash 取 headers["X-Sdk-Content-Sha256"]，没有则用空体 SHA256
      - POST chat 必须显式带 X-Sdk-Content-Sha256: UNSIGNED-PAYLOAD（实测不带会 401）
    """
    p = urllib.parse.urlsplit(url)
    dt = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    all_headers = dict(headers)
    all_headers["X-Sdk-Date"] = dt
    host = p.hostname
    if p.port and not ((p.scheme == "https" and p.port == 443) or (p.scheme == "http" and p.port == 80)):
        host = "%s:%d" % (host, p.port)
    all_headers["Host"] = host

    # canonicalURI
    uri = "/".join(_url_encode(seg) for seg in (p.path or "/").split("/"))
    if not uri.endswith("/"):
        uri += "/"

    # canonicalQueryString
    qs = ""
    if p.query:
        params = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        qs = "&".join("%s=%s" % (_url_encode(k), _url_encode(v)) for k, v in sorted(params))

    sorted_keys = sorted(all_headers.keys(), key=lambda x: x.lower())
    signed_names = ";".join(k.lower() for k in sorted_keys)
    canonical_headers = ""
    for k in sorted_keys:
        v = str(all_headers[k]).replace("\r", " ").replace("\n", " ").strip()
        canonical_headers += "%s:%s\n" % (k.lower(), v)

    payload_hash = all_headers.get("X-Sdk-Content-Sha256") or EMPTY_BODY_SHA256
    canonical_request = "\n".join([method.upper(), uri, qs, canonical_headers, signed_names, payload_hash])
    creq_hash = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "SDK-HMAC-SHA256\n%s\n%s" % (dt, creq_hash)
    signature = hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    all_headers["Authorization"] = "SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s" % (ak, signed_names, signature)
    return all_headers


def upstream_headers(session: dict) -> dict:
    """构造 chat 请求头并签名（复刻内核 getInferhubHcHeaders 的头集合 + UNSIGNED-PAYLOAD）"""
    nid = lambda: str(uuid.uuid4()).replace("-", "")
    h = {
        "X-Security-Token": session["securitytoken"],
        "X-Sdk-Content-Sha256": "UNSIGNED-PAYLOAD",   # 实测必需：POST 网关要求显式声明
        "x-ot-trace-id": nid(),
        "x-ot-span-id": nid(),
        "x-snap-traceid": nid(),
        "x-ot-session-id": _OT_SESSION_ID,  # 进程级复用：上游按 session id 计并发会话数
        "user-session-id": _USER_SESSION_ID,
        "X-Language": "zh-cn",
    }
    return sign_request("POST", UPSTREAM, h, session["ak"], session["sk"])


# 忽略系统代理（公司代理可能不支持直连回环/篡改头）
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 会话 id：进程级复用（上游按 x-ot-session-id 计并发会话数，每次随机新值会迅速
# 触发 TM.00001041 "并发会话数已达上限(3个)"；内核与 IDE 也是长会话复用同一 id）
_OT_SESSION_ID = str(uuid.uuid4()).replace("-", "")
_USER_SESSION_ID = str(uuid.uuid4()).replace("-", "")

# 会话 id：进程级复用（上游按 x-ot-session-id 计并发会话数，每次随机新值会迅速
# 触发 TM.00001041 "并发会话数已达上限(3个)"；内核与 IDE 也是长会话复用同一 id）
_OT_SESSION_ID = str(uuid.uuid4()).replace("-", "")
_USER_SESSION_ID = str(uuid.uuid4()).replace("-", "")


# ── 协议转换：Responses / Anthropic → OpenAI Chat 请求 ────────────────────
def _rand():
    return hashlib.md5(os.urandom(8)).hexdigest()[:24]


def convert_responses_to_openai(body: dict) -> dict:
    """/v1/responses 请求体 → OpenAI chat.completions 请求体。"""
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("max_output_tokens") is not None:
        oa["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oa["top_p"] = body["top_p"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("stop") is not None:
        oa["stop"] = body["stop"]
    if (body.get("reasoning") or {}).get("effort"):
        oa["reasoning_effort"] = body["reasoning"]["effort"]

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
                        elif isinstance(part, dict) and part.get("type") == "input_image":
                            text += "[图片]"
                if text:
                    msgs.append({"role": role, "content": text})
            elif t == "function_call":
                tc = {"id": item.get("call_id", ""), "type": "function",
                      "function": {"name": item.get("name", ""), "arguments": item.get("arguments", "{}")}}
                if msgs and msgs[-1].get("role") == "assistant" and "tool_calls" in msgs[-1]:
                    msgs[-1]["tool_calls"].append(tc)
                else:
                    msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
            elif t == "function_call_output":
                o = item.get("output")
                out_text = ""
                if isinstance(o, str):
                    out_text = o
                elif isinstance(o, list):
                    for part in o:
                        if isinstance(part, dict) and part.get("text"):
                            out_text += part["text"]
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": out_text})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]

    if isinstance(body.get("tools"), list):
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict) or t.get("type") != "function":
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            })
        if tools:
            oa["tools"] = tools
    if body.get("tool_choice") is not None:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "function" and tc.get("name"):
            oa["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        else:
            oa["tool_choice"] = tc
    return oa


def convert_anthropic_to_openai(body: dict) -> dict:
    """/v1/messages 请求体 → OpenAI chat.completions 请求体。"""
    oa = {}
    if body.get("model"):
        oa["model"] = body["model"]
    if body.get("max_tokens") is not None:
        oa["max_tokens"] = body["max_tokens"]
    if body.get("stream") is not None:
        oa["stream"] = body["stream"]
    if body.get("temperature") is not None:
        oa["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        oa["top_p"] = body["top_p"]
    if body.get("stop_sequences") is not None:
        oa["stop"] = body["stop_sequences"]
    if (body.get("thinking") or {}).get("type") == "enabled":
        oa.pop("temperature", None)
        oa["reasoning_effort"] = "high"

    msgs = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        sys_text = ""
        for part in sys_:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                sys_text += part["text"] + "\n"
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
                elif part.get("type") == "thinking":
                    pass
                elif part.get("type") == "tool_use":
                    tool_calls.append({
                        "id": part.get("id", ""),
                        "type": "function",
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
                    res = ""
                    if isinstance(rc, str):
                        res = rc
                    elif isinstance(rc, list):
                        for cc in rc:
                            if isinstance(cc, dict) and cc.get("type") == "text" and cc.get("text"):
                                res += cc["text"]
                    tmsg = {"role": "tool", "tool_call_id": part.get("tool_use_id", ""), "content": res}
                    if part.get("name"):
                        tmsg["name"] = part["name"]
                    msgs.append(tmsg)
            parts = []
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    parts.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "image":
                    src = part.get("source") or {}
                    if src.get("type") == "url" and src.get("url"):
                        parts.append({"type": "image_url", "image_url": {"url": src["url"]}})
                    elif src.get("data"):
                        parts.append({"type": "image_url", "image_url": {
                            "url": "data:%s;base64,%s" % (src.get("media_type", "image/png"), src["data"])}})
            if parts:
                msgs.append({"role": "user", "content": parts})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]

    if isinstance(body.get("tools"), list):
        tools = []
        for t in body["tools"]:
            if not isinstance(t, dict):
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        if tools:
            oa["tools"] = tools
    if body.get("tool_choice") is not None:
        tc = body["tool_choice"]
        if isinstance(tc, dict) and tc.get("type") == "tool" and tc.get("name"):
            oa["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        elif isinstance(tc, str):
            oa["tool_choice"] = "required" if tc == "any" else ("none" if tc == "none" else "auto")
    return oa


def parse_anthropic_text(body: dict) -> list:
    """从 Anthropic Messages 协议提取全部文本（count_tokens 本地估算用）。"""
    texts = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        texts.append(sys_)
    elif isinstance(sys_, list):
        for part in sys_:
            if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                texts.append(part["text"])
    for msg in body.get("messages") or []:
        c = msg.get("content")
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for part in c:
                if not isinstance(part, dict):
                    continue
                t = part.get("type")
                if t == "text" and part.get("text"):
                    texts.append(part["text"])
                elif t == "tool_use":
                    texts.append(json.dumps(part, ensure_ascii=False))
                elif t == "tool_result":
                    rc = part.get("content")
                    if isinstance(rc, str):
                        texts.append(rc)
                    elif isinstance(rc, list):
                        for c2 in rc:
                            if isinstance(c2, dict) and c2.get("type") == "text" and c2.get("text"):
                                texts.append(c2["text"])
    return texts


# ── 协议转换：OpenAI Chat 响应 → Responses / Anthropic ─────────────────────
def convert_openai_to_responses(openai_obj: dict, req_body: dict) -> dict:
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    output = []
    if message.get("reasoning_content"):
        output.append({"type": "reasoning", "id": "rs_" + _rand(),
                       "summary": [{"type": "summary_text", "text": message["reasoning_content"]}]})
    if message.get("content"):
        output.append({"type": "message", "id": "msg_" + _rand(), "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"], "annotations": []}]})
    for tc in message.get("tool_calls") or []:
        output.append({
            "type": "function_call", "id": "fc_" + _rand(), "call_id": tc.get("id", ""),
            "name": (tc.get("function") or {}).get("name", ""),
            "arguments": (tc.get("function") or {}).get("arguments", "{}"), "status": "completed",
        })
    incomplete = choice.get("finish_reason") == "length"
    return {
        "id": "resp_" + _rand(),
        "object": "response",
        "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": req_body.get("model", ""),
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
    }


def convert_openai_to_anthropic(openai_obj: dict, req_body: dict) -> dict:
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
    if finish == "stop":
        stop_reason = "end_turn"
    elif finish == "tool_calls":
        stop_reason = "tool_use"
    elif finish == "length":
        stop_reason = "max_tokens"
    else:
        stop_reason = finish or "end_turn"
    return {
        "id": "msg_" + _rand(),
        "type": "message",
        "role": "assistant",
        "model": req_body.get("model", ""),
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ── 流式状态机：上游 OpenAI chat SSE → Responses / Anthropic SSE ───────────
class _ResponsesStreamState:
    """上游 chat chunk → Responses 协议事件序列（codex-cli 依赖 response.completed 等）。"""

    def __init__(self, model: str):
        self.model = model
        self.resp_id = "resp_" + _rand()
        self.created = int(time.time())
        self.started = False
        self.finished = False
        self.finish_reason = None
        self.text = ""
        self.reasoning = ""
        self.tool_args = ""
        self.tool_name = ""
        self.tool_id = ""
        self.usage = None
        self.text_item_id = "msg_" + _rand()
        self.reason_item_id = "rs_" + _rand()
        self.reason_opened = False
        self.text_opened = False
        self.reason_index = None
        self.text_index = None
        self.next_output_index = 0

    def feed(self, chunk: dict):
        out = []
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        if not self.started:
            self.started = True
            out.append(("response.created", json.dumps({
                "type": "response.created",
                "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                             "status": "in_progress", "model": self.model, "output": []},
            }, ensure_ascii=False)))
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]

        rc = delta.get("reasoning_content")
        if rc:
            self.reasoning += rc
            if not self.reason_opened:
                self.reason_opened = True
                idx = self.next_output_index
                self.next_output_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": idx,
                    "item": {"type": "reasoning", "id": self.reason_item_id, "summary": []},
                }, ensure_ascii=False)))
                out.append(("response.reasoning_summary_part.added", json.dumps({
                    "type": "response.reasoning_summary_part.added", "item_id": self.reason_item_id,
                    "output_index": idx, "summary_index": 0, "part": {"type": "summary_text", "text": ""},
                }, ensure_ascii=False)))
                self.reason_index = idx
            out.append(("response.reasoning_summary_text.delta", json.dumps({
                "type": "response.reasoning_summary_text.delta", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "delta": rc,
            }, ensure_ascii=False)))

        content = delta.get("content")
        if content:
            self.text += content
            if not self.text_opened:
                self.text_opened = True
                idx = self.next_output_index
                self.next_output_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": idx,
                    "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                             "status": "in_progress", "content": []},
                }, ensure_ascii=False)))
                out.append(("response.content_part.added", json.dumps({
                    "type": "response.content_part.added", "item_id": self.text_item_id,
                    "output_index": idx, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }, ensure_ascii=False)))
                self.text_index = idx
            out.append(("response.output_text.delta", json.dumps({
                "type": "response.output_text.delta", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "delta": content,
            }, ensure_ascii=False)))

        if isinstance(delta, dict) and delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
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
            idx = self.reason_index
            out.append(("response.reasoning_summary_text.done", json.dumps({
                "type": "response.reasoning_summary_text.done", "item_id": self.reason_item_id,
                "output_index": idx, "summary_index": 0, "text": self.reasoning,
            }, ensure_ascii=False)))
            out.append(("response.reasoning_summary_part.done", json.dumps({
                "type": "response.reasoning_summary_part.done", "item_id": self.reason_item_id,
                "output_index": idx, "summary_index": 0,
                "part": {"type": "summary_text", "text": self.reasoning},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": idx,
                "item": {"type": "reasoning", "id": self.reason_item_id,
                         "summary": [{"type": "summary_text", "text": self.reasoning}]},
            }, ensure_ascii=False)))
            self.reason_opened = False
        if self.text_opened:
            idx = self.text_index
            out.append(("response.output_text.done", json.dumps({
                "type": "response.output_text.done", "item_id": self.text_item_id,
                "output_index": idx, "content_index": 0, "text": self.text,
            }, ensure_ascii=False)))
            out.append(("response.content_part.done", json.dumps({
                "type": "response.content_part.done", "item_id": self.text_item_id,
                "output_index": idx, "content_index": 0,
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": idx,
                "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": self.text, "annotations": []}]},
            }, ensure_ascii=False)))
            self.text_opened = False

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
                          "status": "completed", "content": [{"type": "output_text", "text": "", "annotations": []}]})
        incomplete = self.finish_reason == "length"
        usage = self.usage or {}
        out.append(("response.completed", json.dumps({
            "type": "response.completed",
            "response": {
                "id": self.resp_id, "object": "response", "created_at": self.created,
                "status": "incomplete" if incomplete else "completed",
                "model": self.model, "output": items,
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
                "error": None,
                "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
            },
        }, ensure_ascii=False)))
        return out


class _AnthropicStreamState:
    """上游 chat chunk → Anthropic Messages SSE 事件序列（message_start/stop）。"""

    def __init__(self, model: str):
        self.model = model
        self.msg_id = "msg_" + _rand()
        self.started = False
        self.finished = False
        self.text_block = False
        self.thinking_block = False
        self.block_index = 0
        self.text = ""
        self.thinking = ""
        self.tool_blocks = {}
        self.usage = None

    def _start(self, out):
        if self.started:
            return
        self.started = True
        out.append(("message_start", json.dumps({
            "type": "message_start",
            "message": {
                "id": self.msg_id, "type": "message", "role": "assistant", "content": [],
                "model": self.model, "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }, ensure_ascii=False)))

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
                self.thinking_block = True
                idx = self.block_index
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                }, ensure_ascii=False)))
            self.thinking += rc
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "thinking_delta", "thinking": rc},
            }, ensure_ascii=False)))
        if content:
            if self.thinking_block:
                out.append(("content_block_stop", json.dumps({
                    "type": "content_block_stop", "index": self.block_index - 1,
                }, ensure_ascii=False)))
                self.thinking_block = False
            if not self.text_block:
                self.text_block = True
                idx = self.block_index
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": idx,
                    "content_block": {"type": "text", "text": ""},
                }, ensure_ascii=False)))
            self.text += content
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "text_delta", "text": content},
            }, ensure_ascii=False)))
        if delta.get("tool_calls"):
            for tc in delta["tool_calls"]:
                fn = tc.get("function") or {}
                idx = tc.get("index", 0)
                if tc.get("id") or fn.get("name"):
                    if idx not in self.tool_blocks:
                        self.tool_blocks[idx] = {"id": tc.get("id", ""), "name": fn.get("name", ""), "args": ""}
                        self.block_index += 1
                        out.append(("content_block_start", json.dumps({
                            "type": "content_block_start", "index": self.block_index - 1,
                            "content_block": {"type": "tool_use", "id": tc.get("id", ""),
                                              "name": fn.get("name", ""), "input": {}},
                        }, ensure_ascii=False)))
                if fn.get("arguments"):
                    self.tool_blocks[idx]["args"] += fn["arguments"]
                    out.append(("content_block_delta", json.dumps({
                        "type": "content_block_delta", "index": self.block_index - 1,
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                    }, ensure_ascii=False)))
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        self._start(out)
        if self.thinking_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.thinking_block = False
        if self.text_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1}, ensure_ascii=False)))
            self.text_block = False
        for idx in self.tool_blocks:
            try:
                parsed = json.loads(self.tool_blocks[idx]["args"] or "{}")
            except Exception:
                parsed = {}
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - len(self.tool_blocks) + list(self.tool_blocks).index(idx)},
                ensure_ascii=False)))
        usage = self.usage or {}
        stop_reason = "tool_use" if self.tool_blocks else "end_turn"
        out.append(("message_delta", json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }, ensure_ascii=False)))
        out.append(("message_stop", json.dumps({"type": "message_stop"}, ensure_ascii=False)))
        return out


# ── HTTP Handler ──────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        _log("%s - %s" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
                    return self._json(200, {"ok": True, "account": s["account"],
                                            "credExp": s["expires_at"] or "unknown"})
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
                        "owned_by": "huawei-codearts",
                        "metadata": {"name": m["name"], "context_window": m["ctx"], "desc": m["desc"]},
                    } for mid, m in MODELS.items()],
                })
            return self._json(404, {"error": {"message": "支持: GET /v1/models, GET /health, POST /v1/chat/completions"}})
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/v1/messages/count_tokens":
            return self._handle_count_tokens()
        if path in ("/v1/messages",):
            return self._handle_chat("anthropic")
        if path in ("/v1/responses", "/responses"):
            return self._handle_chat("responses")
        if path in ("/v1/chat/completions", "/chat/completions"):
            return self._handle_chat("openai")
        return self._json(404, {"error": {"message": "支持: GET /v1/models, GET /health, POST /v1/chat/completions, POST /v1/responses, POST /v1/messages"}})

    def _handle_count_tokens(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            req = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})
        total_chars = sum(len(t) for t in parse_anthropic_text(req))
        return self._json(200, {"input_tokens": max(1, total_chars // 3)})

    def _handle_chat(self, protocol: str = "openai"):
        """
        protocol: 'openai' | 'responses' | 'anthropic'
        三种协议统一转成 OpenAI chat 发上游，响应再转回目标协议。
        """
        # 1) 读请求体
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            req_body = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json(400, {"error": {"message": "无效 JSON 请求体"}})

        # 2) 协议请求转换 → OpenAI chat
        req_model_name = req_body.get("model") or ""   # 客户端请求的原始名（响应回填用）
        if protocol == "responses":
            req = convert_responses_to_openai(req_body)
        elif protocol == "anthropic":
            req = convert_anthropic_to_openai(req_body)
        else:
            req = dict(req_body)

        # 3) 模型解析（别名 → 官方 id）
        model = req.get("model") or ""
        real = MODEL_ALIAS.get(model, model)
        if real not in MODELS:
            return self._json(404, {"error": {"message": "模型 %s 不存在。可用: %s" % (model, ", ".join(sorted(MODELS)))}})
        req["model"] = real

        # 4) 凭证 + 签名
        try:
            session = _read_session()
            headers = upstream_headers(session)
        except Exception as e:
            return self._json(500, {"error": {"message": str(e)}})

        payload = json.dumps(req, ensure_ascii=False).encode("utf-8")
        is_stream = bool(req.get("stream"))

        # 5) 转发上游
        r = urllib.request.Request(UPSTREAM, data=payload, method="POST")
        for k, v in headers.items():
            if k.lower() == "host":
                continue
            r.add_header(k, v)
        r.add_header("Content-Type", "application/json")

        try:
            resp = _opener.open(r, timeout=600)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            _log("上游 %s %s: %s" % (e.code, real, detail[:200]))
            return self._json(e.code, {"error": {"message": "上游 %s: %s" % (e.code, detail)}})
        except Exception as e:
            return self._json(502, {"error": {"message": "上游连接失败: %s" % e}})

        # 6a) 流式：SSE 按协议转换转发
        if is_stream:
            self.send_response(resp.status)
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

            buf = b""
            data_parts = []
            cur_event = ""
            final_sent = False

            def flush_event():
                nonlocal data_parts, cur_event, final_sent
                if not data_parts:
                    cur_event = ""
                    return
                data_text = "\n".join(data_parts)
                event_name = cur_event
                cur_event = ""
                data_parts = []
                if data_text == "[DONE]":
                    return
                try:
                    obj = json.loads(data_text)
                except Exception:
                    return
                if st is not None:
                    for ev, pl in st.feed(obj):
                        _write_sse(ev, pl)
                        if ev in ("response.completed", "message_stop"):
                            final_sent = True
                else:
                    _write_sse("", json.dumps(obj, ensure_ascii=False))
                    if obj.get("choices") and obj["choices"][0].get("finish_reason"):
                        _write_sse("", "[DONE]")
                        final_sent = True

            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        sline = line.decode("utf-8", "replace").rstrip("\r")
                        if sline == "":
                            flush_event()
                        elif sline.startswith(":"):
                            continue
                        else:
                            idx = sline.find(":")
                            field = sline[:idx] if idx != -1 else sline
                            value = sline[idx + 1:] if idx != -1 else ""
                            if value.startswith(" "):
                                value = value[1:]
                            if field == "event":
                                cur_event = value
                            elif field == "data":
                                data_parts.append(value)
                flush_event()
                if st is not None and not final_sent:
                    for ev, pl in st.finish():
                        _write_sse(ev, pl)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                _log("客户端提前断开 stream model=%s" % real)
            return

        # 6b) 非流式：整读。上游可能对非流式请求也返回 SSE 伪流式（单个 data: 帧包完整
        #     chat.completion），此时聚合成 JSON 再做协议转换，保证客户端兼容。
        data = resp.read()
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "event-stream" in ct:
            text = data.decode("utf-8", "replace")
            frames = [ln[len("data:"):].strip() for ln in text.splitlines()
                      if ln.startswith("data:") and ln.strip() != "data: [DONE]"]
            if frames:
                try:
                    merged = json.loads(frames[-1])
                    if merged.get("object") == "chat.completion" and len(frames) == 1:
                        data = json.dumps(merged, ensure_ascii=False).encode("utf-8")
                        ct = "application/json"
                except Exception:
                    pass

        # 协议响应转换（openai 直通；responses/anthropic 转换后返回）
        if protocol == "responses":
            try:
                out = convert_openai_to_responses(json.loads(data.decode("utf-8")),
                                                  {"model": req_model_name})
                return self._json(200, out)
            except Exception as e:
                return self._json(502, {"error": {"message": "转换响应失败: %s" % e}})
        if protocol == "anthropic":
            try:
                out = convert_openai_to_anthropic(json.loads(data.decode("utf-8")),
                                                   {"model": req_model_name})
                return self._json(200, out)
            except Exception as e:
                return self._json(502, {"error": {"message": "转换响应失败: %s" % e}})

        self.send_response(resp.status)
        self.send_header("Content-Type", ct or "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)


def main():
    start_writer()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _log("CodeArts 官方模型代理启动: http://127.0.0.1:%d （模型: %s）" % (PORT, ", ".join(sorted(MODELS))))
    print("CodeArts 官方模型代理已启动: http://127.0.0.1:%d" % PORT)
    print("模型: %s" % ", ".join(sorted(MODELS)))
    print("认证: 自动复用 CodeArts Agent 登录态（凭证过期时打开客户端刷新）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
