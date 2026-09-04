# -*- coding: utf-8 -*-
"""ws-proxy 本地测试脚本：不触网，用 mock 上游验证协议转换 / 脱敏 / 伪装还原。

用法：
    python test_proxy.py

原理：
    - 设置 WS_PROXY_MOCK 后，proxy 内部所有上游 HTTP 调用都会走 _http_json_mock，
      由本脚本注入的 _MockServer 返回预设的 OpenAI chat SSE / JSON。
    - 用 threading 起一个真实代理实例（127.0.0.1:PORT），用 urllib 发请求验证。
    - 模型列表也走 mock（list-organizations / list-assistants）。
"""
import io
import json
import os
import re
import threading
import urllib.error
import urllib.request
import time

os.environ["WS_PROXY_MOCK"] = "1"
os.environ["WS_PROXY_PORT"] = "8899"

import proxy  # noqa: E402


# ── 可切换的 mock 上游 ────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status, headers, data):
        self.status = status
        self.headers = headers
        self._data = data.encode("utf-8") if isinstance(data, str) else data
        self._consumed = False

    def read(self, n=-1):
        if self._consumed:
            return b""
        self._consumed = True
        if n == -1 or n >= len(self._data):
            return self._data
        out, self._data = self._data[:n], self._data[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _MockServer:
    """每次请求返回预设响应。records 记录收到的请求 URL / 头 / 体。"""
    def __init__(self):
        self.records = []
        self._queue = []

    def enqueue(self, data: str, status: int = 200, headers: dict = None):
        self._queue.append(_FakeResp(status, headers or {}, data))

    def next_response(self):
        return self._queue.pop(0) if self._queue else _FakeResp(200, {}, "{}")

    def read(self, n=-1):
        # 兼容旧用法
        return self.next_response().read(n)


class _DummyDB:
    """替身 sqlite 连接：proxy 的 _load_session_from_db / _write_session_row 走它读写。"""
    def __init__(self, rows):
        self.rows = list(rows)  # [(id, key, encrypted_hex, timestamp), ...]
        self.written = []

    def execute(self, sql, params=()):
        class _Cur:
            def __init__(self, owner, sql, params):
                self.owner = owner
                self.sql = sql
                self.params = params

            def fetchall(self):
                if "SELECT" in self.sql.upper() and "workspace_session" in self.sql:
                    return self.owner.rows
                return []

            def fetchone(self):
                r = self.fetchall()
                return r[0] if r else None
        return _Cur(self, sql, params)

    def commit(self):
        pass

    def close(self):
        pass


def _xor_enc(key: str, payload: dict) -> str:
    """用固定 key 对 payload 做 XOR，返回 hex（与 proxy._decrypt_session_row 互逆）。"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return bytes(b ^ ord(key[i % len(key)]) for i, b in enumerate(raw)).hex()


def upstream_error_response(status: int, body: str) -> "urllib.error.HTTPError":
    """构造与真实 urllib 一致的 HTTPError（4xx/5xx 时 _http_json 应抛出它）。"""
    return urllib.error.HTTPError(url="mock", code=status, msg="Mock Error",
                                  hdrs={}, fp=io.BytesIO(body.encode("utf-8")))


def chat_stream_response(text="你好，我是通义千问！", reasoning="", chunk_size=8):
    """构造 OpenAI chat 流式 SSE 字符串。

    chunk_size 控制每片字符数。为避免 mock 把 Workspace_<hash> / PH_<hash>
    占位符拦腰切断（真实模型按 token 输出、占位符是整 token），分片时跳过
    占位符内部。
    """
    parts = []
    if reasoning:
        parts.append('data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"reasoning_content":"%s"}}]}' % reasoning)
    # 智能分片：不切断 Workspace_<hash> / PH_<hash> 占位符
    ph_re = re.compile(r"(?:Workspace_[A-F0-9]{6}|PH_[A-F0-9]{10})")
    pieces = []
    i = 0
    while i < len(text):
        seg = text[i:i + chunk_size]
        m = ph_re.search(text, i)
        if m and i <= m.start() < i + chunk_size and m.end() > i + chunk_size:
            # 片会切断占位符 → 延伸到占位符结束
            seg = text[i:m.end()]
        pieces.append(seg)
        i += len(seg)
    for piece in pieces:
        parts.append('data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"%s"}}]}' % piece)
    parts.append('data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    parts.append("data: [DONE]")
    return "\n\n".join(parts) + "\n\n"


def chat_stream_response_frags(text_frags):
    """构造 OpenAI chat 流式 SSE，每个碎片作为独立 chunk 输出。

    用于模拟真实模型把 PH_/Workspace_ 占位符按 token 拦腰切分的场景，
    验证代理跨 chunk 缓冲后能完整还原。
    """
    parts = []
    for piece in text_frags:
        parts.append('data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"%s"}}]}' % piece)
    parts.append('data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}')
    parts.append("data: [DONE]")
    return "\n\n".join(parts) + "\n\n"


def chat_nonstream_response(text="你好，我是通义千问！"):
    return json.dumps({
        "id": "chatcmpl-x", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    })


def orgs_response():
    return json.dumps({"organizations": [{"id": "org1"}]})


def assistants_response():
    return json.dumps({"assistants": [{"configResult": {"config": {"models": [
        {"model": "qwen3.8-max", "name": "Qwen3.8-Max",
         "onPremProxyUrl": "https://mock.example/llm/f-devops-python-litellm/v1",
         "capabilities": ["chat"]},
    ]}}}]})


def _jwt(exp, pref="ex_mazy16"):
    """构造一个与真实 accessToken 同构的 JWT（本地 mock，非真实凭据）。"""
    import base64 as _b64
    h = _b64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    p = _b64.urlsafe_b64encode(json.dumps({
        "iss": "https://workspace-prd.midea.com/auth/realms/workspace",
        "preferred_username": pref, "exp": exp,
    }).encode()).rstrip(b"=").decode()
    return "%s.%s.sig" % (h, p)


def refresh_response(access_token=None, refresh_token=None, success=True, expire_time=0):
    """构造 /auth/refresh-token 响应体（access_token 带 Bearer 前缀，验证剥壳）。"""
    access_token = access_token or _jwt(int(time.time()) + 3600)
    refresh_token = refresh_token or "rt-new-token"
    return json.dumps({
        "success": success,
        "body": {
            "access_token": "Bearer " + access_token,
            "refresh_token": refresh_token,
            "user": {"uid": "ex_mazy16", "cn": "麦志业"},
            "access_token_info": {"expire_time": expire_time or int(time.time()) + 3600},
            "refresh_token_info": {"expire_time": expire_time or int(time.time()) + 7200},
        },
    })


def make_server():
    s = _MockServer()
    proxy.MOCK_SERVER = s
    # 清空模型列表缓存，确保每个测试都重新走 mock 的 list-organizations/list-assistants
    with proxy._models_lock:
        proxy._models_cache.update({"at": 0.0, "map": {}})
    return s


def _capture_last_request(s: _MockServer) -> dict:
    """取 mock 最近一次收到的上游请求体（records 由 _http_json_mock 记录）。"""
    return json.loads(s.records[-1][2].decode("utf-8"))


def http_req(method, path, body=None, headers=None):
    url = "http://127.0.0.1:%d%s" % (int(os.environ["WS_PROXY_PORT"]), path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=30)


def read_sse(resp):
    """读 HTTP 响应体，解析 SSE 事件。返回 [(event, data)]。"""
    body = resp.read().decode("utf-8")
    events = []
    for block in body.split("\n\n"):
        ev = None
        datas = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                datas.append(line[5:].strip())
        if datas:
            events.append((ev, "\n".join(datas)))
    return events


def _all_text_from_chat_stream(events):
    """从 OpenAI chat 流式 events 里拼出全部 content 文本。"""
    out = []
    for _, d in events:
        if d == "[DONE]":
            continue
        try:
            j = json.loads(d)
        except Exception:
            continue
        for ch in j.get("choices", []):
            t = (ch.get("delta") or {}).get("content")
            if t:
                out.append(t)
    return "".join(out)


PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  [PASS] " + name)
    else:
        FAILED.append(name)
        print("  [FAIL] " + name + ("  " + extra if extra else ""))


def test_openai_chat():
    print("\n== OpenAI chat（流式透传 + 伪装还原） ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response(text="我是 Codex 客户端，来自 Anthropic 公司。"))
    body = {"model": "qwen3.8-max", "stream": True,
            "messages": [{"role": "user", "content": "我是 Codex 用户，来自 Anthropic。"}]}
    resp = http_req("POST", "/v1/chat/completions", body)
    events = read_sse(resp)
    all_text = _all_text_from_chat_stream(events)
    check("流式正文还原客户端名（上游引用时）", "Codex" in all_text and "Anthropic" in all_text, all_text)
    # 上游请求体应含伪装占位符（且不含原词）
    upstream_req = next((json.loads(b.decode("utf-8")) for u, h, b in s.records if b), None)
    check("上游请求体不含 Codex/Anthropic 原词", upstream_req is not None
          and "Codex" not in json.dumps(upstream_req, ensure_ascii=False)
          and "Anthropic" not in json.dumps(upstream_req, ensure_ascii=False))
    check("上游请求体含 Workspace 占位符", upstream_req is not None
          and "Workspace_" in json.dumps(upstream_req, ensure_ascii=False))


def test_openai_chat_nonstream():
    print("\n== OpenAI chat（非流式 + usage 审计） ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_nonstream_response(text="我来自 Codex。"))
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/chat/completions", body)
    out = json.loads(resp.read().decode("utf-8"))
    check("非流式正文还原 Codex", "Codex" in out["choices"][0]["message"]["content"])
    check("usage 透传", out.get("usage", {}).get("total_tokens") == 30)


def test_responses_stream():
    print("\n== Responses（流式，事件序列对齐 Codex CLI） ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response(text="你好！", reasoning="用户打招呼"))
    body = {"model": "qwen3.8-max", "stream": True, "input": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/responses", body)
    events = read_sse(resp)
    names = [ev for ev, _ in events]
    check("含 response.created", "response.created" in names)
    check("含 output_item.added", "response.output_item.added" in names)
    check("含 output_text.delta", "response.output_text.delta" in names)
    check("含 response.completed", "response.completed" in names)
    # 事件顺序：created 在前，completed 在后
    check("created 在 completed 之前", names.index("response.created") < names.index("response.completed"))
    # 文本 delta 还原客户端名（mock 响应引用 Codex）
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response(text="我是 Codex 助手！", reasoning="我是 Claude 思维"))
    body = {"model": "qwen3.8-max", "stream": True, "input": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/responses", body)
    events = read_sse(resp)
    deltas = [json.loads(d).get("delta", "") for ev, d in events if ev == "response.output_text.delta"]
    check("output_text.delta 还原客户端名", any("Codex" in x for x in deltas))


def test_anthropic_stream():
    print("\n== Anthropic（流式事件序列） ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    # mock 上游响应引用我们发出的占位符（模拟模型自我认知引用），验证代理还原成原名
    spoofed = proxy._spoof_text("我是 Claude！", "test")
    s.enqueue(chat_stream_response(text=spoofed))
    body = {"model": "qwen3.8-max", "max_tokens": 100, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/messages", body)
    events = read_sse(resp)
    names = [ev for ev, _ in events]
    check("含 message_start", "message_start" in names)
    check("含 content_block_delta", "content_block_delta" in names)
    check("含 message_stop", "message_stop" in names)
    deltas = [json.loads(d).get("delta", {}).get("text", "") for ev, d in events
              if ev == "content_block_delta"]
    check("text_delta 还原客户端名", any("Claude" in x for x in deltas))


def test_block_and_redact():
    print("\n== 合规拦截 + 可逆脱敏（多值替换/还原） ==")
    # 敏感词仍拦截（不可逆）
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "法轮功是什么"}]}
    try:
        http_req("POST", "/v1/chat/completions", body)
        check("敏感词应被拦截", False)
    except urllib.error.HTTPError as e:
        check("敏感词 403 拦截", e.code == 403, str(e.code))
        err = json.loads(e.read().decode("utf-8"))
        check("拦截提示统一", "禁止向外部大模型传敏感信息" in err["error"]["message"])

    # PII（手机号/身份证/车牌/银行卡）可逆替换：多条消息多个值 + 响应还原
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    body = {"model": "qwen3.8-max", "messages": [
        {"role": "user", "content": "我的手机号 13812345678 和备用 13998765432"},
        {"role": "user", "content": "银行卡 6222021234567890 身份证 11010519491231002X 车牌 京A12345"},
    ]}
    resp = http_req("POST", "/v1/chat/completions", body)
    out = json.loads(resp.read().decode("utf-8"))
    sent = _capture_last_request(s)
    sent_text = json.dumps(sent, ensure_ascii=False)
    check("上游请求不含手机号/身份证/车牌/银行卡原文",
          not any(x in sent_text for x in ["13812345678", "13998765432", "6222021234567890",
                                           "11010519491231002X", "京A12345"]))
    check("上游请求含多个 PH 占位符", sent_text.count("PH_") >= 5, sent_text)
    # 响应还原：mock 上游回显请求里的占位符 → 代理应还原成原文
    # 先从上游请求体里取出真实的占位符序列
    phs = re.findall(r"PH_[A-F0-9]{10}", sent_text)
    echo_text = "号码是 %s 和 %s" % (phs[0], phs[1]) if len(phs) >= 2 else "号码是 " + (phs[0] if phs else "")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_nonstream_response(text=echo_text))
    body2 = {"model": "qwen3.8-max", "messages": [
        {"role": "user", "content": "我的手机号 13812345678 和备用 13998765432"},
    ]}
    resp = http_req("POST", "/v1/chat/completions", body2)
    out2 = json.loads(resp.read().decode("utf-8"))
    resp_text = out2["choices"][0]["message"]["content"]
    check("响应非流式还原手机号", "13812345678" in resp_text and "13998765432" in resp_text, resp_text)

    # 密码类 + 同号去重
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_nonstream_response(text="你的密码是 PH_%s" % proxy._ph("Abc123")))
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "password=Abc123 是什么"}]}
    resp = http_req("POST", "/v1/chat/completions", body)
    out = json.loads(resp.read().decode("utf-8"))
    check("响应还原明文密码", "Abc123" in out["choices"][0]["message"]["content"])

    # 同一个手机号出现两次 → 同一占位符（去重）
    t1, _ = proxy._replace_redact("13812345678 再打 13812345678", "test")
    check("同号去重同一占位符", t1.count("PH_") == 2 and len(set(p for p in [t1[t1.find("PH_")], t1[t1.find("PH_", 8)]]) ) == 1, t1)


def test_github_ignore():
    print("\n== .github 路径跳过检查 ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_nonstream_response(text="ok"))
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "13812345678 手机号测试"}]}
    resp = http_req("POST", "/repo/.github/v1/chat/completions", body)
    out = json.loads(resp.read().decode("utf-8"))
    check(".github 路径放行（含手机号也不拦）", out["choices"][0]["message"]["content"] == "ok")


def test_models():
    print("\n== GET /v1/models ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    try:
        resp = http_req("GET", "/v1/models")
    except urllib.error.HTTPError as e:
        print("   !! 500 body:", e.read().decode("utf-8")[:300])
        check("GET /v1/models 成功", False)
        return
    out = json.loads(resp.read().decode("utf-8"))
    check("模型列表非空", len(out.get("data", [])) >= 1)
    check("含 qwen3.8-max", any(m["id"] == "qwen3.8-max" for m in out.get("data", [])))


def test_ex_account_block():
    print("\n== ex_ 个人账号拦截 ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}]}
    for uid, label in [("ex_shenyk4", "ex_ 个人账号(工号特征)"), ("ex_mazy16", "ex_ 个人账号(工号特征)")]:
        try:
            http_req("POST", "/v1/chat/completions", body, headers={"X-User-Id": uid})
            check(label + " 应被拦截", False)
        except urllib.error.HTTPError as e:
            check(label + " 403", e.code == 403, str(e.code))
    # 公司工号（非 ex_）放行账号检查
    try:
        http_req("POST", "/v1/chat/completions", body, headers={"X-User-Id": "ZG00123"})
        check("公司工号 ZG00123 放行账号检查", True)
    except urllib.error.HTTPError as e:
        check("公司工号 ZG00123 放行账号检查", False, str(e.code) + e.read().decode("utf-8")[:100])


def test_stream_pii_restore():
    print("\n== 流式 PH_ 占位符跨 chunk 还原（Responses + Anthropic） ==")
    # 先让手机号进入占位符表（请求侧脱敏），再让 mock 上游按碎片回显占位符
    t1, _ = proxy._replace_redact("我的手机号 13812345678", "test")
    ph = re.search(r"PH_[A-F0-9]{10}", t1).group(0)

    # Responses：占位符被切成 "PH_1CC0E1" "8DDC" 两块（模拟按 token 切分）
    frags_resp = ["号码是 ", ph[:7], ph[7:], " 请查收"]
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response_frags(frags_resp))
    body = {"model": "qwen3.8-max", "stream": True, "input": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/responses", body)
    events = read_sse(resp)
    deltas = [json.loads(d).get("delta", "") for ev, d in events if ev == "response.output_text.delta"]
    joined = "".join(deltas)
    check("Responses 流式 delta 还原手机号", "13812345678" in joined, joined)

    # Anthropic：占位符被切成 "PH_1CC0E18" "DDC" 两块
    frags_anth = ["手机 ", ph[:8], ph[8:], " 确认"]
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response_frags(frags_anth))
    body = {"model": "qwen3.8-max", "max_tokens": 100, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/messages", body)
    events = read_sse(resp)
    deltas = [json.loads(d).get("delta", {}).get("text", "") for ev, d in events
              if ev == "content_block_delta"]
    joined = "".join(deltas)
    check("Anthropic 流式 delta 还原手机号", "13812345678" in joined, joined)

    # 客户端名占位符跨 chunk 还原（Codex 伪装场景）
    spoofed = proxy._spoof_text("我是 Codex 助手", "test")
    m = re.search(r"Workspace_[A-F0-9]{6}", spoofed)
    sp = m.group(0)
    frags = ["我来自 ", sp[:5], sp[5:], " 工具"]
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response_frags(frags))
    body = {"model": "qwen3.8-max", "stream": True, "input": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/responses", body)
    events = read_sse(resp)
    deltas = [json.loads(d).get("delta", "") for ev, d in events if ev == "response.output_text.delta"]
    joined = "".join(deltas)
    check("Responses 流式 delta 还原客户端名", "Codex" in joined, joined)


def test_openai_chat_stream_restore_across_chunks():
    print("\n== OpenAI chat 流式透传：占位符跨 chunk 也要还原 ==")
    # openai chat 走的是透传分支（restore_body + _unspoof_walk），历史上无跨 chunk 缓冲，
    # 占位符被上游按 token 拆成两块时无法还原。这里锁定该行为。
    spoofed = proxy._spoof_text("我是 Trae 助手", "test")
    sp = re.search(r"Workspace_[A-F0-9]{6}", spoofed).group(0)
    frags = ["我用 ", sp[:len(sp)//2], sp[len(sp)//2:], "客户端 干活"]
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response_frags(frags))
    body = {"model": "qwen3.8-max", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/chat/completions", body)
    events = read_sse(resp)
    all_text = _all_text_from_chat_stream(events)
    check("openai chat 流式跨 chunk 还原客户端名", "Trae客户端" in all_text, all_text)


def test_email_redact():
    print("\n== 邮箱可逆脱敏（请求替换 + 响应还原） ==")
    # 单元级：请求侧替换
    t1, hits = proxy._replace_redact("联系 ex_mazy16@partner.midea.com 或 a.b@qq.com", "test")
    check("请求侧邮箱替换为占位符", "PH_" in t1 and "@" not in t1, t1)
    check("多个邮箱各自占位符", t1.count("PH_") == 2, t1)
    # 还原
    restored = proxy.restore_text(t1)
    check("邮箱还原原文",
          "ex_mazy16@partner.midea.com" in restored and "a.b@qq.com" in restored, restored)

    # 端到端：请求含邮箱 → 上游请求体无邮箱原文 → 响应回显占位符 → 客户端收到原文
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    body = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "联系我 ex_mazy16@partner.midea.com"}]}
    resp = http_req("POST", "/v1/chat/completions", body)
    out = json.loads(resp.read().decode("utf-8"))
    sent = _capture_last_request(s)
    sent_text = json.dumps(sent, ensure_ascii=False)
    check("上游请求不含邮箱原文", "ex_mazy16@partner.midea.com" not in sent_text, sent_text)
    check("上游请求含 PH_ 占位符", "PH_" in sent_text)
    # 响应侧还原：mock 上游回显该占位符
    ph = re.search(r"PH_[A-F0-9]{10}", sent_text).group(0)
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_nonstream_response(text="邮箱是 " + ph))
    body2 = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "我的邮箱 ex_mazy16@partner.midea.com"}]}
    resp = http_req("POST", "/v1/chat/completions", body2)
    out2 = json.loads(resp.read().decode("utf-8"))
    resp_text = out2["choices"][0]["message"]["content"]
    check("响应还原邮箱原文", "ex_mazy16@partner.midea.com" in resp_text, resp_text)

    # 流式：PH_ 占位符被模型按 token 切碎（如 "PH_1CC0E" "18DDC"），跨 chunk 必须还原
    print("  -- 邮箱流式跨 chunk 还原 --")
    for frags in (["邮箱 ", ph[:6], ph[6:], " 已确认"],
                  ["邮箱 ", ph[:4], ph[4:8], ph[8:], " 已确认"]):
        s = make_server()
        s.enqueue(orgs_response())
        s.enqueue(assistants_response())
        s.enqueue(chat_stream_response_frags(frags))
        body = {"model": "qwen3.8-max", "stream": True, "input": [{"role": "user", "content": "hi"}]}
        resp = http_req("POST", "/v1/responses", body)
        events = read_sse(resp)
        deltas = [json.loads(d).get("delta", "") for ev, d in events if ev == "response.output_text.delta"]
        joined = "".join(deltas)
        check("Responses 流式 delta 还原邮箱原文（%d 块）" % len(frags),
              "ex_mazy16@partner.midea.com" in joined, joined)

        s = make_server()
        s.enqueue(orgs_response())
        s.enqueue(assistants_response())
        s.enqueue(chat_stream_response_frags(frags))
        body = {"model": "qwen3.8-max", "max_tokens": 100, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}
        resp = http_req("POST", "/v1/messages", body)
        events = read_sse(resp)
        deltas = [json.loads(d).get("delta", {}).get("text", "") for ev, d in events
                  if ev == "content_block_delta"]
        joined = "".join(deltas)
        check("Anthropic 流式 delta 还原邮箱原文（%d 块）" % len(frags),
              "ex_mazy16@partner.midea.com" in joined, joined)


def test_stream_restore_fastpath():
    print("\n== 流式还原快路径契约（无占位符文本必须原样透传） ==")
    # 刷盘线程需先启动（生产环境由 main() 启动）
    proxy.start_writer()
    # 不含任何占位符的普通文本：_unrestore_text / _stream_restore 必须原样返回
    plain_cases = [
        "你好，我是通义千问！",
        "普通英文 hello world 123",
        "",  # 空串
        "换行\n和制表符\t混合",
        "前缀 Work 匹配但不是占位符 Workspacex",
        "PH结尾不带下划线 PH_1 PH_12x",
        "中文标点。，！？、引号\"'" + "x" * 500,
    ]
    for t in plain_cases:
        check("快路径 _unrestore_text 原样返回(%d字)" % len(t),
              proxy._unrestore_text(t) == t, repr(t[:40]))
        safe, hold = proxy._stream_restore(t, "")
        check("快路径 _stream_restore 原样返回+无残留(%d字)" % len(t),
              safe == t and hold == "", repr(t[:40]))
    # pend 非空但拼起来仍无占位符：同样原样透传
    safe, hold = proxy._stream_restore("abc", "xyz")
    check("快路径 pend+delta 无占位符", safe == "xyzabc" and hold == "", repr(safe))
    # 快路径不得误伤完整占位符（占位符仍要还原）
    t1, _ = proxy._replace_redact("我的手机号 13812345678", "fp-test")
    ph = re.search(r"PH_[A-F0-9]{10}", t1).group(0)
    restored = proxy._unrestore_text("号码是 " + ph)
    check("完整占位符仍被还原（快路径不吞占位符）", "13812345678" in restored, restored)
    # 尾部孕育：不完整前缀必须进缓冲，等下一段拼完整
    safe, hold = proxy._stream_restore(" Wor", "")
    check("尾部 Wor 进缓冲", safe == " " and hold == "Wor", repr((safe, hold)))
    safe2, hold2 = proxy._stream_restore("kspaceX", hold)
    check("Wor+kspaceX 释放非占位符", safe2 == "WorkspaceX" and hold2 == ""
          and safe + safe2 == " WorkspaceX", repr((safe2, hold2)))
    safe3, hold3 = proxy._stream_restore("号码 PH_1CC0E", "")
    check("不完整 PH_ 尾部缓冲", safe3 == "号码 " and hold3 == "PH_1CC0E", repr((safe3, hold3)))
    # 多线程并发还原：快路径下结果稳定
    errors = []
    def _worker():
        try:
            for _ in range(200):
                out = proxy._unrestore_text("纯文本无占位符内容")
                if out != "纯文本无占位符内容":
                    errors.append(out)
        except Exception as e:
            errors.append(repr(e))
    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check("并发快路径 1600 次无错误", not errors, errors[:3])


def test_buffered_audit_flush():
    print("\n== 日志/审计行缓冲 + 强制刷盘完整性 ==")
    # 直接调用审计/日志接口后，最终必须落盘完整 JSONL（每行合法 JSON）
    marker = "buf-test-%d" % int(time.time() * 1000)
    entry = {"user": marker, "note": "缓冲审计完整性", "ts": "t"}
    proxy._audit_record(entry)
    proxy._audit_pass({"user": marker, "protocol": "openai", "ts": "t"})
    proxy._log("[test] %s 缓冲日志完整性" % marker)
    flushed = proxy.flush_buffers()
    check("flush_buffers 返回 int", isinstance(flushed, int))
    # 刷盘后文件必须包含刚才的记录，且行行合法 JSON
    for path in (proxy.AUDIT_FILE, proxy.PASS_AUDIT_FILE):
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        last = json.loads(lines[-1])
        check("%s 刷盘后最后一条为本次记录" % os.path.basename(path),
              marker in json.dumps(last, ensure_ascii=False), lines[-1][:120])
        ok_json = True
        try:
            for ln in lines:
                json.loads(ln)
        except Exception:
            ok_json = False
        check("%s 全文件行行合法 JSON" % os.path.basename(path), ok_json)
    with open(proxy.LOG_FILE, "r", encoding="utf-8") as f:
        logtxt = f.read()
    check("proxy.log 刷盘后含本次日志", marker in logtxt)
    # 再发一条并关闭代理写通道 → 也必须完整落盘（兜底关闭时 flush）
    proxy._log("[test] %s 关闭前最后一条" % marker)
    proxy.shutdown_writer()
    with open(proxy.LOG_FILE, "r", encoding="utf-8") as f:
        check("shutdown_writer 后日志完整", ("关闭前最后一条" in f.read()))
    # 审计批量写 + 并发完整性：多线程各写一条，flush 后全在且行行合法
    def _audit_worker(i):
        proxy._audit_record({"user": "conc-%s-%d" % (marker, i), "i": i})
    ths = [threading.Thread(target=_audit_worker, args=(i,)) for i in range(20)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    proxy.flush_buffers()
    with open(proxy.AUDIT_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    check("并发审计 20 条全部落盘", all(("conc-%s-%d" % (marker, i)) in content for i in range(20)))
    ok_json = True
    try:
        for ln in content.splitlines():
            if ln.strip():
                json.loads(ln)
    except Exception:
        ok_json = False
    check("并发审计后行行合法 JSON", ok_json)


def test_session_cache_fastpath():
    print("\n== session 缓存命中（不重复打开 DB） ==")
    # 先正常读一次，填充缓存
    s1 = proxy._read_session()
    check("首次读取返回 session", isinstance(s1, dict) and s1.get("accessToken"))
    # 记录缓存命中基线：缓存有效期内再读，必须直接命中（时间戳不更新即未重读 DB）
    at_before = proxy._session_cache["at"]
    s2 = proxy._read_session()
    check("缓存有效期内命中且对象一致", s2 is proxy._session_cache["data"])
    check("缓存命中未重置时间戳", proxy._session_cache["at"] == at_before)
    # 单元级：_read_session 在缓存新鲜时不应触碰 sqlite（用计数探针验证）
    calls = {"n": 0}
    orig_connect = proxy.sqlite3.connect
    def _counting_connect(*a, **kw):
        calls["n"] += 1
        return orig_connect(*a, **kw)
    proxy.sqlite3.connect = _counting_connect
    try:
        proxy._read_session()
        check("缓存命中 0 次 sqlite connect", calls["n"] == 0, calls["n"])
    finally:
        proxy.sqlite3.connect = orig_connect
    # 缓存过期后应重读并重建缓存
    proxy._session_cache["at"] = 0
    s3 = proxy._read_session()
    check("缓存过期后重读 DB 成功", isinstance(s3, dict) and s3.get("accessToken"))


def test_upstream_retry():
    print("\n== 上游 400 自愈：max_tokens 超限截断 + temperature/reasoning 互斥 ==")
    # 用可编程 mock：第一次请求返回 400 max_tokens too large，第二次 200
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    err_body = json.dumps({"error": {"message":
        "litellm.BadRequestError: OpenAIException - max_tokens is too large: 200000. "
        "This model supports at most 128000 completion tokens, whereas you provided 200000.."}})
    s._queue.append(upstream_error_response(400, err_body))
    s.enqueue(chat_nonstream_response(text="重试成功"))
    body = {"model": "qwen3.8-max", "max_tokens": 200000,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = http_req("POST", "/v1/chat/completions", body)
    out = json.loads(resp.read().decode("utf-8"))
    check("max_tokens 超限自动重试成功", out["choices"][0]["message"]["content"] == "重试成功",
          json.dumps(out, ensure_ascii=False)[:200])
    # 验证第二次请求的 max_tokens 已被截到 128000
    chat_bodies = [json.loads(b.decode("utf-8")) for u, h, b in s.records
                   if b and "/chat/completions" in u]
    check("重试请求 max_tokens 截到 128000",
          len(chat_bodies) == 2 and chat_bodies[1].get("max_tokens") == 128000,
          json.dumps([b.get("max_tokens") for b in chat_bodies]))

    # temperature + reasoning_effort 互斥：thinking 请求转换后只保留 reasoning_effort
    oa = proxy.convert_anthropic_to_openai({
        "model": "gpt-5.6-luna", "max_tokens": 32000, "temperature": 1,
        "thinking": {"type": "enabled", "budget_tokens": 16000},
        "messages": [{"role": "user", "content": "hi"}],
    })
    check("thinking 时去掉 temperature 保留 reasoning_effort",
          "temperature" not in oa and oa.get("reasoning_effort") == "high",
          json.dumps(oa, ensure_ascii=False)[:200])
    # 非 thinking 请求 temperature 照常透传
    oa2 = proxy.convert_anthropic_to_openai({
        "model": "gpt-5.6-luna", "max_tokens": 1000, "temperature": 0.7,
        "messages": [{"role": "user", "content": "hi"}],
    })
    check("非 thinking 保留 temperature", oa2.get("temperature") == 0.7)

    # 非法模型名 404 不应触发重试
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    body = {"model": "no-such-model", "messages": [{"role": "user", "content": "hi"}]}
    try:
        http_req("POST", "/v1/chat/completions", body)
        check("未知模型 404", False)
    except urllib.error.HTTPError as e:
        check("未知模型 404", e.code == 404, str(e.code))


def test_models_singleflight():
    s = make_server()
    # 慢上游：第 0 个响应直接返回（orgs），assistants 延迟 300ms
    s.enqueue(orgs_response())
    s.enqueue(assistants_response(), headers={"X-Mock-Delay": "0.3"})
    # 更慢场景：直接 patch _http_json 加延迟更可控
    orig_http = proxy._http_json
    state = {"calls": 0, "lock": threading.Lock()}
    def _slow_http(url, headers, body=None, method=None, timeout=120):
        with state["lock"]:
            state["calls"] += 1
        if "list-organizations" in url:
            import io as _io
            class _R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self, n=-1): return orgs_response().encode("utf-8")
            return _R()
        time.sleep(0.3)  # 模拟上游慢响应，放大并发窗口
        class _R2:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1): return assistants_response().encode("utf-8")
        return _R2()
    proxy._http_json = _slow_http
    try:
        with proxy._models_lock:
            proxy._models_cache.update({"at": 0.0, "map": {}})  # 强制 miss
        results = []
        errs = []
        def _fetch():
            try:
                results.append(proxy.fetch_models({"accessToken": "fake"}))
            except Exception as e:
                errs.append(repr(e))
        ths = [threading.Thread(target=_fetch) for _ in range(10)]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        check("10 并发 fetch 无异常", not errs, errs[:2])
        check("全部拿到相同 map", all(r is results[0] for r in results), len(results))
        # single-flight 核心断言：慢上游（assistants）只被真实调用一次
        check("上游仅被调用一次（single-flight）", state["calls"] == 2,
              "calls=%d (期望 2: 1 orgs + 1 assistants)" % state["calls"])
    finally:
        proxy._http_json = orig_http
    # 缓存命中路径：热缓存下再并发取，0 次上游调用
    state["calls"] = 0
    results2 = []
    ths = [threading.Thread(target=lambda: results2.append(proxy.fetch_models({"accessToken": "fake"})))
           for _ in range(10)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    check("热缓存并发 0 次上游调用", state["calls"] == 0, state["calls"])
    check("热缓存返回一致", all(r is results[0] for r in results2))


def test_spoof_dispatch():
    print("\n== 客户端名伪装（_spoof_text 幂等 + 占位符表） ==")
    out = proxy._spoof_text("我是 Codex CLI 用户，来自 Anthropic 公司", "test")
    check("Codex CLI 与 Anthropic 均被替换", "Codex CLI" not in out and "Anthropic" not in out
          and out.count("Workspace_") == 2, out)
    # 同词去重：同一客户端名出现两次 → 同一占位符
    out2 = proxy._spoof_text("Codex 和 Codex", "test")
    phs = re.findall(r"Workspace_[A-F0-9]{6}", out2)
    check("同词去重同一占位符", len(phs) == 2 and phs[0] == phs[1], out2)
    # 幂等：已占位的文本再走一次不变（不套娃）
    once = proxy._spoof_text("我是 Codex", "test")
    twice = proxy._spoof_text(once, "test")
    check("二次伪装幂等", twice == once, twice)
    # Claude Code 与 ZCode（及带空格变体 Z Code）应被替换为 Workspace 占位符
    out3 = proxy._spoof_text("我同时用 Claude Code 和 ZCode，以及 Z Code 变体", "test")
    check("Claude Code 被替换", "Claude Code" not in out3 and "Claude" not in out3, out3)
    check("ZCode / Z Code 均被替换", "ZCode" not in out3 and "Z Code" not in out3
          and out3.count("Workspace_") >= 3, out3)
    # 还原：占位符能精确还原回 Claude Code 与 ZCode 原名
    restored = proxy._unspoof_text(out3)
    check("还原 Claude Code 与 ZCode 原名",
          "Claude Code" in restored and "ZCode" in restored and "Z Code" in restored, restored)
    # 边界：仅命中独立词，不误伤普通单词（如 coding / encode 中的 "code" 不触发）
    out4 = proxy._spoof_text("this is some encode and decode work", "test")
    check("普通单词不误伤", out4 == "this is some encode and decode work", out4)
    # OpenAI / MCP 是协议名/公司名，不是第三方客户端，不应被伪装
    out5 = proxy._spoof_text("我用 OpenAI 的 MCP 协议", "test")
    check("OpenAI / MCP 不伪装", "OpenAI" in out5 and "MCP" in out5
          and "Workspace_" not in out5, out5)
    # 中文连写边界：客户端名后面直接跟中文也必须伪装（\b 对中文失效的回归）
    out6 = proxy._spoof_text("我用 Trae客户端 和 Codex助手 干活", "test")
    check("客户端名连中文仍伪装", "Trae" not in out6 and "Codex" not in out6
          and "Trae客户端" not in out6 and "Codex助手" not in out6, out6)


def test_spoof_placeholder_stability():
    print("\n== 客户端名占位符由原词稳定决定（实现优化后不得改变） ==")
    # 占位符 = sha1(原词) 前 6 位，与 _spoof_text 内部实现无关。改实现后仍需一致。
    for orig in ["Codex", "Claude Code", "ZCode", "Z Code", "Trae"]:
        expected = "Workspace_" + __import__("hashlib").sha1(orig.encode("utf-8")).hexdigest()[:6].upper()
        out = proxy._spoof_text("我用 %s 干活" % orig, "test")
        check("占位符稳定(%s)" % orig, expected in out, out)
    # 中文连写还原：占位符后面直接跟中文也必须还原回原名（\b 对中文失效的回归）
    spoofed = proxy._spoof_text("我用 Trae 干活", "test")
    m = re.search(r"Workspace_[A-F0-9]{6}", spoofed)
    assert m, spoofed
    ph = m.group(0)
    restored = proxy._unspoof_text("上游回复：%s客户端" % ph)
    check("占位符连中文仍还原", "Trae客户端" in restored, restored)
    # 流式快路径也走 _unrestore_text，连写还原应一致
    restored_stream, hold = proxy._stream_restore("上游回复：%s客户端" % ph, "")
    check("流式还原连中文", "Trae客户端" in restored_stream and hold == "", restored_stream)
    print("\n== 客户端名占位符由原词稳定决定（实现优化后不得改变） ==")
    # 占位符 = sha1(原词) 前 6 位，与 _spoof_text 内部实现无关。改实现后仍需一致。
    for orig in ["Codex", "Claude Code", "ZCode", "Z Code", "Trae"]:
        expected = "Workspace_" + __import__("hashlib").sha1(orig.encode("utf-8")).hexdigest()[:6].upper()
        out = proxy._spoof_text("我用 %s 干活" % orig, "test")
        check("占位符稳定(%s)" % orig, expected in out, out)


def test_count_tokens():
    print("\n== Anthropic count_tokens（本地估算，不转发上游） ==")
    body = {"model": "qwen3.8-max",
            "messages": [{"role": "user", "content": "你好世界，这是测试 token 估算的一段文本"}]}
    resp = http_req("POST", "/v1/messages/count_tokens", body)
    out = json.loads(resp.read().decode("utf-8"))
    check("count_tokens 返回 input_tokens 且 >=1",
          isinstance(out.get("input_tokens"), int) and out["input_tokens"] >= 1, out)


def test_responses_parallel_tool_calls():
    print("\n== Responses parallel_tool_calls 仅在声明 tools 时透传 ==")
    # codex 即使不带 tools 也会发 parallel_tool_calls=false；
    # 上游 litellm 会 400（parallel_tool_calls is only allowed when 'tools' are specified），
    # 因此无 tools 时必须丢弃该字段。
    oa = proxy.convert_responses_to_openai({
        "model": "gpt-5.6-luna",
        "input": "你好",
        "parallel_tool_calls": False,
    })
    check("无 tools 时丢弃 parallel_tool_calls", "parallel_tool_calls" not in oa, oa)
    # 有 function tools 时应透传
    oa2 = proxy.convert_responses_to_openai({
        "model": "gpt-5.6-luna",
        "input": "你好",
        "parallel_tool_calls": True,
        "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
    })
    check("有 tools 时透传 parallel_tool_calls", oa2.get("parallel_tool_calls") is True, oa2)


def test_client_abort_no_traceback():
    print("\n== 客户端中途断开：不产生 traceback，服务照常 ==")
    # 流式：响应写出过程中客户端直接关闭连接（模拟客户端超时/取消）
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_stream_response(text="你好！"))
    # 构造一个原始 socket 客户端，收到首个字节后立即关闭（模拟客户端中途放弃）
    import socket as _socket
    body = json.dumps({"model": "qwen3.8-max", "stream": True,
                       "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
    port = int(os.environ["WS_PROXY_PORT"])
    sock = _socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n"
                 b"Host: 127.0.0.1\r\nContent-Type: application/json\r\n"
                 + ("Content-Length: %d\r\n" % len(body)).encode("utf-8")
                 + b"Connection: close\r\n\r\n" + body)
    sock.settimeout(3)
    try:
        first = sock.recv(1)   # 等服务端开始写响应
        sock.close()           # 主动断开 → 服务端写盘触发 ConnectionResetError，但不打印 traceback
        check("客户端收到首字节后断开", bool(first), repr(first))
    except _socket.timeout:
        sock.close()
        check("客户端收到首字节后断开", False, "等待服务端响应超时")
    # 服务端线程是 daemon，无需等待；再次发一个正常请求，确认服务没被弄挂
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    s.enqueue(chat_nonstream_response(text="ok"))
    resp = http_req("POST", "/v1/chat/completions",
                    {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}]})
    out = json.loads(resp.read().decode("utf-8"))
    print("  后续正常请求响应:", json.dumps(out, ensure_ascii=False)[:200])
    check("客户端断开后服务仍可正常服务", out.get("choices", [{}])[0].get("message", {}).get("content") == "ok",
          json.dumps(out, ensure_ascii=False)[:300])


def test_auto_refresh_trigger():
    print("\n== accessToken 临期/过期自动刷新（refreshToken 续期） ==")
    # 用 mock 记录刷新请求；DB 里预置 refreshToken + 临期 accessToken
    s = make_server()
    now = int(time.time())
    near_exp = now + 60
    far_rt = "rt-old-token"
    rows = [("default", "k" * 64,
             _xor_enc("k" * 64, {"accessToken": _jwt(near_exp), "id": "ex_mazy16",
                                 "label": "麦志业", "refreshToken": far_rt}), now)]
    orig_connect = proxy.sqlite3.connect
    proxy.sqlite3.connect = lambda *a, **kw: _DummyDB(rows)
    try:
        with proxy._session_lock:
            proxy._session_cache.update({"at": 0, "data": None})
        # mock 上游响应顺序：_read_session 先触发 refresh（消费第 1 个），
        # 之后 get_models 消费 orgs+assistants，最后转发消费 chat 响应。
        new_at = _jwt(now + 3600)
        s.enqueue(refresh_response(access_token=new_at, refresh_token="rt-new-token"))
        s.enqueue(orgs_response())
        s.enqueue(assistants_response())
        s.enqueue(chat_nonstream_response(text="续期成功"))
        resp = http_req("POST", "/v1/chat/completions",
                        {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}]})
        out = json.loads(resp.read().decode("utf-8"))
        check("临期自动刷新后请求成功", out.get("choices", [{}])[0].get("message", {}).get("content") == "续期成功",
              json.dumps(out, ensure_ascii=False)[:200])
        # 刷新请求应带 Refresh-Token 头，且 URL 指向 refresh-token 端点
        refresh_reqs = [(u, h) for u, h, b in s.records if "refresh-token" in u]
        check("触发了一次刷新请求", len(refresh_reqs) == 1, len(refresh_reqs))
        if refresh_reqs:
            check("刷新请求带 Refresh-Token 头", refresh_reqs[0][1].get("Refresh-Token") == far_rt,
                  refresh_reqs[0][1])
        # 刷新后会话应使用新 accessToken（剥掉 Bearer 前缀）
        cur = proxy._session_cache["data"]
        check("刷新后缓存使用新 accessToken", cur and cur["accessToken"] == new_at,
              "缓存 accessToken 是否更新")
        check("刷新后缓存 refreshToken 更新", cur and cur["refreshToken"] == "rt-new-token")
    finally:
        proxy.sqlite3.connect = orig_connect


def test_auto_refresh_fail_prompt():
    print("\n== 刷新失败提示重新登录 ==")
    s = make_server()
    s.enqueue(orgs_response())
    s.enqueue(assistants_response())
    now = int(time.time())
    near_exp = now + 60
    rows = [("default", "k" * 64,
             _xor_enc("k" * 64, {"accessToken": _jwt(near_exp), "id": "ex_mazy16",
                                 "label": "麦志业", "refreshToken": "rt-old-token"}), now)]
    orig_connect = proxy.sqlite3.connect
    proxy.sqlite3.connect = lambda *a, **kw: _DummyDB(rows)
    try:
        with proxy._session_lock:
            proxy._session_cache.update({"at": 0, "data": None})
        # mock 上游：refresh-token 端点返回 success=false（SSO 会话到期）
        s.enqueue(json.dumps({"success": False, "body": {}}))
        try:
            http_req("POST", "/v1/chat/completions",
                     {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "hi"}]})
            check("刷新失败应返回 500", False)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            check("刷新失败返回 500", e.code == 500, str(e.code))
            check("错误信息提示重新登录", "重新登录" in body, body[:200])
    finally:
        proxy.sqlite3.connect = orig_connect


def test_refresh_strips_bearer_and_writes_refresh():
    print("\n== 刷新函数：剥 Bearer 前缀 + 写回 refreshToken ==")
    s = make_server()
    new_at = _jwt(int(time.time()) + 3600)
    s.enqueue(refresh_response(access_token=new_at, refresh_token="rt-new-token"))
    res = proxy._refresh_token_via_refresh("rt-old-token")
    check("刷新返回新 accessToken（已剥 Bearer）", res["accessToken"] == new_at, res)
    check("刷新返回新 refreshToken", res["refreshToken"] == "rt-new-token", res)


def main():
    tests = [test_models, test_openai_chat, test_openai_chat_nonstream,
             test_responses_stream, test_anthropic_stream,
             test_block_and_redact, test_github_ignore, test_ex_account_block,
             test_stream_pii_restore, test_openai_chat_stream_restore_across_chunks,
             test_email_redact,
             test_spoof_dispatch, test_spoof_placeholder_stability,
             test_responses_parallel_tool_calls, test_count_tokens,
             test_stream_restore_fastpath, test_buffered_audit_flush,
             test_session_cache_fastpath, test_models_singleflight,
             test_upstream_retry, test_client_abort_no_traceback,
             test_auto_refresh_trigger, test_auto_refresh_fail_prompt,
             test_refresh_strips_bearer_and_writes_refresh]

    # 起真实代理（mock 模式下不触网）
    server = proxy.Handler
    httpd = proxy.http.server.ThreadingHTTPServer(("127.0.0.1", int(os.environ["WS_PROXY_PORT"])), server)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)

    try:
        for fn in tests:
            fn()
    finally:
        try:
            proxy.shutdown_writer()
        except Exception:
            pass
        httpd.shutdown()

    print("\n======== 结果：%d 通过, %d 失败 ========" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败项：", FAILED)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())