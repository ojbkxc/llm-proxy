# -*- coding: utf-8 -*-
"""
test_qoder_proxy.py — qoder_proxy.py 离线测试套件（不触网、不碰真实客户端）

覆盖：
  1. 纯函数：expiresAt 解析、SSE 信封解包、chunk 聚合、三协议请求/响应转换、流式状态机
  2. HTTP 端到端（mock 签名器 + mock 上游）：/health、/v1/models、/v1/chat/completions
     （流式 + 非流式）、/v1/messages、/v1/responses、/v1/messages/count_tokens
  3. 账号池：入池/去重/导出导入/移除、401 换号、429 冷却换号、5xx 退避重试、并发取号均匀、
     临期自动续期
  4. 边界：API Key 鉴权、body 超限 413、未知模型 404、上游错误帧 502
  5. 自动签到：到点计算、调度四边界、状态文件往返、单号异常隔离、开关与线程

运行：python test_qoder_proxy.py
约束：纯标准库，全部假数据（dt-fake / fake-uid…），不连真实网关、不读真实 auth.v1.dat。
"""
import http.server as _srv
import base64
import hashlib
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import qoder_admin as QA
import qoder_proxy as Q

TEST_KEY = "testkey123"
MAX_BODY = Q.MAX_BODY_BYTES

# 测试进程不许碰真东西：这两个路径在 import 时就按环境变量定死了，测试里再怎么打桩，
# 也总有活下来的 daemon 线程在 teardown 之后按"还原了的"全局再跑一轮 ——
# 那一轮拿的是真账号池、打的是真上游、写的就是真签到状态。
# 真凭据 + 真状态双保险：没显式指定就一律搬进只有本进程看得见的临时目录。
if not os.environ.get("QODER_PROXY_ACCOUNTS"):
    Q.ACCOUNTS_FILE = os.path.join(tempfile.mkdtemp(prefix="qp-sandbox-"), "accounts.json")
if not os.environ.get("QODER_PROXY_CHECKIN_STATE"):
    Q.CHECKIN_STATE_FILE = os.path.join(tempfile.mkdtemp(prefix="qp-sandbox-"), "checkin_state.json")

_results = []


def _record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" - " + str(detail)[:300]) if detail else ""))


def _stdout_of(fn):
    """抓 CLI 输出（授权链接必须打印到 stdout，失败原因走 stderr，两边都要能看到）。"""
    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        rc = fn()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, buf.getvalue()


