#!/usr/bin/env python3
"""
trae_proxy.py 离线测试套件。

单元测试（不触网）：
  - 协议转换函数（Responses / Anthropic ↔ OpenAI）
  - 流式状态机（_ResponsesStreamState / _AnthropicStreamState）
  - parse_anthropic_text

集成测试（可选，需代理在 127.0.0.1:8790 运行）：
  - health / models / chat 非流式 / chat 流式 / anthropic / responses
  - 只用 "hello" 等无害占位内容

用法：
  python test_trae_proxy.py              # 只跑单元测试
  python test_trae_proxy.py --integration # 单元 + 集成测试
"""
import json
import sys
import unittest
import urllib.request

import trae_proxy as tp


# ── 协议转换：Responses → OpenAI ──────────────────────────────────────────────
class TestConvertResponsesToOpenAI(unittest.TestCase):
    def test_input_string(self):
        oa = tp.convert_responses_to_openai({"model": "m", "input": "hello"})
        self.assertEqual(oa["model"], "m")
        self.assertEqual(oa["messages"], [{"role": "user", "content": "hello"}])

    def test_input_list_messages(self):
        oa = tp.convert_responses_to_openai({"input": [
            {"type": "message", "role": "user", "content": "hi"},
            {"type": "message", "role": "assistant", "content": "hey"},
        ]})
        roles = [m["role"] for m in oa["messages"]]
        self.assertEqual(roles, ["user", "assistant"])

    def test_instructions_to_system(self):
        oa = tp.convert_responses_to_openai({"instructions": "be nice", "input": "hi"})
        self.assertEqual(oa["messages"][0], {"role": "system", "content": "be nice"})

    def test_max_output_tokens(self):
        oa = tp.convert_responses_to_openai({"input": "x", "max_output_tokens": 50})
        self.assertEqual(oa["max_tokens"], 50)

    def test_image_placeholder(self):
        oa = tp.convert_responses_to_openai({"input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "看图"},
                {"type": "input_image", "image_url": "data:image/png;base64,xxx"},
            ]},
        ]})
        combined = " ".join(m.get("content", "") for m in oa["messages"] if m.get("content"))
        self.assertIn("[图片输入暂不支持]", combined)


# ── 协议转换：Anthropic → OpenAI ──────────────────────────────────────────────
class TestConvertAnthropicToOpenAI(unittest.TestCase):
    def test_system_string(self):
        oa = tp.convert_anthropic_to_openai({"system": "sys", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(oa["messages"][0], {"role": "system", "content": "sys"})

    def test_system_list(self):
        oa = tp.convert_anthropic_to_openai({"system": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "messages": []})
        self.assertEqual(oa["messages"][0]["content"], "a\nb")

    def test_assistant_tool_use(self):
        oa = tp.convert_anthropic_to_openai({"messages": [{"role": "assistant", "content": [
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {"x": 1}},
        ]}]})
        msg = oa["messages"][0]
        self.assertEqual(msg["content"], "ok")
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "echo")
        self.assertEqual(json.loads(msg["tool_calls"][0]["function"]["arguments"]), {"x": 1})

    def test_image_placeholder(self):
        oa = tp.convert_anthropic_to_openai({"messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "data": "xxx"}},
        ]}]})
        combined = " ".join(m.get("content", "") for m in oa["messages"] if m.get("content"))
        self.assertIn("[图片输入暂不支持]", combined)


# ── 协议转换：OpenAI → Responses ──────────────────────────────────────────────
class TestConvertOpenAIToResponses(unittest.TestCase):
    def _make(self, **kw):
        msg = {"role": "assistant", "content": "hello", **kw}
        return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}

    def test_basic(self):
        r = tp.convert_openai_to_responses(self._make(), "m")
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["output_text"], "hello")
        self.assertEqual(r["usage"]["total_tokens"], 3)

    def test_reasoning(self):
        r = tp.convert_openai_to_responses(self._make(reasoning_content="think"), "m")
        self.assertTrue(any(o["type"] == "reasoning" for o in r["output"]))

    def test_tool_calls(self):
        tc = {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{\"x\":1}"}}
        r = tp.convert_openai_to_responses(self._make(content=None, tool_calls=[tc]), "m")
        fcs = [o for o in r["output"] if o["type"] == "function_call"]
        self.assertEqual(len(fcs), 1)
        self.assertEqual(fcs[0]["name"], "echo")

    def test_incomplete(self):
        obj = {"choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "length"}], "usage": {}}
        r = tp.convert_openai_to_responses(obj, "m")
        self.assertEqual(r["status"], "incomplete")


# ── 协议转换：OpenAI → Anthropic ──────────────────────────────────────────────
class TestConvertOpenAIToAnthropic(unittest.TestCase):
    def _make(self, **kw):
        msg = {"role": "assistant", "content": "hello", **kw}
        return {"choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

    def test_basic(self):
        r = tp.convert_openai_to_anthropic(self._make(), "m")
        self.assertEqual(r["type"], "message")
        self.assertEqual(r["content"][0], {"type": "text", "text": "hello"})
        self.assertEqual(r["stop_reason"], "end_turn")

    def test_thinking(self):
        r = tp.convert_openai_to_anthropic(self._make(reasoning_content="think"), "m")
        self.assertEqual(r["content"][0]["type"], "thinking")

    def test_tool_use(self):
        tc = {"id": "t1", "type": "function", "function": {"name": "echo", "arguments": "{\"x\":1}"}}
        obj = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [tc]}, "finish_reason": "tool_calls"}], "usage": {}}
        r = tp.convert_openai_to_anthropic(obj, "m")
        self.assertEqual(r["stop_reason"], "tool_use")
        self.assertEqual(r["content"][0]["type"], "tool_use")

    def test_stop_reason_length(self):
        obj = {"choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "length"}], "usage": {}}
        r = tp.convert_openai_to_anthropic(obj, "m")
        self.assertEqual(r["stop_reason"], "max_tokens")


# ── parse_anthropic_text ──────────────────────────────────────────────────────
class TestParseAnthropicText(unittest.TestCase):
    def test_mixed(self):
        body = {"system": "sys", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "id": "t", "name": "f", "input": {}}]},
        ]}
        texts = tp.parse_anthropic_text(body)
        self.assertIn("sys", texts)
        self.assertIn("hi", texts)
        self.assertIn("ok", texts)


