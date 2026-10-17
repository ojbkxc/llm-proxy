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

运行：python test_qoder_proxy.py
约束：纯标准库，全部假数据（dt-fake / fake-uid…），不连真实网关、不读真实 auth.v1.dat。
"""
import http.server as _srv
import base64
import hashlib
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

import qoder_proxy as Q

TEST_KEY = "testkey123"
MAX_BODY = Q.MAX_BODY_BYTES

_results = []


def _record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, (" - " + detail) if detail else ""))


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
                        "_backoff_sleep")}
        Q.ACCOUNTS_FILE = self.accounts_file
        Q.GATEWAY = self.upstream.base
        Q.OPENAPI = self.upstream.base
        Q._signer = self.signer
        Q._backoff_sleep = lambda n: self.sleeps.append(n)
        Q._pool.update({"accounts": [], "mtime": 0.0})
        Q._rr = 0
        Q._models.update({"at": 0.0, "list": [], "by_id": {}, "alias": {}})
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

    def fake_openapi(method, path, body=None, token="", timeout=20):
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

        def dead_openapi(method, path, body=None, token="", timeout=20):
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
              and pool[0]["machineId"] == "")
        _record("device_login_new_device", ok, "machineId=%s" % pool[0].get("machineId"))
    finally:
        for k, v in saved.items():
            setattr(Q, k, v)
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
          and Q._map_upstream_error(999) == 502)
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


def test_checkin_claim_flow():
    acct = _fake_account()
    calls = []

    def fake(method, path, body=None, token="", timeout=20, headers=None):
        calls.append((method, path, body, dict(headers or {}), token))
        if method == "GET":
            first = sum(1 for c in calls if c[0] == "GET") == 1
            return 200, ({"campaigns": [{"id": "c1", "title": "daily 100"}],
                          "claimable": True, "showCampaign": True}
                         if first else {"campaigns": [], "claimable": False,
                                        "showCampaign": False})
        return 200, {"success": True}

    orig = Q._openapi_json
    try:
        Q._openapi_json = fake
        ok, rep = Q._checkin_account(acct)
    finally:
        Q._openapi_json = orig
    gets = [c for c in calls if c[0] == "GET"]
    posts = [c for c in calls if c[0] == "POST"]
    good = (ok and len(gets) == 2 and len(posts) == 1
            and posts[0][1] == Q.CAMPAIGNS_PATH + "/c1/claim"
            and posts[0][2] == {}
            and posts[0][3].get("Cosy-MachineId") == acct["machineId"]
            and posts[0][3].get("Cosy-Version") == acct["cosyVersion"]
            and posts[0][4] == acct["token"]
            and rep["claimed"][0]["status"] == 200
            and rep["after"]["claimable"] is False)
    _record("checkin_claim_flow", good, str(rep["claimed"])[:100])


def test_checkin_not_claimable():
    acct = _fake_account()
    calls = []

    def fake(method, path, body=None, token="", timeout=20, headers=None):
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

    def fake(method, path, body=None, token="", timeout=20, headers=None):
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
    env = _Env(accounts=[_fake_account("uid-ck0001"), _fake_account("uid-ck0002")])
    try:
        calls = []

        def fake(method, path, body=None, token="", timeout=20, headers=None):
            calls.append((method, path, token))
            return 200, {"campaigns": [], "claimable": False}

        env.patch_openapi(fake)
        rc, out = _stdout_of(Q._cmd_checkin)
        ok = (rc == 0 and len(calls) == 2 and all(c[0] == "GET" for c in calls)
              and "uid-ck0001" in out and "uid-ck0002" in out
              and out.count("claimable=False") == 2)
        _record("checkin_cli_all_accounts", ok,
                "calls=%d" % len(calls))
    finally:
        env.close()


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
        test_checkin_not_claimable,
        test_checkin_http_error,
        test_checkin_cli_all_accounts,
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