# ── mock 上游（Qoder 网关替身）─────────────────────────────────────────────
class _MockUpstream:
    """按请求体里的关键词决定回什么帧，方便同一端口测多种场景。"""

    def __init__(self):
        self.received = []          # [(path, body_text, headers_dict)]
        self.force_status = None    # 设成 401 等可直接测错误分支
        self.status_queue = []      # 按请求次序逐个弹出；0/None 表示这次正常回 200
        self.retry_after = None     # 错误响应带上 Retry-After，测 429 冷却时长
        self.error_frames = False
        srv_mod = _srv

        class H(srv_mod.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                b = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                if code >= 400 and self.up.retry_after is not None:
                    self.send_header("Retry-After", str(self.up.retry_after))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _sse(self, inner_chunks, with_error=False):
                # 标准 OpenAI SSE：data:<chunk JSON>\n\n，以 data:[DONE]\n\n 结束
                lines = []
                for c in inner_chunks:
                    body = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    lines.append("data:%s\n\n" % body)
                if with_error:
                    lines.append('data:{"error":{"message":"boom"}}\n\n')
                lines.append('data:[DONE]\n\n')
                b = "".join(lines).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream;charset=UTF-8")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.startswith("/algo/api/v2/model/list"):
                    return self._json(_MODELS_FIXTURE)
                return self._json({"error": "no route"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8", "replace")
                self.up.received.append((self.path, raw, dict(self.headers)))
                st = self.up.status_queue.pop(0) if self.up.status_queue else None
                st = st or self.up.force_status
                if st:
                    return self._json({"message": "rejected"}, st)
                if "TOOL" in raw:
                    return self._sse(_tool_chunks(), with_error=self.up.error_frames)
                if "ERRFRAME" in raw:
                    return self._sse(_text_chunks("半截"), with_error=True)
                return self._sse(_text_chunks("你好，我是假模型"), with_error=self.up.error_frames)

        H.up = self
        # 线程版：代理会按 host 复用 keep-alive 连接，单线程 server 会被一条
        # 挂着的连接卡住（shutdown 永远等不到 handle_request 返回）。
        self.server = srv_mod.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


_MODELS_FIXTURE = {
    "chat": [
        {"key": "fkmodel", "format": "openai", "source": "system", "enable": True,
         "display_name": "Fake One", "is_vl": True, "is_reasoning": True, "is_default": True,
         "price_factor": 1.0, "max_input_tokens": 180000,
         "thinking_config": {"disabled": {"description": "Disable thinking"},
                             "enabled": {"description": "Enable thinking", "is_default": True,
                                         "efforts": {"low": {}, "high": {},
                                                     "max": {"is_default": True}}}},
         "context_config": {"200K": {"token_count": 200000, "is_default": True},
                            "1M": {"token_count": 1000000}}},
        {"key": "fktwo", "format": "openai", "source": "system", "enable": True,
         "display_name": "Fake Two Pro", "is_vl": False, "is_reasoning": False,
         "price_factor": 0.5, "max_input_tokens": 96000,
         "context_config": {"200K": {"token_count": 200000}}},
        {"key": "fkoff", "format": "openai", "source": "system", "enable": False,
         "display_name": "Fake Off", "max_input_tokens": 8000, "context_config": {}},
    ],
    "assistant": [],
}


def _chunk(**kw):
    base = {"created": 1790000000, "id": "chatcmpl-fake", "model": "auto",
            "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, }]}
    if "delta" in kw:
        base["choices"][0]["delta"] = kw.pop("delta")
    if "finish_reason" in kw:
        base["choices"][0]["finish_reason"] = kw.pop("finish_reason")
    base.update(kw)
    return base


def _text_chunks(text):
    half = max(1, len(text) // 2)
    return [
        _chunk(delta={"role": "assistant", "content": "", "reasoning_content": "想一想"}),
        _chunk(delta={"content": text[:half]}),
        _chunk(delta={"content": text[half:]}),
        _chunk(delta={}, finish_reason="stop",
               usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                      "completion_tokens_details": {"reasoning_tokens": 3}}),
    ]


def _tool_chunks():
    return [
        _chunk(delta={"role": "assistant", "content": ""}),
        _chunk(delta={"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                      "function": {"name": "get_", "arguments": ""}}]}),
        _chunk(delta={"tool_calls": [{"index": 0, "function": {"name": "time",
                                                                "arguments": "{\"z"}}]}),
        _chunk(delta={"tool_calls": [{"index": 0, "function": {"arguments": "one\":1}"}}]}),
        _chunk(delta={}, finish_reason="tool_calls",
               usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}),
    ]


# ── mock 签名器已移除（OpenAI 通道不需要签名）──────────────────────────────
class _FakeSigner:
    """签名 sidecar 替身：把签名请求映射到 mock 上游，并记录 params 供断言。"""

    def __init__(self, base):
        self.base = base
        self.calls = []
        self.fail_times = 0     # 前 N 次 request 直接抛异常，测错误路径

    def request(self, acct, params, timeout=60):
        self.calls.append({"acct": acct, "params": params})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise Q.SignerError("sidecar 崩了（测试用）")
        if params.get("infer"):
            body = json.dumps(params["body"], ensure_ascii=False)
            return {"url": self.base + "/algo/api/v2/service/pro/sse/agent_chat_generation"
                            "?FetchKeys=llm_model_result&Encode=1",
                    "headers": {"Content-Type": "application/json",
                                "Authorization": "Bearer COSY.fake"},
                    "body": body}
        return {"url": self.base + params.get("path", "/algo/api/v2/model/list"),
                "headers": {"Authorization": "Bearer COSY.fake"}, "body": None}

    def peek(self):
        return {"started": True, "node": "v-fake", "cosyVersion": "9.9.9",
                "bundle": True, "error": None}

    def info(self):
        return self.peek()


def _fake_account(label="uid-fake0001", token="dt-fake", exp=None):
    return {
        "account": label, "token": token, "refreshToken": "drt-fake-" + label,
        "expiresAt": "2099-01-01T00:00:00Z", "exp": exp or (time.time() + 86400 * 30),
        "refreshTokenExpiresAt": "2099-06-01T00:00:00Z",
        "uid": "fake-uid-" + label[-4:], "machineId": "fake-machine",
        "machineToken": "fake-machine-token", "machineType": "5", "machineCode": "",
        "cosyVersion": "9.9.9", "organizationId": "", "organizationTags": [],
        "dataPolicyAgreed": False, "session_seed": "fake-seed",
    }


class _FakeLoginProc:
    """sidecar --login 一次性模式的替身：stdout 是预设好的 JSON-lines。"""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.stdin = _NullPipe()
        self._rc = returncode

    def poll(self):
        return self._rc

    def wait(self, timeout=None):
        return self._rc

    def kill(self):
        pass

    def __getattr__(self, name):
        raise AssertionError("_cmd_login 不该用到 sidecar 的 .%s" % name)


class _NullPipe:
    def write(self, *a):
        pass

    def flush(self):
        pass


# ── 夹具：mock 上游 + 假池 + 假签名器 + 被测代理 ───────────────────────────
class _Env:
    def __init__(self, accounts=None):
        self.upstream = _MockUpstream()
        self.tmpdir = tempfile.mkdtemp(prefix="qoder-proxy-test-")
        self.accounts_file = os.path.join(self.tmpdir, "accounts.json")
        self.accounts = accounts if accounts is not None else [_fake_account()]
        with open(self.accounts_file, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "accounts": self.accounts}, f, ensure_ascii=False)
        self.signer = _FakeSigner(self.upstream.base)

        # 备份并打桩
        self.sleeps = []       # 记录退避第几次，测试不真等
        self._saved = {k: getattr(Q, k) for k in
                       ("ACCOUNTS_FILE", "GATEWAY", "OPENAPI", "API_KEY", "_signer",
                        "_backoff_sleep", "CHECKIN_STATE_FILE", "ADMIN_PASSWORD",
                        "ADMIN_KEYS_FILE", "MODEL_ROUTES_FILE", "_key_store", "_admin_api")}
        # 签到状态路径在 import 时按环境变量定死，不一起搬进临时目录的话，
        # 在服务器上跑测试会把真那轮的账（今天领没领到）写成测试池的 0/0。
        Q.ACCOUNTS_FILE = self.accounts_file
        Q.CHECKIN_STATE_FILE = os.path.join(self.tmpdir, "checkin_state.json")
        Q.GATEWAY = self.upstream.base
        Q.OPENAPI = self.upstream.base
        Q._signer = self.signer
        Q._backoff_sleep = lambda n: self.sleeps.append(n)
        # 管理面（qoder_admin 集成）也全部搬进临时目录：KeyStore 文件、模型路由
        # 文件、管理密码，避免测试在仓库目录落下 qoder_admin_keys.json 之类。
        Q.ADMIN_KEYS_FILE = os.path.join(self.tmpdir, "admin_keys.json")
        Q.MODEL_ROUTES_FILE = os.path.join(self.tmpdir, "model_routes.json")
        Q.ADMIN_PASSWORD = ""
        Q._key_store = QA.KeyStore(Q.ADMIN_KEYS_FILE)
        # intl 版 _admin_api 是工厂函数（每次现取），无需重置；cn 版曾是缓存对象
        Q._pool.update({"accounts": [], "mtime": 0.0})
        Q._rr = 0
        Q._models.update({"at": 0.0, "list": [], "by_id": {}, "alias": {}})
        Q._model_routes.update({"routes": {"mappings": {}, "aliases": {}}, "mtime": 0.0})
        with Q._usage_stats_lock:
            Q._usage_stats.clear()
        self._openapi_orig = Q._openapi_json
        self._clear_conns()

        self.server = Q.QuietServer(("127.0.0.1", 0), Q.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.upstream.stop()
        self._clear_conns()
        Q._openapi_json = self._openapi_orig
        for k, v in self._saved.items():
            setattr(Q, k, v)
        Q._pool.update({"accounts": [], "mtime": 0.0})
        Q._model_routes.update({"routes": {"mappings": {}, "aliases": {}}, "mtime": 0.0})
        with Q._usage_stats_lock:
            Q._usage_stats.clear()

    @staticmethod
    def _clear_conns():
        """丢掉代理里连到上一个 mock 端口的 keep-alive 连接（端口会被 OS 复用）。"""
        for conns in Q._conn_pools.values():
            for c in conns:
                try:
                    c.close()
                except Exception:
                    pass
        Q._conn_pools.clear()

    def patch_openapi(self, fn):
        Q._openapi_json = fn


def _get(url, headers=None, timeout=15):
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url, obj, headers=None, raw=None, timeout=20):
    data = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post_stream(url, obj, timeout=25):
    """流式请求，返回 (status, [事件/数据行列表])。"""
    code, raw = _post(url, obj, timeout=timeout)
    lines = []
    cur_event = ""
    for ln in raw.decode("utf-8", "replace").split("\n"):
        ln = ln.strip()
        if ln.startswith("event:"):
            cur_event = ln[6:].strip()
        elif ln.startswith("data:"):
            lines.append((cur_event, ln[5:].strip()))
            cur_event = ""
    return code, lines


def _adm_req(env, method, path, obj=None, headers=None, raw=None, timeout=15):
    """管理端任意 method 的 HTTP 请求 helper（GET/POST/PUT/PATCH/DELETE）。"""
    data = raw if raw is not None else (
        json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else None)
    req = urllib.request.Request(env.url + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ── 1. 纯函数 ──────────────────────────────────────────────────────────────
def test_parse_exp():
    ok = (Q._parse_exp("2026-11-15T13:44:15Z") == Q._parse_exp("2026-11-15T21:44:15+08:00")
          and Q._parse_exp("") is None and Q._parse_exp("garbage") is None
          and Q._parse_exp(1794750255) == 1794750255)
    _record("parse_exp", ok)


def test_unwrap_frame():
    env = json.dumps({"headers": {}, "body": json.dumps({"object": "chat.completion.chunk"}),
                      "statusCodeValue": 200})
    a = Q._unwrap_frame(env)
    b = Q._unwrap_frame("[DONE]")
    c = Q._unwrap_frame('{"statusCodeValue":500,"body":"oops"}')
    d = Q._unwrap_frame('{"choices":[{"delta":{"content":"x"}}]}')
    ok = (json.loads(a)["object"] == "chat.completion.chunk" and b == "[DONE]"
          and c and "error" in json.loads(c) and d and "choices" in json.loads(d))
    _record("unwrap_frame", ok, "a=%s b=%s c=%s d=%s" % (bool(a), b, bool(c), d))


def test_iter_upstream_split_boundaries():
    """上游 SSE 在任意字节边界被切开都不能丢帧。

    回归：早期实现 resp.read(4096) 后直接 split("\\n")，跨块的半截 data: 行会被当成
    完整帧解析失败而丢弃（真机表现为回复掉字、usage 归零），且按块 decode 会把
    多字节字符切成乱码。
    """
    texts = ["你好世界🙂 #%d" % i for i in range(6)]
    inner = [json.dumps({"choices": [{"index": 0, "delta": {"content": t}}]}, ensure_ascii=False)
             for t in texts]
    wire = b""
    for s in inner:
        env = json.dumps({"headers": {}, "body": s, "statusCodeValue": 200, "statusCode": "OK"},
                         ensure_ascii=False)
        wire += ("data:%s\n\n" % env).encode("utf-8")
    wire += b'data:{"headers":{},"body":"[DONE]","statusCodeValue":200}\n\n'

    class _ShortRead:
        def __init__(self, b, n):
            self.b, self.n, self.i = b, n, 0

        def read(self, k=-1):
            p = self.b[self.i:self.i + self.n]
            self.i += len(p)
            return p

    for stride in (1, 7, 13, 64, 255, 4096, len(wire)):
        got = list(Q._iter_upstream(_ShortRead(wire, stride)))
        out = [json.loads(g)["choices"][0]["delta"]["content"] for g in got]
        if out != texts:
            _record("iter_upstream_split_boundaries", False,
                    "stride=%d got=%d/%d %r" % (stride, len(out), len(texts), out[:2]))
            return
    _record("iter_upstream_split_boundaries", True, "strides=1,7,13,64,255,4096,all")


def test_aggregate_text_and_reasoning():
    frames = [json.dumps(c) for c in _text_chunks("你好世界")]
    out = Q.aggregate_sse(frames, "fkmodel")
    msg = out["choices"][0]["message"]
    ok = (out["object"] == "chat.completion" and msg["content"] == "你好世界"
          and msg["reasoning_content"] == "想一想"
          and out["choices"][0]["finish_reason"] == "stop"
          and out["usage"]["total_tokens"] == 18 and out["model"] == "auto")
    _record("aggregate_text_and_reasoning", ok, json.dumps(msg, ensure_ascii=False)[:120])


def test_aggregate_tool_calls():
    frames = [json.dumps(c) for c in _tool_chunks()]
    out = Q.aggregate_sse(frames, "fkmodel")
    tc = out["choices"][0]["message"]["tool_calls"][0]
    ok = (out["choices"][0]["finish_reason"] == "tool_calls" and tc["id"] == "call_1"
          and tc["function"]["name"] == "get_time"
          and json.loads(tc["function"]["arguments"]) == {"zone": 1})
    _record("aggregate_tool_calls", ok, json.dumps(tc, ensure_ascii=False)[:160])


def test_convert_responses_to_openai():
    body = {"model": "fkmodel", "instructions": "你是助手", "max_output_tokens": 64,
            "input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "北京几点"}]},
                      {"type": "function_call", "call_id": "c1", "name": "get_time",
                       "arguments": "{\"zone\":8}"},
                      {"type": "function_call_output", "call_id": "c1", "output": "12:00"}],
            "tools": [{"type": "function", "name": "get_time", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "get_time"}, "stream": True}
    oa = Q.convert_responses_to_openai(body)
    ok = oa["max_tokens"] == 64 and oa["stream"] is True
    roles = [m["role"] for m in oa["messages"]]
    ok = ok and roles == ["system", "user", "assistant", "tool"]
    ok = ok and oa["messages"][2]["tool_calls"][0]["function"]["name"] == "get_time"
    ok = ok and oa["tools"][0]["function"]["name"] == "get_time"
    ok = ok and oa["tool_choice"] == {"type": "function", "function": {"name": "get_time"}}
    _record("convert_responses_to_openai", ok, str(roles))


def test_convert_anthropic_to_openai():
    body = {"model": "fkmodel", "max_tokens": 32, "system": [{"type": "text", "text": "s1"}],
            "messages": [{"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "r"}]},
                {"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "x"}, {"type": "text", "text": "ok"},
                {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}]}],
            "tools": [{"name": "f", "input_schema": {"type": "object"}}],
            "tool_choice": "any"}
    oa = Q.convert_anthropic_to_openai(body)
    roles = [m["role"] for m in oa["messages"]]
    ok = (oa["max_tokens"] == 32 and roles[0] == "system" and "tool" in roles
          and oa["tool_choice"] == "required"
          and oa["tools"][0]["function"]["parameters"] == {"type": "object"}
          and oa["messages"][-1]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}')
    _record("convert_anthropic_to_openai", ok, str(roles))


def test_convert_openai_back():
    obj = {"choices": [{"message": {"role": "assistant", "content": "hi",
                                    "reasoning_content": "th",
                                    "tool_calls": [{"id": "c", "function":
                                        {"name": "f", "arguments": "{\"a\":1}"}}]},
                        "finish_reason": "tool_calls"}],
           "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}
    r = Q.convert_openai_to_responses(obj, {"model": "fkmodel"})
    a = Q.convert_openai_to_anthropic(obj, {"model": "fkmodel"})
    types = [o["type"] for o in r["output"]]
    ok = (types == ["reasoning", "message", "function_call"]
          and r["usage"]["input_tokens"] == 3
          and a["stop_reason"] == "tool_use"
          and [c["type"] for c in a["content"]] == ["thinking", "text", "tool_use"]
          and a["content"][2]["input"] == {"a": 1})
    _record("convert_openai_back", ok, str(types))


def test_stream_states_events():
    rs = Q._ResponsesStreamState("fkmodel")
    out = []
    for c in _text_chunks("hello there"):
        out += rs.feed(c)
    out += rs.finish()
    evs = [e for e, _ in out]
    txt = "".join(json.loads(p).get("delta", "") for e, p in out
                  if e == "response.output_text.delta")
    ok = (evs[0] == "response.created" and evs[1] == "response.in_progress"
          and evs[-1] == "response.completed"
          and txt == "hello there" and "response.output_item.added" in evs
          and "response.output_text.done" in evs
          and "response.content_part.done" in evs
          and "response.output_item.done" in evs
          and sum(1 for e in evs if e == "response.output_item.added")
              == 2  # reasoning 与 message 两个 item
          and "response.reasoning_summary_text.done" in evs
          and "response.output_text.delta" in evs
          and "response.reasoning_summary_text.delta" in evs
          and json.loads(out[-1][1]).get("response", {}).get("status") == "completed"
          and not any(e == "event: response.failed" for e, _ in out)
          and not any(b"event:" in p.encode() for _, p in out)
          and all(json.loads(p)["type"] == e for e, p in out))

    an = Q._AnthropicStreamState("fkmodel")
    out2 = []
    for c in _tool_chunks():
        out2 += an.feed(c)
    out2 += an.finish()
    evs2 = [e for e, _ in out2]
    md = [p for e, p in out2 if e == "message_delta"]
    ok = ok and evs2[0] == "message_start" and evs2[-1] == "message_stop"
    ok = ok and "content_block_start" in evs2 and "content_block_stop" in evs2
    ok = ok and md and json.loads(md[0])["delta"]["stop_reason"] == "tool_use"
    _record("stream_states_events", ok, "resp=%d anth=%d" % (len(evs), len(evs2)))


def test_parse_anthropic_text_and_count():
    body = {"system": "s", "messages": [{"role": "user", "content": "abcdefg"}]}
    ok = sum(len(t) for t in Q.parse_anthropic_text(body)) == 8
    _record("parse_anthropic_text", ok)


# ── 2. HTTP 端到端 ─────────────────────────────────────────────────────────
def test_http_health_and_models():
    env = _Env()
    try:
        code, raw = _get(env.url + "/health")
        h = json.loads(raw)
        code2, raw2 = _get(env.url + "/v1/models")
        ml = json.loads(raw2)
        ids = [m["id"] for m in ml["data"]]
        ok = (code == 200 and h["ok"] is True and h["account_count"] == 1
              and h["signer"]["cosyVersion"] == "9.9.9" and code2 == 200
              and ids == ["fkmodel", "fktwo", "fkoff"]
              and ml["data"][0]["context_window"] == 1000000
              and ml["data"][0]["name"] == "Fake One"
              and ml["data"][2]["metadata"]["enabled"] is False)
        _record("http_health_and_models", ok, str(ids))
    finally:
        env.close()


def test_http_chat_stream_openai():
    env = _Env()
    try:
        code, lines = _post_stream(env.url + "/v1/chat/completions",
                                   {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}],
                                    "stream": True})
        datas = [p for _, p in lines]
        chunks = [json.loads(d) for d in datas if d not in ("[DONE]", "")]
        content = "".join((c.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
                          for c in chunks)
        ok = (code == 200 and datas[-1] == "[DONE]" and content == "你好，我是假模型"
              and all(c.get("object") == "chat.completion.chunk" for c in chunks))
        # 上游请求体带上了 client 的 messages / model_config
        sent = json.loads(env.upstream.received[-1][1])
        ok = ok and sent["model_config"]["key"] == "fkmodel" and sent["stream"] is True
        ok = ok and sent["messages"][0]["content"] == "hi" and sent["business"]["type"]
        _record("http_chat_stream_openai", ok, "content=%s" % content)
    finally:
        env.close()


def test_http_chat_non_stream():
    env = _Env()
    try:
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        obj = json.loads(raw)
        ok = (code == 200 and obj["object"] == "chat.completion"
              and obj["choices"][0]["message"]["content"] == "你好，我是假模型"
              and obj["choices"][0]["message"]["reasoning_content"] == "想一想"
              and obj["usage"]["total_tokens"] == 18)
        _record("http_chat_non_stream", ok, str(obj.get("object")))
    finally:
        env.close()


def test_http_chat_tools():
    env = _Env()
    try:
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "TOOL now"}],
                           "tools": [{"type": "function", "function":
                                      {"name": "get_time", "parameters": {"type": "object"}}}]})
        obj = json.loads(raw)
        tc = obj["choices"][0]["message"]["tool_calls"][0]
        sent = json.loads(env.upstream.received[-1][1])
        ok = (code == 200 and obj["choices"][0]["finish_reason"] == "tool_calls"
              and tc["function"]["name"] == "get_time"
              and sent["tools"][0]["function"]["name"] == "get_time")
        _record("http_chat_tools", ok, json.dumps(tc, ensure_ascii=False)[:120])
    finally:
        env.close()


def test_http_messages_anthropic():
    env = _Env()
    try:
        body = {"model": "fkmodel", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}], "stream": True}
        code, lines = _post_stream(env.url + "/v1/messages", body)
        evs = [e for e, _ in lines]
        text = "".join(json.loads(p)["delta"]["text"] for e, p in lines
                       if e == "content_block_delta"
                       and json.loads(p).get("delta", {}).get("type") == "text_delta")
        sent = json.loads(env.upstream.received[-1][1])
        ok = (code == 200 and evs[0] == "message_start" and evs[-1] == "message_stop"
              and text == "你好，我是假模型" and sent["model_config"]["key"] == "fkmodel")
        _record("http_messages_anthropic_stream", ok, "text=%s evs=%d" % (text[:6], len(evs)))

        code2, raw2 = _post(env.url + "/v1/messages", dict(body, stream=False))
        o2 = json.loads(raw2)
        ok2 = (code2 == 200 and o2["type"] == "message"
               and o2["content"][0]["type"] == "thinking"
               and o2["content"][1]["text"] == "你好，我是假模型"
               and o2["usage"]["input_tokens"] == 11)
        _record("http_messages_anthropic_complete", ok2, json.dumps(o2, ensure_ascii=False)[:140])

        code3, raw3 = _post(env.url + "/v1/messages/count_tokens", body)
        ok3 = code3 == 200 and json.loads(raw3)["input_tokens"] >= 1
        _record("http_count_tokens", ok3, str(raw3[:60]))
    finally:
        env.close()


def test_http_responses_protocol():
    env = _Env()
    try:
        body = {"model": "fkmodel", "input": "hi", "stream": True,
                "instructions": "you are fake"}
        code, lines = _post_stream(env.url + "/v1/responses", body)
        evs = [e for e, _ in lines]
        text = "".join(json.loads(p)["delta"] for e, p in lines
                       if e == "response.output_text.delta")
        done = [p for e, p in lines if e == "response.completed"]
        sent = json.loads(env.upstream.received[-1][1])
        ok = (code == 200 and evs[0] == "response.created" and evs[-1] == "response.completed"
              and text == "你好，我是假模型" and json.loads(done[0])["response"]["status"] == "completed"
              and sent["model_config"]["key"] == "fkmodel"
              and sent["messages"][0]["role"] == "system")
        _record("http_responses_stream", ok, "evs=%d" % len(evs))

        code2, raw2 = _post(env.url + "/v1/responses", dict(body, stream=False))
        o2 = json.loads(raw2)
        types = [x["type"] for x in o2["output"]]
        ok2 = (code2 == 200 and o2["object"] == "response"
               and types == ["reasoning", "message"]
               and o2["usage"]["output_tokens"] == 7)
        _record("http_responses_complete", ok2, str(types))
    finally:
        env.close()


