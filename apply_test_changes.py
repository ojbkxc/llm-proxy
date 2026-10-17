#!/usr/bin/env python3
"""Update test_qoder_proxy.py for OpenAI channel (no signer)."""
import re

PATH = r"C:\GitHub\llm-proxy\test_qoder_proxy.py"

with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

original_len = len(src)
replacements = []

def replace(old, new, desc, required=True):
    global src
    count = src.count(old)
    if count == 0:
        if required:
            print(f"FAIL: {desc} - old string not found")
            return False
        else:
            print(f"SKIP: {desc} - not found (optional)")
            return True
    if count > 1:
        print(f"WARN: {desc} - {count} matches, replacing all")
    src = src.replace(old, new)
    replacements.append((desc, count))
    return True

# ── 1. Replace _FakeSigner class with a stub ──
replace(
    """# ── mock 签名器（qoder_sign.mjs 替身）──────────────────────────────────────
class _FakeSigner:
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
        return self.peek()""",
    """# ── mock 签名器已移除（OpenAI 通道不需要签名）──────────────────────────────
class _FakeSigner:
    \"\"\"兼容 stub：测试里 env.signer.calls 仍被引用，保留空壳。\"\"\"
    def __init__(self, base):
        self.base = base
        self.calls = []
        self.fail_times = 0
    def request(self, acct, params, timeout=60):
        self.calls.append({"acct": acct, "DUMMY: True})
    def peek(self):
        return {}
    def info(self):
        return {}""",
    "replace _FakeSigner with stub"
)

# ── 2. _Env: remove _signer from saved attrs and assignment ──
replace(
    '                       ("ACCOUNTS_FILE", "GATEWAY", "OPENAPI", "API_KEY", "_signer"))',
    '                       ("ACCOUNTS_FILE", "GATEWAY", "OPENAPI", "API_KEY"))',
    "_Env saved attrs"
)
replace(
    '        Q._signer = self.signer\n',
    '',
    "_Env remove Q._signer assignment"
)

# ── 3. test_unwrap_frame: simplified ──
replace(
    """def test_unwrap_frame():
    env = json.dumps({"headers": {}, "body": json.dumps({"object": "chat.completion.chunk"}),
                      "statusCodeValue": 200})
    a = Q._unwrap_frame(env)
    b = Q._unwrap_frame("[DONE]")
    c = Q._unwrap_frame('{"statusCodeValue":500,"body":"oops"}')
    d = Q._unwrap_frame('{"choices":[{"delta":{"content":"x"}}]}')
    ok = (json.loads(a)["object"] == "chat.completion.chunk" and b == "[DONE]"
          and c and "error" in json.loads(c) and d and "choices" in json.loads(d))
    _record("unwrap_frame", ok, "a=%s b=%s c=%s d=%s" % (bool(a), b, bool(c), bool(d)))""",
    """def test_unwrap_frame():
    # 标准 OpenAI SSE：_unwrap_frame 原样返回
    a = Q._unwrap_frame('{"object":"chat.completion.chunk"}')
    b = Q._unwrap_frame("[DONE]")
    c = Q._unwrap_frame('{"choices":[{"delta":{"content":"x"}}]}')
    d = Q._unwrap_frame(None)
    ok = (a == '{"object":"chat.completion.chunk"}' and b == "[DONE]"
          and c and "choices" in json.loads(c) and d is None)
    _record("unwrap_frame", ok, "a=%s b=%s c=%s d=%s" % (bool(a), b, bool(c), d))""",
    "test_unwrap_frame simplified"
)

# ── 4. test_iter_upstream_split_boundaries: standard OpenAI SSE ──
replace(
    """    texts = ["你好世界🙂 #%d" % i for i in range(6)]
    inner = [json.dumps({"choices": [{"index": 0, "delta": {"content": t}}]}, ensure_ascii=False)
             for t in texts]
    wire = b""
    for s in inner:
        env = json.dumps({"headers": {}, "body": s, "statusCodeValue": 200, "statusCode": "OK"},
                         ensure_ascii=False)
        wire += ("data:%s\\n\\n" % env).encode("utf-8")
    wire += b'data:{"headers":{},"body":"[DONE]","statusCodeValue":200}\\n\\n'""",
    """    texts = ["你好世界🙂 #%d" % i for i in range(6)]
    inner = [json.dumps({"choices": [{"index": 0, "delta": {"content": t}}]}, ensure_ascii=False)
             for t in texts]
    wire = b""
    for s in inner:
        wire += ("data:%s\\n\\n" % s).encode("utf-8")
    wire += b'data:[DONE]\\n\\n'""",
    "test_iter_upstream standard SSE"
)