# ── 流式状态机：_ResponsesStreamState ─────────────────────────────────────────
class TestResponsesStreamState(unittest.TestCase):
    def test_text_stream(self):
        st = tp._ResponsesStreamState("m")
        events = []
        for d in ["hel", "lo"]:
            events += st.feed({"choices": [{"delta": {"content": d}}]})
        events += st.feed({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
        events += st.finish()
        types = [e[0] for e in events]
        self.assertIn("response.created", types)
        self.assertIn("response.output_text.delta", types)
        self.assertIn("response.completed", types)

    def test_tool_stream(self):
        st = tp._ResponsesStreamState("m")
        events = st.feed({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "echo", "arguments": "{\"x\":1}"}}]}}]})
        events += st.feed({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        events += st.finish()
        types = [e[0] for e in events]
        self.assertIn("response.completed", types)
        # 验证 completed 事件里包含 function_call
        completed = [e for e in events if e[0] == "response.completed"][0]
        obj = json.loads(completed[1])
        item_types = [o["type"] for o in obj["response"]["output"]]
        self.assertIn("function_call", item_types)


# ── 流式状态机：_AnthropicStreamState ─────────────────────────────────────────
class TestAnthropicStreamState(unittest.TestCase):
    def test_text_stream(self):
        st = tp._AnthropicStreamState("m")
        events = st.feed({"choices": [{"delta": {"content": "hi"}}]})
        events += st.feed({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
        events += st.finish()
        types = [e[0] for e in events]
        self.assertIn("message_start", types)
        self.assertIn("content_block_delta", types)
        self.assertIn("message_stop", types)


# ── 错误码映射 ─────────────────────────────────────────────────────────────────
class TestErrorMapping(unittest.TestCase):
    def test_auth_error(self):
        self.assertEqual(tp._map_upstream_error("1005", "session expired"), 401)
        self.assertEqual(tp._map_upstream_error(None, "token invalid"), 401)

    def test_rate_limit(self):
        self.assertEqual(tp._map_upstream_error("429", "rate limit exceeded"), 429)
        self.assertEqual(tp._map_upstream_error(None, "too many requests"), 429)

    def test_content_security(self):
        self.assertEqual(tp._map_upstream_error("1006", "content_security"), 400)
        self.assertEqual(tp._map_upstream_error(None, "sensitive content"), 400)

    def test_server_error(self):
        self.assertEqual(tp._map_upstream_error("500", "internal error"), 500)
        self.assertEqual(tp._map_upstream_error("503", "unavailable"), 503)

    def test_unknown(self):
        self.assertEqual(tp._map_upstream_error("9999", "unknown"), 502)
        self.assertEqual(tp._map_upstream_error(None, ""), 502)


# ── 集成测试（可选，需代理运行） ──────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    BASE = "http://127.0.0.1:8790"

    @classmethod
    def setUpClass(cls):
        try:
            urllib.request.urlopen(cls.BASE + "/health", timeout=3)
            cls.available = True
        except Exception:
            cls.available = False

    def setUp(self):
        if not self.available:
            self.skipTest("代理未运行")

    def _post(self, path, body, stream=False):
        r = urllib.request.Request(self.BASE + path, data=json.dumps(body).encode(), method="POST")
        r.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(r, timeout=30)
        if stream:
            chunks = sum(1 for line in resp if line.strip())
            return resp.status, chunks
        return resp.status, resp.read().decode()

    def test_health(self):
        s = urllib.request.urlopen(self.BASE + "/health", timeout=5).status
        self.assertEqual(s, 200)

    def test_models(self):
        s = urllib.request.urlopen(self.BASE + "/v1/models", timeout=5).status
        self.assertEqual(s, 200)

    def test_chat_nonstream(self):
        s, b = self._post("/v1/chat/completions", {"model": "glm-5.2", "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(s, 200)

    def test_chat_stream(self):
        s, chunks = self._post("/v1/chat/completions", {"model": "glm-5.2", "messages": [{"role": "user", "content": "hello"}], "stream": True}, stream=True)
        self.assertEqual(s, 200)
        self.assertGreater(chunks, 3)

    def test_anthropic(self):
        s, b = self._post("/v1/messages", {"model": "glm-5.2", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 50})
        self.assertEqual(s, 200)

    def test_responses(self):
        s, b = self._post("/v1/responses", {"model": "glm-5.2", "input": "hello", "max_output_tokens": 50})
        self.assertEqual(s, 200)


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    if "--integration" not in sys.argv:
        suite = unittest.TestSuite([t for t in suite if t.__class__.__name__ != "TestIntegration"])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)