def test_http_thinking_depth():
    """思考深度：三协议入参 → 上游 reasoningEffort + parameters；按模型档位校验。"""
    env = _Env()
    try:
        def sent_chat(extra):
            body = {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]}
            body.update(extra)
            code, _ = _post(env.url + "/v1/chat/completions", body)
            return code, json.loads(env.upstream.received[-1][1])

        # 不发任何思考字段：与旧行为一致，不注入
        c, s = sent_chat({})
        ok = c == 200 and "reasoningEffort" not in s and "parameters" not in s

        # OpenAI reasoning_effort=high（fkmodel 档位 low/high/max）
        c, s = sent_chat({"reasoning_effort": "high"})
        ok = ok and c == 200 and s.get("reasoningEffort") == "high" \
            and s["parameters"]["reasoning_effort"] == "high" \
            and s["parameters"]["enable_thinking"] is True \
            and "reasoning_budget_tokens" not in s["parameters"]

        # 关思考：reasoning_effort=none → enable_thinking=false，不带预算
        c, s = sent_chat({"reasoning_effort": "none"})
        ok = ok and s.get("reasoningEffort") == "none" \
            and s["parameters"]["enable_thinking"] is False \
            and "reasoning_budget_tokens" not in s["parameters"]

        # OpenAI 新档 minimal → low；大小写与 disabled 别名
        c, s = sent_chat({"reasoning_effort": "MINIMAL"})
        ok = ok and s.get("reasoningEffort") == "low"
        c, s = sent_chat({"reasoning_effort": "off"})
        ok = ok and s.get("reasoningEffort") == "none"

        # Qwen 风格 thinking_budget → 反查档位 + 预算随参数上送
        c, s = sent_chat({"thinking_budget": 20000})
        ok = ok and s.get("reasoningEffort") == "high" \
            and s["parameters"]["reasoning_budget_tokens"] == 20000

        # 预算反查出的档位不被支持（medium）→ 就近调整到 low，预算撤销
        c, s = sent_chat({"thinking_budget": 3000})
        ok = ok and s.get("reasoningEffort") == "low" \
            and "reasoning_budget_tokens" not in s["parameters"]

        # 模型未声明的档位（xhigh）→ 按预算距离就近调整到 max
        c, s = sent_chat({"reasoning_effort": "xhigh"})
        ok = ok and c == 200 and s.get("reasoningEffort") == "max" \
            and s["parameters"]["enable_thinking"] is True

        # 模型没声明 thinking_config（fktwo）：原样透传
        c, s = sent_chat({"model": "fktwo", "reasoning_effort": "medium"})
        ok = ok and s.get("reasoningEffort") == "medium"

        # Anthropic thinking.enabled + budget → s_() 反查档位
        c, _ = _post(env.url + "/v1/messages",
                     {"model": "fkmodel", "max_tokens": 16,
                      "messages": [{"role": "user", "content": "hi"}],
                      "thinking": {"type": "enabled", "budget_tokens": 20000}})
        s = json.loads(env.upstream.received[-1][1])
        ok = ok and c == 200 and s.get("reasoningEffort") == "high" \
            and s["parameters"]["reasoning_budget_tokens"] == 20000 \
            and s["parameters"]["enable_thinking"] is True

        # Anthropic thinking.disabled
        c, _ = _post(env.url + "/v1/messages",
                     {"model": "fkmodel", "max_tokens": 16,
                      "messages": [{"role": "user", "content": "hi"}],
                      "thinking": {"type": "disabled"}})
        s = json.loads(env.upstream.received[-1][1])
        ok = ok and s.get("reasoningEffort") == "none" \
            and s["parameters"]["enable_thinking"] is False

        # Responses reasoning.effort
        c, _ = _post(env.url + "/v1/responses",
                     {"model": "fkmodel", "input": "hi",
                      "reasoning": {"effort": "low"}})
        s = json.loads(env.upstream.received[-1][1])
        ok = ok and c == 200 and s.get("reasoningEffort") == "low" \
            and s["parameters"]["enable_thinking"] is True

        # /v1/models 透出 thinking_config，客户端可据此渲染选择器
        code, raw = _get(env.url + "/v1/models")
        m0 = json.loads(raw)["data"][0]
        tc = m0["metadata"]["thinking_config"]
        ok = ok and code == 200 and set(tc["enabled"]["efforts"]) == {"low", "high", "max"} \
            and "disabled" in tc
        _record("http_thinking_depth", ok, "")
    finally:
        env.close()


def test_api_key_enforcement():
    env = _Env()
    try:
        Q.API_KEY = TEST_KEY
        c1, _ = _get(env.url + "/v1/models")
        c2, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer wrong"})
        c3, r3 = _get(env.url + "/v1/models", {"Authorization": "Bearer " + TEST_KEY})
        c4, _ = _get(env.url + "/v1/models", {"X-Api-Key": TEST_KEY})
        c5, _ = _post(env.url + "/v1/chat/completions",
                      {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        c6, _ = _post(env.url + "/v1/chat/completions",
                      {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]},
                      {"Authorization": "Bearer " + TEST_KEY})
        ok = (c1 == 401 and c2 == 401 and c3 == 200 and c4 == 200 and c5 == 401 and c6 == 200)
        _record("api_key_enforcement", ok, "%s %s %s %s %s %s" % (c1, c2, c3, c4, c5, c6))
    finally:
        Q.API_KEY = ""
        env.close()


def test_body_oversize_413():
    env = _Env()
    try:
        big = json.dumps({"model": "fkmodel", "messages":
                          [{"role": "user", "content": "x" * (MAX_BODY + 10)}]}).encode("utf-8")
        req = urllib.request.Request(env.url + "/v1/chat/completions", data=big, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        _record("body_oversize_413", code == 413, "HTTP %s" % code)
    finally:
        env.close()


def test_unknown_model_404():
    env = _Env()
    try:
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]})
        ok = code == 404 and b"no-such-model" in raw
        _record("unknown_model_404", ok, "%s %s" % (code, raw[:80]))
    finally:
        env.close()


def test_bad_json_400():
    env = _Env()
    try:
        code, _ = _post(env.url + "/v1/chat/completions", None, raw=b"{not json")
        code2, _ = _post(env.url + "/v1/chat/completions", {"model": "fkmodel"})
        _record("bad_request_400", code == 400 and code2 == 400, "%s %s" % (code, code2))
    finally:
        env.close()


def test_upstream_401_switch_account():
    a = _fake_account("uid-aaaa1111", token="dt-bad")
    b = _fake_account("uid-bbbb2222", token="dt-good")
    env = _Env(accounts=[a, b])
    try:
        # 第一次请求上游回 401，之后恢复正常：验证换号后成功
        env.upstream.force_status = 401
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        first_bad = code == 401
        env.upstream.force_status = None
        code2, raw2 = _post(env.url + "/v1/chat/completions",
                            {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        marked = [x for x in pool if x["account"] == "uid-aaaa1111"]
        ok = (first_bad and code2 == 200 and json.loads(raw2)["choices"][0]["message"]["content"]
              and marked and marked[0]["exp"] < time.time())
        _record("upstream_401_switch_account", ok, "%s %s" % (code, code2))
    finally:
        env.close()


def test_upstream_429_cools_account_and_switches():
    """429：按 Retry-After 冷却挨限流的号，并换下一个号把这一轮对话救回来。"""
    a = _fake_account("uid-rate0001", token="dt-limited")
    b = _fake_account("uid-rate0002", token="dt-free")
    env = _Env(accounts=[a, b])
    try:
        env.upstream.retry_after = 7
        env.upstream.status_queue = [429, 0]
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        cooled = [x for x in pool if x.get("cool_until", 0) > time.time()]
        left = cooled[0]["cool_until"] - time.time() if cooled else -1
        ok = (code == 200 and len(cooled) == 1 and cooled[0]["failed_status"] == 429
              and 5 <= left <= 7 and len(env.upstream.received) == 2 and not env.sleeps)
        _record("upstream_429_cools_account_and_switches", ok,
                "%s cooled=%d left=%.1f hits=%d" % (code, len(cooled), left,
                                                    len(env.upstream.received)))
    finally:
        env.close()


def test_upstream_429_all_cooling_returns_429():
    """所有号都被限流：客户端拿到 429（而不是被误报成 401 或 502）。"""
    env = _Env(accounts=[_fake_account("uid-rate0001"), _fake_account("uid-rate0002")])
    try:
        env.upstream.retry_after = 30
        env.upstream.force_status = 429
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        cooled = [x for x in pool if x.get("cool_until", 0) > time.time()]
        hcode, hraw = _get(env.url + "/health")
        shown = json.loads(hraw)["accounts"][0].get("cooling_sec", 0) > 0
        ok = code == 429 and "限流" in raw.decode("utf-8", "replace") \
            and len(cooled) == 2 and shown
        _record("upstream_429_all_cooling_returns_429", ok,
                "%s cooled=%d health_cooling=%s" % (code, len(cooled), shown))
    finally:
        env.close()


def test_upstream_503_retried_then_success():
    """502/503/504 退避重试；重试不吃换号预算。"""
    env = _Env()
    try:
        env.upstream.status_queue = [503, 504, 0]
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        ok = (code == 200 and len(env.upstream.received) == 3 and env.sleeps == [1, 2]
              and json.loads(raw)["choices"][0]["message"]["content"])
        _record("upstream_503_retried_then_success", ok,
                "%s hits=%d sleeps=%s" % (code, len(env.upstream.received), env.sleeps))
    finally:
        env.close()


def test_upstream_retry_exhausted_502():
    """超过 RETRY_MAX 还是 5xx：原样回 502，不无限重试。"""
    env = _Env()
    try:
        env.upstream.force_status = 502
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        ok = code == 502 and len(env.upstream.received) == Q.RETRY_MAX + 1
        _record("upstream_retry_exhausted_502", ok,
                "%s hits=%d" % (code, len(env.upstream.received)))
    finally:
        env.close()


def test_upstream_500_not_retried():
    """上游 500 不重试：它可能已经跑完这一轮并扣了额度，重放等于二次消费。"""
    env = _Env()
    try:
        env.upstream.status_queue = [500, 0]
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        ok = code == 502 and len(env.upstream.received) == 1 and not env.sleeps
        _record("upstream_500_not_retried", ok,
                "%s hits=%d sleeps=%s" % (code, len(env.upstream.received), env.sleeps))
    finally:
        env.close()


def test_next_account_thread_safe_spread():
    """并发取号：_rr 自增在锁内，60 次调用必须均匀摊到 3 个号（各 20 次）。"""
    accs = [_fake_account("uid-rr000%d" % i) for i in range(3)]
    env = _Env(accounts=accs)
    try:
        picked = []
        lock = threading.Lock()

        def worker():
            for _ in range(20):
                a = Q._next_account()
                with lock:
                    picked.append(a["account"])

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        counts = {a["account"]: picked.count(a["account"]) for a in accs}
        ok = len(picked) == 60 and all(v == 20 for v in counts.values())
        _record("next_account_thread_safe_spread", ok, str(counts))
    finally:
        env.close()


def test_upstream_error_frame_502():
    env = _Env()
    try:
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "ERRFRAME"}]})
        ok = code == 502 and b"boom" in raw
        _record("upstream_error_frame_502", ok, "%s %s" % (code, raw[:120]))
    finally:
        env.close()




def test_signer_failure_502():
    env = _Env()
    try:
        env.signer.fail_times = 1
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}],
                           "stream": True})
        ok = code == 502
        # request 抛异常 → _signed_send 里 sidecar 的重试由 _Signer 负责；这里 fake 直接抛，
        # _open_upstream 的 except Exception 兜住 → 502
        _record("signer_failure_502", ok, "%s %s" % (code, raw[:100]))
    finally:
        env.close()


def test_pool_capture_export_import():
    env = _Env(accounts=[])
    try:
        Q._add_session_to_pool(_fake_account("uid-pool1"))
        Q._add_session_to_pool(_fake_account("uid-pool1", token="dt-refreshed"))  # 同标签覆盖
        Q._add_session_to_pool(_fake_account("uid-pool2"))
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        ok = len(pool) == 2 and pool[0]["token"] == "dt-refreshed"
        exp_path = os.path.join(env.tmpdir, "export.json")
        rc = Q._cmd_export(exp_path)
        ok = ok and rc == 0
        # 清空后导入
        Q._pool["accounts"] = []
        Q._pool["mtime"] = 0.0
        os.remove(env.accounts_file)
        rc2 = Q._cmd_import(exp_path)
        with open(env.accounts_file, encoding="utf-8") as f:
            pool2 = json.load(f)["accounts"]
        ok = ok and rc2 == 0 and len(pool2) == 2
        rc3 = Q._cmd_remove("uid-pool1")
        with open(env.accounts_file, encoding="utf-8") as f:
            pool3 = json.load(f)["accounts"]
        ok = ok and rc3 == 0 and len(pool3) == 1 and pool3[0]["account"] == "uid-pool2"
        _record("pool_capture_export_import", ok, "n=%d/%d" % (len(pool), len(pool3)))
    finally:
        env.close()