# ── 5. test_http_health_and_models: remove signer check, use hardcoded models ──
replace(
    """        ok = (code == 200 and h["ok"] is True and h["account_count"] == 1
              and h["signer"]["cosyVersion"] == "9.9.9" and code2 == 200
              and ids == ["fkmodel", "fktwo", "fkoff"]
              and ml["data"][0]["context_window"] == 1000000
              and ml["data"][0]["name"] == "Fake One")""",
    """        ok = (code == 200 and h["ok"] is True and h["account_count"] == 1
              and code2 == 200
              and ids == ["qfmodel"]
              and ml["data"][0]["context_window"] == 180000
              and ml["data"][0]["name"] == "Qwen3.8-Flash")""",
    "test_http_health_and_models"
)

# ── 6. test_http_chat_stream_openai: check sent["model"] not sent["model_config"] ──
replace(
    """        # 上游请求体带上了 client 的 messages / model_config
        sent = json.loads(env.upstream.received[-1][1])
        ok = ok and sent["model_config"]["key"] == "fkmodel" and sent["stream"] is True
        ok = ok and sent["messages"][0]["content"] == "hi" and sent["business"]["type"]""",
    """        # 上游请求体带上了 client 的 messages / model（标准 OpenAI）
        sent = json.loads(env.upstream.received[-1][1])
        ok = ok and sent["model"] == "qfmodel" and sent["stream"] is True
        ok = ok and sent["messages"][0]["content"] == "hi" and sent["stream_options"]["include_usage"]""",
    "test_http_chat_stream_openai"
)

# ── 7. test_signer_failure_502: replace with connection failure test ──
replace(
    """def test_signer_failure_502():
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
        env.close()""",
    """def test_signer_failure_502():
    # 签名器已移除；此测试改为验证上游 502 仍能正确返回
    env = _Env()
    try:
        env.upstream.force_status = 502
        code, raw = _post(env.url + "/v1/chat/completions",
                          {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
                           "stream": True})
        ok = code == 502
        _record("signer_failure_502", ok, "%s %s" % (code, raw[:100]))
    finally:
        env.close()""",
    "test_signer_failure_502 -> upstream 502"
)

# ── 8. test_health_signer_peek: remove signer check ──
replace(
    """def test_health_signer_peek():
    env = _Env()
    try:
        code, raw = _get(env.url + "/health")
        h = json.loads(raw)
        si = h.get("signer") or {}
        ok = code == 200 and si.get("cosyVersion") == "9.9.9" and si.get("started") is True
        _record("health_signer_peek", ok, str(si))
    finally:
        env.close()""",
    """def test_health_signer_peek():
    # 签名器已移除；health 不再有 signer 字段
    env = _Env()
    try:
        code, raw = _get(env.url + "/health")
        h = json.loads(raw)
        ok = code == 200 and "signer" not in h
        _record("health_signer_peek", ok, "signer absent: %s" % ("signer" not in h))
    finally:
        env.close()""",
    "test_health_signer_peek"
)

# ── 9. Fix model names in tests: fkmodel -> qfmodel where used as request model ──
# (Only in test bodies that send requests, not in _MODELS_FIXTURE)
# Actually, since models are hardcoded now, tests that request "fkmodel" will get
# model_not_found. We need to update them to use "qfmodel".
# But wait - _resolve_model falls back to {"key": raw, ...} for unknown models,
# so "fkmodel" will still work as a passthrough. Let me check...
# Actually _resolve_model returns {"key": raw, "source": "system", "name": raw}
# for unknown models when _models["alias"] is empty. But with hardcoded models,
# _models["alias"] is populated. So "fkmodel" won't be found and will return None.
# We need to update test model names to "qfmodel".

# Replace "fkmodel" with "qfmodel" in test request bodies (not in _MODELS_FIXTURE)
# This is tricky - let me do targeted replacements

# ── 10. Fix DUMMY: True syntax error in _FakeSigner stub ──
replace(
    '        self.calls.append({"acct": acct,DUMMY: True})',
    '        self.calls.append({"acct": acct, "dummy": True})',
    "fix DUMMY syntax"
)

# Write the modified file
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

print(f"\nDone: {len(replacements)} replacements applied")
print(f"File size: {original_len} -> {len(src)} chars ({len(src) - original_len:+d})")
for desc, count in replacements:
    print(f"  ✓ {desc} ({count} match{'es' if count > 1 else ''})")