def test_auto_refresh():
    accts = [_fake_account("uid-ref001", exp=time.time() + 60)]   # 1 分钟后过期 → 触发续期
    env = _Env(accounts=accts)
    calls = []

    def fake_openapi(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        calls.append((method, path, body))
        return 200, {"device_token": "dt-refreshed", "refresh_token": "drt-rotated",
                     "expires_at": "2099-01-02T00:00:00Z",
                     "refresh_token_expires_at": "2099-06-02T00:00:00Z"}

    try:
        env.patch_openapi(fake_openapi)
        acct = Q._read_session()
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        ok = (acct["token"] == "dt-refreshed" and pool[0]["refreshToken"] == "drt-rotated"
              and calls and calls[0][0] == "POST" and calls[0][1] == Q.REFRESH_PATH
              and calls[0][2]["refresh_token"] == "drt-fake-uid-ref001"
              and pool[0]["exp"] > time.time() + 86400 * 300)
        _record("auto_refresh_on_near_expiry", ok, str(calls[:1])[:120])

        # 续期链死亡：401 → 标 refresh_dead，不再反复尝试
        calls2 = []

        def dead_openapi(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
            calls2.append(path)
            return 401, {"message": "invalid refresh token"}

        env.patch_openapi(dead_openapi)
        a2 = _fake_account("uid-dead01", exp=time.time() + 30)
        Q._add_session_to_pool(a2)
        Q._read_session()
        with open(env.accounts_file, encoding="utf-8") as f:
            pool2 = json.load(f)["accounts"]
        dead = [x for x in pool2 if x["account"] == "uid-dead01"][0]
        n_before = len(calls2)
        Q._read_session()
        ok2 = dead.get("refresh_dead") is True and len(calls2) <= n_before + 1
        _record("refresh_dead_marked", ok2, "calls=%d" % len(calls2))
    finally:
        env.close()


def test_refresh_single_flight_per_account():
    """同一个号的 rotation 同时只允许一次：refresh_token 用一次即作废，
    并发双刷必有一败，败方会被判成"续期链已死"。"""
    acct = _fake_account("uid-race01", exp=time.time() + 60)
    env = _Env(accounts=[acct])
    lock = threading.Lock()
    live = []
    peak = [0]
    done = []

    def fake_openapi(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        with lock:
            live.append(1)
            peak[0] = max(peak[0], len(live))
        time.sleep(0.2)                     # 把并发窗口拉宽，让没有单飞锁时一定撞车
        with lock:
            live.pop()
            done.append(path)
        return 200, {"device_token": "dt-raced", "refresh_token": "drt-raced",
                     "expires_at": "2099-01-02T00:00:00Z"}

    try:
        env.patch_openapi(fake_openapi)
        ts = [threading.Thread(target=Q._refresh_account, args=(dict(acct),)) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ok = peak[0] == 1 and len(done) == 1
        _record("refresh_single_flight_per_account", ok, "峰值并发=%d 实际刷新=%d" % (peak[0], len(done)))
    finally:
        env.close()


def test_sweep_guard_is_atomic():
    """"距上次巡检够久了没" 的判断和 "记一下这次扫了" 必须在同一把锁里。
    用 _SlowList 把"读旧值"这一步放大成 50ms，让没加锁的写法一定被撞出多次全池枚举。"""
    accts = [_fake_account("uid-sw%d" % i, exp=time.time() + 60) for i in range(3)]
    env = _Env(accounts=accts)
    seen = []
    refreshes = []
    orig_cool, orig_sweep_state = Q._acct_cooling, Q._last_sweep

    class _SlowList(list):
        def __getitem__(self, i):
            v = list.__getitem__(self, i)
            time.sleep(0.05)
            return v

    def counting(a, now):
        seen.append(a.get("account"))
        return orig_cool(a, now)

    def fake_openapi(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        refreshes.append(path)
        return 200, {"device_token": "dt-swept", "refresh_token": "drt-swept",
                     "expires_at": "2099-01-02T00:00:00Z"}

    try:
        Q._acct_cooling = counting
        env.patch_openapi(fake_openapi)
        Q._last_sweep = _SlowList([0.0])
        ts = [threading.Thread(target=Q._sweep_due) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        ok = len(seen) == 3 and len(refreshes) == 3
        _record("sweep_guard_is_atomic", ok, "枚举=%d 刷新=%d" % (len(seen), len(refreshes)))
    finally:
        Q._acct_cooling = orig_cool
        Q._last_sweep = orig_sweep_state
        env.close()


def test_env_isolates_checkin_state_file():
    """两层保险都得在：`_Env` 把状态搬进自己的临时目录，import 时又把默认路径换成进程私有沙箱。
    漏一层的代价不是断言失败，而是真那份"今天领到几个号"的账被写成测试池的 0/0。"""
    user_default = os.path.join(os.path.expanduser("~"), ".qoder-cn-proxy", "checkin_state.json")
    before = open(user_default, encoding="utf-8").read() if os.path.exists(user_default) else None
    env = _Env()
    try:
        inside = os.path.dirname(Q.CHECKIN_STATE_FILE) == env.tmpdir
        sandboxed = Q.CHECKIN_STATE_FILE != user_default
        Q._write_checkin_state({"date": "2099-01-01", "attempts": 1, "done": True})
        after = open(user_default, encoding="utf-8").read() if os.path.exists(user_default) else None
        _record("env_isolates_checkin_state_file", inside and sandboxed and after == before,
                "_Env=%s 沙箱=%s 真文件(%s)未变=%s" % (inside, sandboxed, user_default, after == before))
    finally:
        env.close()


def test_pool_write_keeps_file_secret():
    """自动续期写回的池必须还是 0600：真 token 不能因为默认 umask 每次续期就变 644。"""
    env = _Env(accounts=[_fake_account("uid-mode01")])
    try:
        with Q._pool_lock:
            Q._save_pool_locked()
        if os.name == "posix":
            mode = oct(os.stat(env.accounts_file).st_mode & 0o777)
            _record("pool_write_keeps_file_secret", mode == "0o600", "mode=%s" % mode)
        else:
            src = inspect.getsource(Q._save_pool_locked)
            _record("pool_write_keeps_file_secret", "0o600" in src and "os.open" in src,
                    "Windows 没有 POSIX 权限位，改为源码核对 0o600/os.open")
    finally:
        env.close()


# ── 多 Key 鉴权 + 管理面板 + 模型映射（qoder_admin 集成）────────────────────

def test_multi_key_auth():
    """KeyStore 多 Key 数据面鉴权：命中 200 / 错 Key 401 / 禁用 401 / 限额 429 / reset 恢复。"""
    env = _Env()
    try:
        ks = Q._key_store
        row = ks.create("多Key一号", {"key": "sk-multi-0001"})
        kv = row["key_value"]
        c1, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + kv})
        c2, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer sk-wrong"})
        c3, _ = _get(env.url + "/v1/models")
        c4, _ = _get(env.url + "/v1/models", {"X-Api-Key": kv})
        _record("multi_key_auth_basic",
                c1 == 200 and c2 == 401 and c3 == 401 and c4 == 200,
                "%s %s %s %s" % (c1, c2, c3, c4))
        # 禁用 → 401；重新启用 → 200
        ks.patch(row["id"], {"enabled": 0})
        c5, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + kv})
        ks.patch(row["id"], {"enabled": 1})
        c6, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + kv})
        _record("multi_key_auth_disable", c5 == 401 and c6 == 200, "%s %s" % (c5, c6))
        # 限额：reset 后 max_calls=1 → 第一次 200、第二次 429；再 reset 恢复 200
        ks.reset_usage(row["id"])
        ks.patch(row["id"], {"max_calls": 1})
        c7, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + kv})
        c8, r8 = _get(env.url + "/v1/models", {"Authorization": "Bearer " + kv})
        ks.reset_usage(row["id"])
        c9, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + kv})
        _record("multi_key_auth_quota",
                c7 == 200 and c8 == 429 and c9 == 200 and b"rate_limit_exceeded" in r8,
                "%s %s %s body=%s" % (c7, c8, c9, r8[:80]))
    finally:
        env.close()


def test_keystore_precedence_over_single_key():
    """KeyStore 有 key 时旧单 Key（QODER_PROXY_API_KEY）不再放行——多 Key 优先。"""
    env = _Env()
    try:
        Q.API_KEY = TEST_KEY
        ks = Q._key_store
        row = ks.create("优先级", {"key": "sk-prec-0001"})
        c1, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + TEST_KEY})
        c2, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer " + row["key_value"]})
        _record("keystore_precedence_over_single_key",
                c1 == 401 and c2 == 200, "%s %s" % (c1, c2))
    finally:
        Q.API_KEY = ""
        env.close()


def test_admin_page_and_login():
    """/admin 页面免 Bearer；密码未配置管理接口全 403；登录对/错、Bearer 对/错/缺。"""
    env = _Env()
    try:
        c1, raw1 = _get(env.url + "/admin")
        c2, raw2 = _get(env.url + "/admin/")
        c3, _ = _adm_req(env, "GET", "/api/keys")
        c4, _ = _adm_req(env, "POST", "/api/auth/login", {"password": "whatever"})
        ok_page = (c1 == 200 and c2 == 200 and b"<html" in raw1.lower()
                   and b"Qoder Admin" in raw1)
        ok_nopw = c3 == 403 and c4 == 403
        Q.ADMIN_PASSWORD = "adm-pw-1"
        H = {"Authorization": "Bearer adm-pw-1"}
        c5, _ = _adm_req(env, "POST", "/api/auth/login", {"password": "adm-pw-1"})
        c6, _ = _adm_req(env, "POST", "/api/auth/login", {"password": "bad"})
        c7, raw7 = _adm_req(env, "GET", "/api/keys", headers=H)
        c8, _ = _adm_req(env, "GET", "/api/keys")
        c9, _ = _adm_req(env, "GET", "/api/keys", {"Authorization": "Bearer bad"})
        ok_auth = (c5 == 200 and c6 == 401 and c7 == 200 and c8 == 401 and c9 == 401
                   and json.loads(raw7) == {"keys": []})
        _record("admin_page_and_login", ok_page and ok_nopw and ok_auth,
                "%s %s %s | %s %s %s %s %s %s" % (c1, c2, c3, c4, c5, c6, c7, c8, c9))
    finally:
        Q.ADMIN_PASSWORD = ""
        env.close()


def test_admin_keys_crud():
    """/api/keys CRUD（带 Bearer 管理密码），管理面改动立即作用于数据面鉴权。"""
    env = _Env()
    try:
        Q.ADMIN_PASSWORD = "adm-pw-2"
        H = {"Authorization": "Bearer adm-pw-2"}
        c1, raw1 = _adm_req(env, "POST", "/api/keys",
                            {"name": "key1", "key": "sk-admin-0001"}, headers=H)
        row = json.loads(raw1)
        kid = row["id"]
        ok1 = c1 == 200 and row["key_value"] == "sk-admin-0001" and row["enabled"] == 1
        c2, raw2 = _adm_req(env, "GET", "/api/keys", headers=H)
        ok2 = c2 == 200 and json.loads(raw2)["keys"][0]["name"] == "key1"
        c3, raw3 = _adm_req(env, "PATCH", "/api/keys/" + kid, {"enabled": 0}, headers=H)
        ok3 = c3 == 200 and json.loads(raw3)["enabled"] == 0
        c4, _ = _adm_req(env, "PATCH", "/api/keys/no-such-id", {"enabled": 1}, headers=H)
        c5, raw5 = _adm_req(env, "POST", "/api/keys/%s/reset" % kid, headers=H)
        ok5 = c5 == 200 and json.loads(raw5) == {"ok": True, "used": 0}
        # 管理面禁用后，数据面立即 401
        c6, _ = _get(env.url + "/v1/models", {"Authorization": "Bearer sk-admin-0001"})
        c7, raw7 = _adm_req(env, "DELETE", "/api/keys/" + kid, headers=H)
        c8, _ = _adm_req(env, "DELETE", "/api/keys/" + kid, headers=H)
        _record("admin_keys_crud",
                ok1 and ok2 and ok3 and c4 == 404 and ok5 and c6 == 401
                and c7 == 200 and json.loads(raw7) == {"ok": True} and c8 == 404,
                "%s %s %s %s %s %s %s %s" % (c1, c2, c3, c4, c5, c6, c7, c8))
    finally:
        Q.ADMIN_PASSWORD = ""
        env.close()


def test_admin_accounts_adapters():
    """账号适配钩子：list/checkin/test/refresh/patch/import/delete/stats/models 全链路（mock 上游）。"""
    env = _Env()
    try:
        Q.ADMIN_PASSWORD = "adm-pw-3"
        H = {"Authorization": "Bearer adm-pw-3"}
        # list：假池 1 个号（uid-fake0001 的 uid=fake-uid-0001 → 管理端 id "fake-uid"）
        c1, raw1 = _adm_req(env, "GET", "/api/accounts", headers=H)
        obj1 = json.loads(raw1)
        acc = obj1["accounts"][0] if obj1.get("accounts") else {}
        ok1 = (c1 == 200 and len(obj1.get("accounts") or []) == 1
               and acc.get("id") == "fake-uid" and acc.get("label") == "uid-fake0001"
               and acc.get("has_refresh") is True and acc.get("expires_at")
               and isinstance(obj1.get("checkins"), list))
        # 签到：打桩 campaigns（先 CLAIMABLE，claim 之后 CLAIMED）
        claimed = {"done": False}

        def fake_openapi(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
            if path.startswith(Q.CAMPAIGNS_PATH):
                if method == "POST":
                    claimed["done"] = True
                    return 200, {"grantId": "g-1", "claimedAt": "2026-10-21T10:00:00Z",
                                 "grantedAt": "2026-10-21T10:00:00Z"}
                status = "CLAIMED" if claimed["done"] else "CLAIMABLE"
                return 200, {"campaigns": [{
                    "campaignId": "camp-1", "campaignKey": "daily_credits",
                    "actionType": "CLAIM_BENEFIT", "claimStatus": status,
                    "benefit": {"kind": "CREDITS", "amount": 100}}],
                    "claimable": not claimed["done"]}
            if path == Q.USAGE_PATH:
                return 200, {"qoderUsage": {"addOnQuota": {"total": 100}}}
            return 404, {"message": "no route"}

        env.patch_openapi(fake_openapi)
        c2, raw2 = _adm_req(env, "POST", "/api/accounts/fake-uid/checkin", headers=H)
        obj2 = json.loads(raw2)
        ok2 = (c2 == 200 and obj2.get("ok") is True
               and obj2.get("rep", {}).get("secured") is True
               and obj2["rep"]["claimed"][0]["status"] == 200)
        c3, _ = _adm_req(env, "POST", "/api/accounts/no-such/checkin", headers=H)
        # 测试连通（走 mock 上游模型清单）
        c4, raw4 = _adm_req(env, "POST", "/api/accounts/fake-uid/test", headers=H)
        obj4 = json.loads(raw4)
        ok4 = c4 == 200 and obj4.get("ok") is True and obj4.get("models") == 3
        # 续期：打桩 deviceToken/refresh
        def fake_refresh(method, path, body=None, token="", timeout=20, headers=None):
            if path == Q.REFRESH_PATH:
                return 200, {"device_token": "dt-refreshed",
                             "refresh_token": "drt-refreshed",
                             "expires_at": "2099-01-01T00:00:00Z",
                             "refresh_token_expires_at": "2099-06-01T00:00:00Z"}
            return fake_openapi(method, path, body, token, timeout, headers)

        env.patch_openapi(fake_refresh)
        c5, raw5 = _adm_req(env, "POST", "/api/accounts/fake-uid/refresh", headers=H)
        obj5 = json.loads(raw5)
        ok5 = c5 == 200 and obj5.get("ok") is True \
            and obj5.get("expires_at") == "2099-01-01T00:00:00Z"
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        ok5b = any(a.get("token") == "dt-refreshed" for a in pool)
        # patch 改 label
        c6, raw6 = _adm_req(env, "PATCH", "/api/accounts/fake-uid",
                            {"label": "renamed-acct"}, headers=H)
        obj6 = json.loads(raw6)
        ok6 = c6 == 200 and obj6.get("label") == "renamed-acct"
        c6b, _ = _adm_req(env, "PATCH", "/api/accounts/zz", {"label": "x"}, headers=H)
        # import：token 必填，缺 token 记 skipped
        c7, raw7 = _adm_req(env, "POST", "/api/accounts/import",
                            {"accounts": [{"token": "dt-imported", "uid": "imported-uid-42",
                                           "expiresAt": "2099-01-01T00:00:00Z"}]},
                            headers=H)
        obj7 = json.loads(raw7)
        ok7 = c7 == 200 and obj7.get("imported") == 1 and obj7.get("skipped") == 0
        c7b, raw7b = _adm_req(env, "POST", "/api/accounts", [{"uid": "no-token"}], headers=H)
        ok7b = json.loads(raw7b).get("skipped") == 1
        # delete 一个，剩 1 个
        c8, _ = _adm_req(env, "DELETE", "/api/accounts/fake-uid", headers=H)
        c8b, raw8b = _adm_req(env, "GET", "/api/accounts", headers=H)
        ok8 = c8 == 200 and len(json.loads(raw8b)["accounts"]) == 1
        # models（先拉清单）→ stats（models 字段是缓存计数）→ model-routes
        c10, raw10 = _adm_req(env, "GET", "/api/models", headers=H)
        obj10 = json.loads(raw10)
        ok10 = c10 == 200 and len(obj10.get("models") or []) == 3 \
            and obj10["models"][0]["id"] == "fkmodel"
        c9, raw9 = _adm_req(env, "GET", "/api/stats", headers=H)
        obj9 = json.loads(raw9)
        ok9 = (c9 == 200 and obj9.get("accounts") == 1 and obj9.get("models") == 3
               and obj9.get("keys") == 0 and "today" in obj9 and "trend" in obj9
               and "checkins" in obj9)
        c11, raw11 = _adm_req(env, "GET", "/api/model-routes", headers=H)
        ok11 = c11 == 200 and json.loads(raw11) == {"mappings": {}, "aliases": {}}
        _record("admin_accounts_adapters",
                ok1 and ok2 and c3 == 404 and ok4 and ok5 and ok5b and ok6
                and c6b == 404 and ok7 and ok7b and ok8 and ok9 and ok10 and ok11,
                "page=%s checkin=%s test404=%s test=%s refresh=%s/%s patch=%s/%s "
                "import=%s/%s del=%s stats=%s models=%s routes=%s"
                % (ok1, ok2, c3, ok4, ok5, ok5b, ok6, c6b, ok7, ok7b, ok8, ok9,
                   ok10, ok11))
    finally:
        Q.ADMIN_PASSWORD = ""
        env.close()


def test_admin_stats_usage():
    """stats.today 的请求数/token 近似计数：非流式 + 流式各一次后 requests==2。"""
    env = _Env()
    try:
        Q.ADMIN_PASSWORD = "adm-pw-5"
        H = {"Authorization": "Bearer adm-pw-5"}
        code1, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        code2, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "fkmodel",
                          "messages": [{"role": "user", "content": "hi"}], "stream": True})
        c, raw = _adm_req(env, "GET", "/api/stats", headers=H)
        obj = json.loads(raw)
        today = obj.get("today") or {}
        trend = obj.get("trend") or []
        ok = (code1 == 200 and code2 == 200 and c == 200
              and today.get("requests") == 2 and (today.get("total_tokens") or 0) >= 2
              and obj.get("accounts") == 1 and obj.get("models") == 3
              and isinstance(trend, list) and len(trend) == 7
              and trend[-1].get("requests") == 2)
        _record("admin_stats_usage", ok, "today=%s trend_len=%d"
                % (json.dumps(today, ensure_ascii=False), len(trend)))
    finally:
        Q.ADMIN_PASSWORD = ""
        env.close()


def test_model_routes_apply():
    """请求侧模型映射：alias 链式（alias→mapping）、alias 直指、mapping、不命中原样透传；
    /v1/models 附带假名行。"""
    env = _Env()
    try:
        routes = {"mappings": {"fkmapped": "fkmodel"}, "aliases": {"myfake": "fkmapped",
                                                                   "plainfk": "fkmodel"}}
        with open(Q.MODEL_ROUTES_FILE, "w", encoding="utf-8") as f:
            json.dump(routes, f, ensure_ascii=False)
        # 链式：myfake →(alias) fkmapped →(mapping) fkmodel
        code1, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "myfake", "messages": [{"role": "user", "content": "hi"}]})
        m1 = env.signer.calls[-1]["params"]["infer"]["model"]
        # alias 直指真实名：plainfk → fkmodel（alias 命中后无 mapping，原样）
        code2, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "plainfk", "messages": [{"role": "user", "content": "hi"}]})
        m2 = env.signer.calls[-1]["params"]["infer"]["model"]
        # mapping：fkmapped → fkmodel
        code3, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "fkmapped", "messages": [{"role": "user", "content": "hi"}]})
        m3 = env.signer.calls[-1]["params"]["infer"]["model"]
        # 不命中：真实名原样透传（路由不改名，行为与无路由文件一致）
        code4, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        m4 = env.signer.calls[-1]["params"]["infer"]["model"]
        # 未知名且无路由 → 维持现状 404
        code5, _ = _post(env.url + "/v1/chat/completions",
                         {"model": "zzz-not-routed",
                          "messages": [{"role": "user", "content": "hi"}]})
        ok_resolve = (code1 == 200 and m1 == "fkmodel" and code2 == 200 and m2 == "fkmodel"
                      and code3 == 200 and m3 == "fkmodel" and code4 == 200 and m4 == "fkmodel"
                      and code5 == 404)
        # /v1/models：真实名 + mapping 键 + 假名都在列表里，且假名标注 alias_of/maps_to
        c6, raw6 = _get(env.url + "/v1/models")
        data = json.loads(raw6)["data"]
        ids = [m["id"] for m in data]
        row_myfake = next((m for m in data if m["id"] == "myfake"), {})
        row_fkmapped = next((m for m in data if m["id"] == "fkmapped"), {})
        ok_list = (c6 == 200 and all(x in ids for x in
                                     ("fkmodel", "fktwo", "fkmapped", "myfake", "plainfk"))
                   and row_myfake.get("metadata", {}).get("alias_of") == "fkmapped"
                   and row_myfake.get("metadata", {}).get("maps_to") == "fkmodel"
                   and row_fkmapped.get("metadata", {}).get("maps_to") == "fkmodel")
        _record("model_routes_apply", ok_resolve and ok_list,
                "infer=%s/%s/%s/%s 404=%s models=%s"
                % (m1, m2, m3, m4, code5, c6))
    finally:
        env.close()


def test_admin_model_routes_api():
    """/api/model-routes PUT/PATCH 全量替换 + 落盘 + 请求侧立即生效。"""
    env = _Env()
    try:
        Q.ADMIN_PASSWORD = "adm-pw-4"
        H = {"Authorization": "Bearer adm-pw-4"}
        c1, raw1 = _adm_req(env, "PUT", "/api/model-routes",
                            {"mappings": {"mapA": "fkmodel"},
                             "aliases": {"aliasB": "mapA"}}, headers=H)
        obj1 = json.loads(raw1)
        ok1 = c1 == 200 and obj1.get("mappings") == {"mapA": "fkmodel"} \
            and obj1.get("aliases") == {"aliasB": "mapA"}
        with open(Q.MODEL_ROUTES_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        ok1b = saved.get("mappings") == {"mapA": "fkmodel"} \
            and saved.get("aliases") == {"aliasB": "mapA"}
        c2, raw2 = _adm_req(env, "GET", "/api/model-routes", headers=H)
        obj2 = json.loads(raw2)
        ok2 = c2 == 200 and obj2 == {"mappings": {"mapA": "fkmodel"},
                                     "aliases": {"aliasB": "mapA"}}
        # 请求侧立即生效：aliasB → mapA → fkmodel（链式）
        code, _ = _post(env.url + "/v1/chat/completions",
                        {"model": "aliasB", "messages": [{"role": "user", "content": "hi"}]})
        m = env.signer.calls[-1]["params"]["infer"]["model"]
        ok3 = code == 200 and m == "fkmodel"
        # PATCH 全量替换（只给 aliases → mappings 清空）
        c4, raw4 = _adm_req(env, "PATCH", "/api/model-routes",
                            {"aliases": {"aliasC": "fkmodel"}}, headers=H)
        obj4 = json.loads(raw4)
        ok4 = c4 == 200 and obj4.get("aliases") == {"aliasC": "fkmodel"} \
            and obj4.get("mappings") == {}
        # 坏 JSON 400
        c5, _ = _adm_req(env, "PUT", "/api/model-routes", raw=b"{not json", headers=H)
        _record("admin_model_routes_api",
                ok1 and ok1b and ok2 and ok3 and ok4 and c5 == 400,
                "%s %s %s %s %s %s infer=%s" % (c1, c2, code, c4, c5, ok1b, m))
    finally:
        Q.ADMIN_PASSWORD = ""
        env.close()


def test_capture_decodes_client_blob():
    """--capture 的解密路径：用假 key/假密文打桩，只验证字段抽取，不读真实文件。"""
    plain = {"schemaVersion": 1, "token": "dt-fake", "refreshToken": "drt-fake",
             "expiresAt": "2099-01-01T00:00:00Z", "refreshTokenExpiresAt": "2099-06-01T00:00:00Z",
             "user": {"id": "fake-uid-1", "name": "fake-name"}}
    saved = {k: getattr(Q, k) for k in ("_get_master_key", "_aes_unprotect", "AUTH_BLOB",
                                        "_machine_identity", "_openapi_json")}
    tmp = tempfile.NamedTemporaryFile("w", suffix=".dat", delete=False, encoding="utf-8")
    tmp.write("placeholder")
    tmp.close()
    try:
        Q.AUTH_BLOB = tmp.name
        Q._get_master_key = lambda: b"0" * 32
        Q._aes_unprotect = lambda blob, key: json.dumps(plain).encode("utf-8")
        Q._machine_identity = lambda: {"machineId": "fake-machine", "machineToken": "fake-mt",
                                       "machineType": "5", "machineCode": ""}
        Q._openapi_json = lambda *a, **k: (404, {})
        acct = Q._capture_current()
        ok = (acct["token"] == "dt-fake" and acct["uid"] == "fake-uid-1"
              and acct["account"] == "uid-fake-uid" and acct["machineId"] == "fake-machine"
              and acct["exp"] > time.time() and acct["displayName"] == "fake-name")
        _record("capture_decodes_client_blob", ok, acct["account"])
    finally:
        for k, v in saved.items():
            setattr(Q, k, v)
        os.unlink(tmp.name)


def test_device_login_token_normalization():
    """设备码轮询回来的 JSON → 池内账号：snake_case 与客户端归一化 camelCase 都要吃下。"""
    snake = {"token": "dt-devfake", "refresh_token": "drt-devfake", "user_id": "fake-uid-dev1",
             "user_name": "fake-name", "expires_at": "2099-01-01T00:00:00Z",
             "refresh_token_expires_at": "2099-06-01T00:00:00Z"}
    a1 = Q._account_from_device_token(snake, {"machineId": "fake-machine",
                                              "machineToken": "fake-machine-token",
                                              "machineType": "5", "machineCode": ""})
    camel = {"token": "dt-devfake2", "refreshToken": "drt-devfake2",
             "expireTimeS": 4070908800, "refreshTokenExpireTimeS": 4086806400,
             "machineIdUsed": "fake-machine-2", "profile": {"uid": "fake-uid-dev2",
                                                            "name": "fake-name"}}
    a2 = Q._account_from_device_token(camel)
    a3 = Q._account_from_device_token({"token": "dt-devfake3", "user_id": "fake-uid-dev3",
                                       "expires_in": 3600})
    bad = Q._account_from_device_token({"message": "no token here"})
    ok = (a1["token"] == "dt-devfake" and a1["refreshToken"] == "drt-devfake"
          and a1["uid"] == "fake-uid-dev1" and a1["machineId"] == "fake-machine"
          and a1["exp"] > time.time() and a1["expiresAt"] == "2099-01-01T00:00:00Z"
          and a2["token"] == "dt-devfake2" and a2["exp"] == 4070908800
          and a2["refreshTokenExpiresAt"] == 4086806400
          and a2["uid"] == "fake-uid-dev2" and a2["machineId"] == "fake-machine-2"
          and 3000 < a3["exp"] - time.time() < 3700
          and bad is None)
    _record("device_login_token_normalization", ok,
            "exp=%s/%s None=%s" % (a1["exp"], a2["exp"], bad))


def test_device_login_argv():
    """sidecar 命令行：--login/--site 必给，machine 参数只在拿得到时才传（没有客户端也要能用）。"""
    saved = {k: getattr(Q, k) for k in ("SITE", "SIGN_JS", "NODE")}
    try:
        Q.SITE = "https://qoder.cn"
        Q.SIGN_JS = os.path.join("t", "qoder_sign.mjs")
        Q.NODE = "node"
        argv = Q._device_login_argv({"machineId": "m-1", "machineToken": "mt-1"})
        ok = (argv[:2] == ["node", os.path.join("t", "qoder_sign.mjs")] and "--login" in argv
              and argv[argv.index("--site") + 1] == "https://qoder.cn"
              and argv[argv.index("--machine-id") + 1] == "m-1"
              and argv[argv.index("--machine-token") + 1] == "mt-1")
        argv2 = Q._device_login_argv({"machineId": "", "machineToken": ""})
        ok = ok and "--machine-id" not in argv2 and "--machine-token" not in argv2
        argv3 = Q._device_login_argv({}, site="https://qoder.com", client_id="cid-9")
        ok = ok and argv3[argv3.index("--site") + 1] == "https://qoder.com" \
            and argv3[argv3.index("--client-id") + 1] == "cid-9"
        _record("device_login_argv", ok, " ".join(argv2))
    finally:
        for k, v in saved.items():
            setattr(Q, k, v)


def test_device_login_cmd():
    """--login 全流程（sidecar 打桩）：打印授权链接 → 开浏览器 → 轮询到 token → 入池。"""
    env = _Env(accounts=[])
    saved = {k: getattr(Q, k) for k in ("_machine_identity", "SITE", "SIGN_JS", "NODE")}
    lines = [json.dumps({"ok": True, "event": "ready"}),
             json.dumps({"ok": True, "event": "auth_url",
                         "url": "https://qoder.cn/device/selectAccounts?challenge=ZmFrZQ"
                                "&challenge_method=S256&nonce=fake-nonce&machine_id=fake-machine"}),
             json.dumps({"ok": True, "event": "waiting", "seconds": 1}),
             json.dumps({"ok": True, "event": "token", "data": {
                 "token": "dt-devlogin", "refresh_token": "drt-devlogin",
                 "user_id": "fake-uid-login1", "user_name": "fake-name",
                 "expires_at": "2099-01-01T00:00:00Z"}})]
    seen = {"argv": None, "opened": [], "kw": None}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _FakeLoginProc(lines)

    try:
        Q._openapi_json = lambda *a, **k: (404, {})
        Q._machine_identity = lambda: {"machineId": "fake-machine", "machineToken": "fake-mt",
                                       "machineType": "5", "machineCode": ""}
        rc, out = _stdout_of(lambda: Q._cmd_login(
            popen=fake_popen, open_url=lambda u: seen["opened"].append(u) or True))
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        ok = (rc == 0 and len(pool) == 1 and pool[0]["token"] == "dt-devlogin"
              and pool[0]["refreshToken"] == "drt-devlogin" and pool[0]["uid"] == "fake-uid-login1"
              and pool[0]["machineId"] == "fake-machine" and pool[0]["exp"] > time.time()
              and seen["opened"] and "device/selectAccounts" in seen["opened"][0]
              and "challenge=ZmFrZQ" in out and "--login" in (seen["argv"] or [])
              and "fake-name" not in out and "dt-devlogin" not in out
              and "fake-name" not in json.dumps(pool) and "dt-devlogin" not in out)
        _record("device_login_cmd", ok,
                "rc=%s pool=%d url_shown=%s" % (rc, len(pool),
                                                "challenge=ZmFrZQ" in out))
    finally:
        for k, v in saved.items():
            setattr(Q, k, v)
        env.close()


def test_device_login_new_device():
    """--new-device 时不传本机 machineId，让 sidecar 生成新 UUID。"""
    env = _Env(accounts=[])
    saved = {k: getattr(Q, k) for k in ("_machine_identity", "SITE", "SIGN_JS", "NODE")}
    lines = [json.dumps({"ok": True, "event": "auth_url",
                         "url": "https://qoder.cn/device/selectAccounts?challenge=ZmFrZQ"}),
             json.dumps({"ok": True, "event": "token", "data": {
                 "token": "dt-newdev", "refresh_token": "drt-newdev",
                 "user_id": "fake-uid-newdev", "expires_at": "2099-01-01T00:00:00Z"}})]
    seen = {"argv": None}

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _FakeLoginProc(lines)

    try:
        Q._openapi_json = lambda *a, **k: (404, {})
        Q._machine_identity = lambda: {"machineId": "fake-machine", "machineToken": "fake-mt",
                                       "machineType": "5", "machineCode": ""}
        rc, _ = _stdout_of(lambda: Q._cmd_login(
            popen=fake_popen, open_url=lambda u: True, new_device=True))
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        argv = seen["argv"] or []
        ok = (rc == 0 and len(pool) == 1 and pool[0]["token"] == "dt-newdev"
              and "--machine-id" not in argv and "--machine-token" not in argv
              and pool[0]["machineId"] not in ("", "fake-machine")
              and len(pool[0]["machineId"]) == 36)
        _record("device_login_new_device", ok, "machineId=%s" % pool[0].get("machineId"))
    finally:
        for k, v in saved.items():
            setattr(Q, k, v)
        env.close()


def test_pool_device_isolation():
    """默认每个号一套设备身份：撞车的重发、原来那套留给续期、结果落盘且重跑幂等。"""
    env = _Env(accounts=[_fake_account("uid-dv0001"), _fake_account("uid-dv0002"),
                         _fake_account("uid-dv0003")])
    try:
        rows = Q._account_table()
        with open(env.accounts_file, encoding="utf-8") as f:
            pool = json.load(f)["accounts"]
        mids = [p.get("machineId") for p in pool]
        ok = (len(set(mids)) == 3 and mids[0] == "fake-machine"
              and all(p.get("loginMachineId") == "fake-machine"
                      and p.get("loginMachineToken") == "fake-machine-token"
                      for p in pool[1:])
              and all(r["device"] == "独立" for r in rows)
              and Q._device_summary(rows).startswith("3/3")
              and Q._refresh_device_fields(pool[1]) == {"machine_id": "fake-machine",
                                                        "machine_token": "fake-machine-token"})
        Q._pool.update({"accounts": [], "mtime": 0.0})     # 当作重启进程重新读盘
        again = Q._account_table()
        with open(env.accounts_file, encoding="utf-8") as f:
            pool2 = json.load(f)["accounts"]
        ok = (ok and [p.get("machineId") for p in pool2] == mids
              and all(r["device"] == "独立" for r in again))
        _record("pool_device_isolation", ok, "独立=%d/%d"
                % (sum(1 for r in rows if r["device"] == "独立"), len(rows)))
    finally:
        env.close()


def test_device_login_failures():
    """三条失败路径都要非 0 退出、给中文原因、且绝不往池里写半截账号。"""
    env = _Env(accounts=[])
    saved = {k: getattr(Q, k) for k in ("_machine_identity", "SITE", "SIGN_JS", "NODE")}
    auth_line = json.dumps({"ok": True, "event": "auth_url",
                            "url": "https://qoder.cn/device/selectAccounts?challenge=ZmFrZQ"})
    try:
        Q._openapi_json = lambda *a, **k: (404, {})
        Q._machine_identity = lambda: {"machineId": "", "machineToken": "", "machineType": "",
                                       "machineCode": ""}
        rc1, out1 = _stdout_of(lambda: Q._cmd_login(
            popen=lambda argv, **kw: _FakeLoginProc(
                [auth_line, json.dumps({"ok": False, "event": "error",
                                        "error": "device flow timeout"})]),
            open_url=lambda u: True))
        rc2, out2 = _stdout_of(lambda: Q._cmd_login(
            popen=lambda argv, **kw: _FakeLoginProc([auth_line, json.dumps({"ok": True,
                                                                            "event": "ready"})]),
            open_url=lambda u: True))
        rc3, out3 = _stdout_of(lambda: Q._cmd_login(
            popen=lambda argv, **kw: (_ for _ in ()).throw(OSError("找不到 node")),
            open_url=lambda u: True))
        rc4, out4 = _stdout_of(lambda: Q._cmd_login(
            popen=lambda argv, **kw: _FakeLoginProc([json.dumps(
                {"ok": True, "event": "token", "data": {"user_id": "fake-uid-x"}})]),
            open_url=lambda u: True))
        pool = []
        if os.path.exists(env.accounts_file):
            with open(env.accounts_file, encoding="utf-8") as f:
                pool = json.load(f).get("accounts") or []
        ok = (rc1 and rc2 and rc3 and rc4 and not pool
              and "timeout" in out1 and "node" in out3 and out4.strip())
        _record("device_login_failures", bool(ok),
                "rc=%s/%s/%s/%s pool=%d" % (rc1, rc2, rc3, rc4, len(pool)))
    finally:
        for k, v in saved.items():
            setattr(Q, k, v)
        env.close()


def test_login_wait_deadline():
    """sidecar 一直 only 心跳时，Python 侧 deadline 必须能自己收口（否则留下挂死的 node 子进程）。"""
    def slow_lines():
        yield json.dumps({"ok": True, "event": "auth_url", "url": "https://qoder.cn/device/selectAccounts?x=1"})
        time.sleep(0.05)
        yield json.dumps({"ok": True, "event": "waiting"})
        yield json.dumps({"ok": True, "event": "waiting"})
    urls = []
    try:
        Q._consume_login_lines(slow_lines(), on_auth_url=urls.append,
                               deadline=time.time() + 0.01)
        _record("login_wait_deadline", False, "没抛 LoginError")
    except Q.LoginError as e:
        _record("login_wait_deadline", "超时" in str(e) and urls
                and urls[0].startswith("https://qoder.cn/device/"), str(e)[:60])


def test_model_alias_and_cache():
    env = _Env()
    try:
        Q._fetch_models()
        m = Q._fetch_models()
        # 清单来自 mock /algo model/list；slug 把空格换成 '-'
        ok = len(m["list"]) == 3 and "fkmodel" in m["alias"] and "fake-one" in m["alias"]
        r1 = Q._resolve_model("Fake One")
        r2 = Q._resolve_model("fkmodel")
        r3 = Q._resolve_model("nope")
        ok = ok and r1["key"] == "fkmodel" and r2["key"] == "fkmodel" and r3 is None
        # 第二次调用应命中缓存（_models["at"] 不变，不打上游）
        cached_at = Q._models["at"]
        Q._fetch_models()
        ok = ok and Q._models["at"] == cached_at
        _record("model_alias_and_cache", ok, "list=%d" % len(m["list"]))
    finally:
        env.close()


# ── 3. 纯函数边界 ──────────────────────────────────────────────────────────
def test_redact():
    """_redact 脱敏：保留前缀，短串全掩。"""
    ok = (Q._redact("dt-abcdef123456") == "dt-" + "*" * 12   # len=15, keep=3 → 12 stars
          and Q._redact("ab") == "**"
          and Q._redact("") == ""
          and Q._redact(None) is None
          and Q._redact("x", keep=3) == "*")
    _record("redact", ok)


def test_thinking_helpers():
    """档位换算 / 就近调整 / 模型无声明时透传与丢弃。"""
    ok = (Q._effort_of_budget(0) == "none" and Q._effort_of_budget(1024) == "low"
          and Q._effort_of_budget(8192) == "medium" and Q._effort_of_budget(24576) == "high"
          and Q._effort_of_budget(49152) == "xhigh" and Q._effort_of_budget(65536) == "max"
          and Q._norm_effort("DISABLED") == "none" and Q._norm_effort("off") == "none"
          and Q._norm_effort("minimal") == "low" and Q._norm_effort(4096) == "medium"
          and Q._norm_effort("banana") is None and Q._norm_effort(True) is None)
    m1 = {"key": "m1", "thinking_config": {
        "disabled": {}, "enabled": {"efforts": {"low": {}, "high": {}}}}}
    e, p = Q._thinking_upstream({"reasoning_effort": "medium"}, m1)
    ok = ok and e == "low" and p == {"reasoning_effort": "low", "enable_thinking": True}
    e, p = Q._thinking_upstream({"thinking_budget": 3000}, m1)
    ok = ok and e == "low" and "reasoning_budget_tokens" not in p
    e, p = Q._thinking_upstream({"reasoning_effort": "none", "thinking_budget": 500}, m1)
    ok = ok and e == "none" and p == {"reasoning_effort": "none", "enable_thinking": False}
    # 只有 disabled 没有 enabled.efforts：请求任何思考档位都没有可用项 → 丢弃
    m2 = {"key": "m2", "thinking_config": {
        "disabled": {}, "enabled": {"description": "Enable thinking"}}}
    e, p = Q._thinking_upstream({"reasoning_effort": "high"}, m2)
    ok = ok and e is None and p is None
    e, p = Q._thinking_upstream({"reasoning_effort": "none"}, m2)
    ok = ok and e == "none" and p["enable_thinking"] is False
    # 模型没声明 thinking_config：原样透传
    e, p = Q._thinking_upstream({"reasoning_effort": "xhigh"}, {"key": "m3"})
    ok = ok and e == "xhigh" and p["reasoning_effort"] == "xhigh"
    # 全空入参：零注入
    e, p = Q._thinking_upstream({}, m1)
    ok = ok and e is None and p is None
    _record("thinking_helpers", ok)


def test_merge_tool_name():
    """_merge_tool_name：分片拼接 / 整名覆盖 / 空值。"""
    ok = (Q._merge_tool_name("", "get_") == "get_"
          and Q._merge_tool_name("get_", "time") == "get_time"
          and Q._merge_tool_name("get_time", "get_time") == "get_time"
          and Q._merge_tool_name("get_time", "") == "get_time"
          and Q._merge_tool_name("", "") == "")
    _record("merge_tool_name", ok)


def test_map_upstream_error():
    """_map_upstream_error 全分支。"""
    ok = (Q._map_upstream_error(401) == 401
          and Q._map_upstream_error(403) == 401
          and Q._map_upstream_error(429) == 429
          and Q._map_upstream_error(400) == 400
          and Q._map_upstream_error(404) == 400
          and Q._map_upstream_error(422) == 400
          and Q._map_upstream_error(500, "oops") == 502
          and Q._map_upstream_error(200, "sensitive") == 400
          and Q._map_upstream_error(200, "content_security") == 400
          and Q._map_upstream_error(999) == 502
          # 上游业务限制藏在首帧的 403 里：不能当鉴权失败（实测 code 110 / 10605 / 112）
          and Q._map_upstream_error(403, '{"code":"110","message":"Billing daily count exceeded"}') == 429
          and Q._map_upstream_error(403, '{"code":"10605","message":"{\\"isQueued\\":true}"}') == 429
          and Q._map_upstream_error(403, '{"code":"112","message":"{\\"pricingUrl\\":\\"https://qoder.com/pricing\\"}"}') == 429
          and Q._map_upstream_error(403, "invalid device token") == 401)
    _record("map_upstream_error", ok)


def test_tool_choice_conversion():
    """_tool_choice_to_openai 各协议各形态。"""
    ok = (Q._tool_choice_to_openai(None, "openai") is None
          and Q._tool_choice_to_openai("auto", "openai") == "auto"
          and Q._tool_choice_to_openai("any", "anthropic") == "required"
          and Q._tool_choice_to_openai("none", "anthropic") == "none"
          and Q._tool_choice_to_openai("any", "responses") == "any")   # 非 anthropic 原样返回
    tc = {"type": "function", "name": "get_time"}
    r = Q._tool_choice_to_openai(tc, "openai")
    ok = ok and r == {"type": "function", "function": {"name": "get_time"}}
    tc2 = {"type": "tool", "name": "get_time"}
    r2 = Q._tool_choice_to_openai(tc2, "anthropic")
    ok = ok and r2 == {"type": "function", "function": {"name": "get_time"}}
    _record("tool_choice_conversion", ok)


def test_finish_to_anthropic():
    """_finish_to_anthropic 映射。"""
    ok = (Q._finish_to_anthropic("stop") == "end_turn"
          and Q._finish_to_anthropic("tool_calls") == "tool_use"
          and Q._finish_to_anthropic("length") == "max_tokens"
          and Q._finish_to_anthropic(None) == "end_turn"
          and Q._finish_to_anthropic("weird") == "weird")
    _record("finish_to_anthropic", ok)


def test_tools_to_openai():
    """_tools_to_openai：function 格式 / name 格式 / 非 dict 跳过。"""
    tools = [
        {"type": "function", "function": {"name": "f1", "parameters": {"type": "object"}}},
        {"name": "f2", "description": "desc", "input_schema": {"type": "string"}},
        "not a dict",
    ]
    out = Q._tools_to_openai(tools)
    ok = (len(out) == 2
          and out[0]["function"]["name"] == "f1"
          and out[1]["function"]["name"] == "f2"
          and out[1]["function"]["parameters"] == {"type": "string"}
          and out[1]["function"]["description"] == "desc")
    _record("tools_to_openai", ok, "n=%d" % len(out))


def test_slug():
    """_slug：特殊字符 → 连字符。"""
    ok = (Q._slug("Fake One") == "fake-one"
          and Q._slug("GLM-5.2 Pro") == "glm-5-2-pro"
          and Q._slug("") == ""
          and Q._slug(None) == ""
          and Q._slug("  Hello!!  ") == "hello")
    _record("slug", ok)


def test_pool_expired_logic():
    """_pool_expired：过期 + failed_at 在冷却期内 → True。"""
    now = time.time()
    ok = (Q._pool_expired({"exp": now - 100, "failed_at": now - 10}, now) is True
          and Q._pool_expired({"exp": now - 100, "failed_at": now - 999}, now) is False
          and Q._pool_expired({"exp": now + 100}, now) is False
          and Q._pool_expired({}, now) is False
          and Q._pool_expired({"exp": now - 100}, now) is False)
    _record("pool_expired_logic", ok)


# ── 4. HTTP 边界 ───────────────────────────────────────────────────────────
def test_cors_options():
    """OPTIONS 返回 204 + CORS 头。"""
    env = _Env()
    try:
        req = urllib.request.Request(env.url + "/v1/chat/completions", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
            origin = r.headers.get("Access-Control-Allow-Origin")
            methods = r.headers.get("Access-Control-Allow-Methods")
        _record("cors_options", code == 204 and origin == "*" and "POST" in (methods or ""),
                "%s %s %s" % (code, origin, methods))
    except Exception as e:
        _record("cors_options", False, "异常: %r" % e)
    finally:
        env.close()


def test_empty_pool_health():
    """/health 空池：ok=False + account_count=0。"""
    env = _Env(accounts=[])
    try:
        code, raw = _get(env.url + "/health")
        h = json.loads(raw)
        ok = code == 200 and h["ok"] is False and h["account_count"] == 0
        _record("empty_pool_health", ok, str(h.get("account_count")))
    finally:
        env.close()


def test_empty_pool_chat_401():
    """空池发 chat → 401（账号池为空）。"""
    env = _Env(accounts=[])
    try:
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}]})
        ok = code == 401 and b"\xe8\xb4\xa6\xe5\x8f\xb7\xe6\xb1\xa0" in raw  # "账号池"
        _record("empty_pool_chat_401", ok, str(code))
    finally:
        env.close()


def test_unknown_routes_404():
    """未知 GET/POST 路径 → 404。"""
    env = _Env()
    try:
        c1, _ = _get(env.url + "/v1/unknown")
        c2, _ = _post(env.url + "/v1/unknown", {"x": 1})
        _record("unknown_routes_404", c1 == 404 and c2 == 404, "%s %s" % (c1, c2))
    finally:
        env.close()




def test_health_signer_peek():
    env = _Env()
    try:
        code, raw = _get(env.url + "/health")
        h = json.loads(raw)
        si = h.get("signer") or {}
        ok = code == 200 and si.get("cosyVersion") == "9.9.9" and si.get("started") is True
        _record("health_signer_peek", ok, str(si))
    finally:
        env.close()


def test_non_stream_responses_no_input():
    """/v1/responses 缺 input → 400。"""
    env = _Env()
    try:
        code, _ = _post(env.url + "/v1/responses", {"model": "fkmodel"})
        _record("responses_no_input_400", code == 400, str(code))
    finally:
        env.close()


def test_non_stream_chat_no_messages():
    """/v1/chat/completions 缺 messages → 400。"""
    env = _Env()
    try:
        code, _ = _post(env.url + "/v1/chat/completions", {"model": "fkmodel"})
        _record("chat_no_messages_400", code == 400, str(code))
    finally:
        env.close()


# ── 主入口 ─────────────────────────────────────────────────────────────────
def test_clean_import():
    """全新解释器里 import qoder_proxy：抓"只有独立运行才暴露"的漏导入。

    本测试文件自身会 import http.server 等模块，掩盖源码里的缺失导入，
    所以必须另起一个干净进程验证。
    """
    src = ('import sys; sys.path.insert(0, r%r); import qoder_proxy'
           % os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True)
    _record("clean_import", p.returncode == 0,
            (p.stderr or "").strip().splitlines()[-1] if p.returncode else "")


# ── 纯 Python 签名器（去 node）──────────────────────────────────────────────
def _md5_pre(p0, key, date, body, path):
    return hashlib.md5("\n".join((p0, key, date, body, path)).encode("utf-8")).hexdigest()


def test_native_signer_requests():
    s = Q._NativeSigner()
    acct = _fake_account()
    got = s.request(acct, {"endpoint": "https://gw.example",
                           "path": "/algo/api/v2/model/list", "method": "GET"})
    h = got["headers"]
    auth = h["Authorization"][len("Bearer COSY."):]
    p0, sig = auth.split(".")
    part0 = json.loads(base64.b64decode(p0))
    ok = (got["url"] == "https://gw.example/algo/api/v2/model/list"
          and not got.get("body")
          and h["Cosy-User"] == acct["uid"]
          and h["Cosy-MachineId"] == acct["machineId"]
          and h["Cosy-ClientIp"] == acct["machineId"]
          and h["Cosy-Data-Policy"] == "disagree"
          and part0["version"] == "v1" and part0["cosyVersion"] == acct["cosyVersion"]
          and sig == _md5_pre(p0, h["Cosy-Key"], h["Cosy-Date"], "",
                              "/api/v2/model/list"))

    body = {"model_config": {"key": "m1", "source": "system"},
            "messages": [{"role": "user", "content": "hi"}], "stream": True}
    got2 = s.request(acct, {"endpoint": "https://gw.example", "method": "POST",
                            "path": "/algo/api/v2/service/pro/sse/agent_chat_generation",
                            "infer": {"model": "m1", "source": "system"}, "body": body})
    h2 = got2["headers"]
    p02, sig2 = h2["Authorization"][len("Bearer COSY."):].split(".")
    plain = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    want_body = Q.encode_body(plain).decode()
    path2 = "/api/v2/service/pro/sse/agent_chat_generation"
    ok = ok and got2["url"].endswith(
        path2 + "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")
    ok = ok and got2["body"] == want_body
    ok = ok and h2["X-Model-Key"] == "m1" and h2["X-Model-Source"] == "system"
    ok = ok and h2["Accept"] == "text/event-stream"
    ok = ok and sig2 == _md5_pre(p02, h2["Cosy-Key"], h2["Cosy-Date"], want_body, path2)
    _record("native_signer_requests", ok, got["url"])


def test_native_signer_context_cache():
    s = Q._NativeSigner()
    acct = _fake_account()
    p = {"endpoint": "https://gw.example", "path": "/api/v2/model/list", "method": "GET"}
    k1 = s.request(acct, p)["headers"]["Cosy-Key"]
    k2 = s.request(dict(acct), p)["headers"]["Cosy-Key"]
    k3 = s.request(dict(acct, uid="someone-else"), p)["headers"]["Cosy-Key"]
    _record("native_signer_context_cache", k1 == k2 and k1 != k3)


def test_sign_backend_selection():
    ok = (isinstance(Q._make_signer("py"), Q._NativeSigner)
          and isinstance(Q._make_signer("node"), Q._Signer)
          and isinstance(Q._make_signer("auto"), Q._AutoSigner)
          and isinstance(Q._make_signer("bogus"), Q._AutoSigner))
    _record("sign_backend_selection", ok, Q.SIGN_BACKEND)


def test_sign_backend_fallback():
    class Bad:
        def request(self, *a, **k):
            raise Q.SignerError("py broke")

        def peek(self):
            return {"started": False, "error": "py broke"}

    class Good:
        def __init__(self):
            self.n = 0

        def request(self, *a, **k):
            self.n += 1
            return {"url": "https://x/algo", "headers": {}}

    good = Good()
    r = Q._AutoSigner(native=Bad(), node=good).request({}, {})
    py_failed = False
    try:
        Q._AutoSigner(native=Bad(), node=good, mode="py").request({}, {})
    except Q.SignerError:
        py_failed = True
    _record("sign_backend_fallback",
            r["url"] == "https://x/algo" and good.n == 1 and py_failed)


def test_http_native_signer_e2e():
    env = _Env()
    try:
        Q._signer = Q._NativeSigner()
        c1, raw1 = _get(env.url + "/v1/models")
        models = json.loads(raw1)
        c2, raw2 = _post(env.url + "/v1/chat/completions",
                         {"model": "fkmodel", "messages": [{"role": "user", "content": "hi"}],
                          "stream": False})
        out = json.loads(raw2)
        path, sent, hdrs = env.upstream.received[-1]
        ok = (c1 == 200 and models["object"] == "list" and models["data"]
              and c2 == 200 and out["choices"][0]["message"]["content"]
              and "Cosy-MachineId" in hdrs and not sent.startswith("{"))
        _record("http_native_signer_e2e", ok, "%s %s %s" % (c1, c2, path))
    finally:
        env.close()


def _camp(cid, key, action, status, kind=None, amount=None):
    """按服务端真实字段拼一个 campaign 项（camelCase，id 是 campaignId）。"""
    c = {"campaignId": cid, "campaignKey": key, "actionType": action,
         "claimStatus": status, "startAt": 1792288800, "endAt": 1792375140}
    if kind:
        c["benefit"] = {"kind": kind, "amount": amount,
                        "validity": {"mode": "RELATIVE_DAYS", "days": 30}}
    return c


def test_checkin_claim_flow():
    acct = _fake_account()
    calls = []

    def fake(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        calls.append((method, path, body, dict(headers or {}), token))
        if path == Q.USAGE_PATH:
            return 200, {"displayMode": "qoder",
                         "qoderUsage": {"addOnQuota": {"total": 300, "remaining": 154}}}
        if method == "GET":
            first = sum(1 for c in calls
                        if c[0] == "GET" and c[1] == Q.CAMPAIGNS_PATH) == 1
            return 200, ({"campaigns": [
                            _camp("c1", "act-20261013-582", "CLAIM_BENEFIT",
                                  "CLAIMABLE", "CREDITS", 100),
                            _camp("c2", "act-20260901-922", "VIEW_DETAILS", "CLAIMED")],
                          "claimable": True, "showCampaign": True,
                          "campaignUrl": "https://openapi.qoder.com.cn/growth-page/activity-iframe"}
                         if first else {"campaigns": [], "claimable": False,
                                        "showCampaign": False})
        return 200, {"success": True, "grantId": "g-1",
                     "grantedAt": "2026-10-18T04:35:59Z", "replayed": False}

    orig = Q._openapi_json
    try:
        Q._openapi_json = fake
        ok, rep = Q._checkin_account(acct)
    finally:
        Q._openapi_json = orig
    gets = [c for c in calls if c[0] == "GET"]
    cgets = [c for c in gets if c[1] == Q.CAMPAIGNS_PATH]
    posts = [c for c in calls if c[0] == "POST"]
    good = (ok and len(cgets) == 2 and len(posts) == 1
            and [c[1] for c in gets if c[1] == Q.USAGE_PATH] == [Q.USAGE_PATH]
            and rep["addOnTotal"] == 300
            and posts[0][1] == Q.CAMPAIGNS_PATH + "/c1/claim"
            and posts[0][2] == {}
            and posts[0][3].get("Cosy-MachineId") == acct["machineId"]
            and posts[0][3].get("Cosy-Version") == acct["cosyVersion"]
            and posts[0][3].get("Cosy-ClientType") == "10"
            and posts[0][4] == acct["token"]
            and rep["claimed"][0]["status"] == 200
            and rep["claimed"][0]["kind"] == "CREDITS"
            and rep["claimed"][0]["amount"] == 100
            and rep["claimed"][0]["grantId"] == "g-1"
            and rep["after"]["claimable"] is False)
    _record("checkin_claim_flow", good,
            "calls=%d addOn=%r receipt=%s" % (len(calls), rep.get("addOnTotal"),
                                              str(rep["claimed"])[:90]))


def test_checkin_skips_non_claimable_items():
    """顶层 claimable=true 但每项都已 CLAIMED：不 POST，no-op 成功。"""
    acct = _fake_account()
    calls = []

    def fake(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        calls.append((method, path))
        return 200, {"campaigns": [
            _camp("c1", "act-20261013-582", "CLAIM_BENEFIT", "CLAIMED", "CREDITS", 100),
            _camp("c2", "act-20260901-922", "VIEW_DETAILS", "CLAIMED")],
            "claimable": True, "showCampaign": True}

    orig = Q._openapi_json
    try:
        Q._openapi_json = fake
        ok, rep = Q._checkin_account(acct)
    finally:
        Q._openapi_json = orig
    _record("checkin_skips_non_claimable_items",
            ok and len(calls) == 1 and calls[0][0] == "GET"
            and rep["campaigns"] == 2 and not rep["claimed"]
            and bool(rep.get("message")), str(rep.get("message")))


def test_checkin_claims_every_benefit_item():
    """同一天多项可领（Credits + 兑换码）要全领，不能只 POST 第一项。"""
    acct = _fake_account()
    posts = []

    def fake(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        if method == "POST":
            posts.append(path)
            return 200, {"success": True}
        return 200, {"campaigns": [
            _camp("c1", "act-20261013-582", "CLAIM_BENEFIT", "CLAIMABLE", "CREDITS", 100),
            _camp("c2", "act-20260928-620", "CLAIM_BENEFIT", "CLAIMABLE",
                  "REDEMPTION_CODE", 1)],
            "claimable": True, "showCampaign": True}

    orig = Q._openapi_json
    try:
        Q._openapi_json = fake
        ok, rep = Q._checkin_account(acct)
    finally:
        Q._openapi_json = orig
    _record("checkin_claims_every_benefit_item",
            ok and len(rep["claimed"]) == 2 and len(posts) == 2
            and posts[1].endswith("/c2/claim"), str(posts))


def test_checkin_not_claimable():
    acct = _fake_account()
    calls = []

    def fake(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        calls.append((method, path))
        return 200, {"campaigns": [], "claimable": False, "showCampaign": False}

    orig = Q._openapi_json
    try:
        Q._openapi_json = fake
        ok, rep = Q._checkin_account(acct)
    finally:
        Q._openapi_json = orig
    _record("checkin_not_claimable",
            ok and len(calls) == 1 and calls[0][0] == "GET" and bool(rep.get("message")),
            str(rep.get("message")))


def test_checkin_http_error():
    acct = _fake_account()
    calls = []

    def fake(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
        calls.append(method)
        return 500, {"message": "boom"}

    orig = Q._openapi_json
    try:
        Q._openapi_json = fake
        ok, rep = Q._checkin_account(acct)
    finally:
        Q._openapi_json = orig
    _record("checkin_http_error",
            (not ok) and calls == ["GET"] and rep.get("error") == "boom", str(rep))


def test_checkin_cli_all_accounts():
    """每个号今天的份都到位 → rc=0 并打"已到位 2/2"；一个都没到位 → rc=1（没领到就是失败）。"""
    env = _Env(accounts=[_fake_account("uid-ck0001"), _fake_account("uid-ck0002")])
    try:
        holder = {"campaigns": [_camp("c1", "act-20261013-582", "CLAIM_BENEFIT",
                                      "CLAIMED", "CREDITS", 100)]}
        calls = []

        def fake(method, path, body=None, token="", timeout=20, headers=None, ua=None, realm=None, **kw):
            calls.append((method, path, token))
            return 200, {"campaigns": list(holder["campaigns"]), "claimable": False}

        env.patch_openapi(fake)
        rc, out = _stdout_of(Q._cmd_checkin)
        ok = (rc == 0 and len(calls) == 2 and all(c[0] == "GET" for c in calls)
              and "uid-ck0001" in out and "uid-ck0002" in out
              and out.count("claimable=False") == 2 and "已到位 2/2" in out)
        holder["campaigns"] = []                     # 今天什么都还没到位
        rc2, out2 = _stdout_of(Q._cmd_checkin)
        ok = ok and rc2 == 1 and "已到位 0/2" in out2
        _record("checkin_cli_all_accounts", ok, "rc=%d rc2=%d" % (rc, rc2))
    finally:
        env.close()


def test_checkin_today_at():
    """到点计算：合法 HH:MM、非法退回、00:00 边界。"""
    base = time.mktime((2026, 10, 18, 8, 0, 0, 0, 0, -1))
    want = time.mktime((2026, 10, 18, 10, 30, 0, 0, 0, -1))
    ok = (Q._today_at("10:30", base) == want
          and Q._today_at("25:99", base) == want          # 非法 → 退回 10:30
          and Q._today_at("", base) == want
          and Q._today_at("00:00", base) == time.mktime((2026, 10, 18, 0, 0, 0, 0, 0, -1))
          and Q._today_at("23:59", base) == time.mktime((2026, 10, 18, 23, 59, 0, 0, 0, -1)))
    _record("checkin_today_at", ok, "got=%r" % Q._today_at("10:30", base))


def test_checkin_plan_boundaries():
    """调度四个边界：未到点 / 到点没成 / 今天已完成 / 重试次数用完。"""
    due, day = 2000.0, "D"
    p1 = Q._checkin_plan(1000.0, {"date": day, "attempts": 0}, day, due)
    p2 = Q._checkin_plan(due, {"date": day, "attempts": 1, "done": False}, day, due)
    p3 = Q._checkin_plan(due + 10, {"date": day, "done": True}, day, due)
    p4 = Q._checkin_plan(due + 10, {"date": day, "attempts": Q.CHECKIN_MAX_ATTEMPTS}, day, due)
    p5 = Q._checkin_plan(due + 10, {"date": "昨天", "done": True,
                                    "attempts": 99}, day, due)     # 跨天必须重置并重跑
    ok = (p1[0] == "wait" and p1[1] == 1000
          and p2[0] == "run"
          and p3[0] == "wait" and p3[1] == due + 86400 - (due + 10)
          and p4[0] == "wait"
          and p5[0] == "run")
    _record("checkin_plan_boundaries", ok, "%r %r %r %r %r" % (p1, p2, p3, p4, p5))


def test_checkin_state_roundtrip():
    """状态文件读写：缺文件=空、写后可读、坏 json 不炸（返回空 = 今天会重跑而不是崩线程）。"""
    tmpdir = tempfile.mkdtemp(prefix="qp-checkin-state-")
    saved = Q.CHECKIN_STATE_FILE
    try:
        Q.CHECKIN_STATE_FILE = os.path.join(tmpdir, "nope", "state.json")   # 目录都不存在
        empty = Q._read_checkin_state()
        Q._write_checkin_state({"date": "2026-10-18", "attempts": 2, "done": True})
        st = Q._read_checkin_state()
        with open(Q.CHECKIN_STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{ 坏文件")
        bad = Q._read_checkin_state()
        ok = (empty == {} and st.get("attempts") == 2 and st.get("done") is True
              and bad == {})
    finally:
        Q.CHECKIN_STATE_FILE = saved
    _record("checkin_state_roundtrip", ok, "st=%r bad=%r" % (st, bad))


def test_checkin_round_isolates_bad_account():
    """一轮里：新领到 1 个 → secured=1、total=3；单号抛异常和 HTTP 失败都不能带崩整轮。"""
    accs = [_fake_account("uid-cd0001"), _fake_account("uid-cd0002"), _fake_account("uid-cd0003")]

    def stub(acct):
        lab = acct["account"]
        if lab.endswith("0002"):          # 本轮真领到（有发放回执、非 replayed）
            return True, {"status": 200, "campaigns": 1, "secured": True,
                          "claimed": [{"id": "c1", "status": 200, "kind": "CREDITS",
                                       "amount": 100, "grantId": "g1",
                                       "grantedAt": "2026-10-18T04:35:59Z"}]}
        if lab.endswith("0003"):
            raise RuntimeError("模拟单号炸")
        return False, {"status": 500, "campaigns": 0, "claimed": [],
                       "secured": False, "error": "HTTP 500"}

    tmpdir = tempfile.mkdtemp(prefix="qp-checkin-round-")
    fp = os.path.join(tmpdir, "accounts.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "accounts": accs}, f, ensure_ascii=False)
    saved_acc, saved_ca = Q.ACCOUNTS_FILE, Q._checkin_account
    Q.ACCOUNTS_FILE = fp
    Q._checkin_account = stub
    try:
        Q._pool.update({"accounts": [], "mtime": 0.0})
        secured, total, all_ok = Q._checkin_round("测试")
        ok = secured == 1 and total == 3 and all_ok is False
    finally:
        Q._checkin_account = saved_ca
        Q.ACCOUNTS_FILE = saved_acc
        Q._pool.update({"accounts": [], "mtime": 0.0})
    _record("checkin_round_isolates_bad_account", ok,
            "secured=%d total=%d all_ok=%r" % (secured, total, all_ok))


def test_checkin_disabled_env():
    """关掉开关时不起线程（QODER_PROXY_CHECKIN=0）；开着时是个 daemon 线程。
    轮次桩和状态路径在 teardown 时**故意不还原**：线程睡底 30s，本测试跑完它还活着。"""
    saved_en = Q.CHECKIN_ENABLED
    t2 = None
    try:
        Q.CHECKIN_STATE_FILE = os.path.join(tempfile.mkdtemp(prefix="qp-thread-"), "state.json")
        Q.CHECKIN_ENABLED = False
        ok = Q._start_checkin_thread() is None
        Q.CHECKIN_ENABLED = True
        Q._checkin_round = lambda reason: (0, 0, True)   # 不发真请求，只验线程起得来
        t2 = Q._start_checkin_thread()
        ok = ok and t2 is not None and t2.daemon and t2.name == "checkin"
        # teardown 故意不还原 `_checkin_round` 和状态路径：线程睡底 30s，本测试跑完它还在。
        # 还原 = 把真轮次函数 + 真状态文件交还给一个孤儿线程，它会拿真账号池打真上游领 Credits。
    finally:
        Q.CHECKIN_ENABLED = saved_en
    _record("checkin_disabled_env", ok, "thread=%r" % (t2,))


def main():
    tests = [
        test_clean_import,
        test_parse_exp,
        test_unwrap_frame,
        test_iter_upstream_split_boundaries,
        test_aggregate_text_and_reasoning,
        test_aggregate_tool_calls,
        test_convert_responses_to_openai,
        test_convert_anthropic_to_openai,
        test_convert_openai_back,
        test_stream_states_events,
        test_parse_anthropic_text_and_count,
        test_model_alias_and_cache,
        test_http_health_and_models,
        test_http_chat_stream_openai,
        test_http_chat_non_stream,
        test_http_chat_tools,
        test_http_messages_anthropic,
        test_http_responses_protocol,
        test_http_thinking_depth,
        test_api_key_enforcement,
        test_body_oversize_413,
        test_unknown_model_404,
        test_bad_json_400,
        test_upstream_401_switch_account,
        test_upstream_429_cools_account_and_switches,
        test_upstream_429_all_cooling_returns_429,
        test_upstream_503_retried_then_success,
        test_upstream_retry_exhausted_502,
        test_upstream_500_not_retried,
        test_next_account_thread_safe_spread,
        test_upstream_error_frame_502,
        test_signer_failure_502,
        test_pool_capture_export_import,
        test_auto_refresh,
        test_capture_decodes_client_blob,
        test_device_login_token_normalization,
        test_device_login_argv,
        test_device_login_cmd,
        test_device_login_new_device,
        test_pool_device_isolation,
        test_device_login_failures,
        test_login_wait_deadline,
        # 纯函数边界
        test_redact,
        test_thinking_helpers,
        test_merge_tool_name,
        test_map_upstream_error,
        test_tool_choice_conversion,
        test_finish_to_anthropic,
        test_tools_to_openai,
        test_slug,
        test_pool_expired_logic,
        # HTTP 边界
        test_cors_options,
        test_empty_pool_health,
        test_empty_pool_chat_401,
        test_unknown_routes_404,
        test_health_signer_peek,
        test_non_stream_responses_no_input,
        test_non_stream_chat_no_messages,
        # 纯 Python 签名器
        test_native_signer_requests,
        test_native_signer_context_cache,
        test_sign_backend_selection,
        test_sign_backend_fallback,
        test_http_native_signer_e2e,
        # 每日签到
        test_checkin_claim_flow,
        test_checkin_skips_non_claimable_items,
        test_checkin_claims_every_benefit_item,
        test_checkin_not_claimable,
        test_checkin_http_error,
        test_checkin_cli_all_accounts,
        test_checkin_today_at,
        test_checkin_plan_boundaries,
        test_checkin_state_roundtrip,
        test_checkin_round_isolates_bad_account,
        test_checkin_disabled_env,
        test_refresh_single_flight_per_account,
        test_sweep_guard_is_atomic,
        test_env_isolates_checkin_state_file,
        test_pool_write_keeps_file_secret,
        # 多 Key 鉴权 + 管理面板 + 模型映射（qoder_admin 集成）
        test_multi_key_auth,
        test_keystore_precedence_over_single_key,
        test_admin_page_and_login,
        test_admin_keys_crud,
        test_admin_accounts_adapters,
        test_admin_stats_usage,
        test_model_routes_apply,
        test_admin_model_routes_api,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as e:
            _record(fn.__name__, False, "异常: %r" % e)

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n==== 总结: %d/%d 通过 ====" % (passed, total))
    if passed != total:
        print("失败项:")
        for name, ok, detail in _results:
            if not ok:
                print("  - %s: %s" % (name, detail))
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
