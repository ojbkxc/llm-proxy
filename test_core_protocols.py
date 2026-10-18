# -*- coding: utf-8 -*-
"""
test_core_protocols.py — llm_proxy_core 的契约测试（两个代理共用这一层）

为什么单独一份：qoder_proxy 与 codearts_proxy 曾经各写一遍协议层，44 个同名函数
没有一个字节相同，漂到 usage 键有无、工具名拼接方式都不一致，同一个客户端打两个
后端会得到两种行为。这里的每个用例都钉住一个当初分叉的点，规定"唯一的正确答案"。

运行：python test_core_protocols.py
约束：纯标准库、纯假数据、不触网、不读真实账号池。
"""
import io
import json
import os
import sys

# 必须在 import 两个代理之前生效：它们会按同目录的 *_accounts.json 解析账号池，
# 指到不存在的临时路径，测试就绝不会碰真凭证。
os.environ.setdefault("QODER_PROXY_ACCOUNTS", os.devnull + "-nonexistent.json")
os.environ.setdefault("CODEARTS_PROXY_ACCOUNTS_FILE", os.devnull + "-nonexistent.json")

import llm_proxy_core as C
import codearts_proxy as A
import qoder_proxy as Q

_results = []


def _record(name, ok, detail=""):
    _results.append((name, ok, detail))
    print("[%-4s] %s%s" % ("PASS" if ok else "FAIL", name,
                           (" - " + str(detail)[:160]) if detail else ""))


def _chunk(**kw):
    """造一个上游 chat.completion.chunk。delta 里的键用 kw 直接给。"""
    out = {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}}]}
    delta = out["choices"][0]["delta"]
    for k in ("content", "reasoning_content", "role"):
        if k in kw:
            delta[k] = kw[k]
    if "tool_calls" in kw:
        delta["tool_calls"] = kw["tool_calls"]
    if "finish_reason" in kw:
        out["choices"][0]["finish_reason"] = kw["finish_reason"]
    if "usage" in kw:
        out["usage"] = kw["usage"]
    for k in ("id", "created", "model"):
        if k in kw:
            out[k] = kw[k]
    return out


# ── 帧聚合 ────────────────────────────────────────────────────────────────────
def test_aggregate_usage_key_always_present():
    """分叉点①：旧 codearts 没有 usage 就整个键不写，旧 qoder 补零。现在恒有。"""
    frames = [_chunk(content="a"), _chunk(finish_reason="stop")]
    a = C.aggregate_sse(frames, "m")
    b = C.aggregate_sse_or_none(frames, "m")
    ok = (a.get("usage") == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
          and b.get("usage") == a["usage"] and a["choices"][0]["message"]["content"] == "a")
    _record("aggregate_usage_key_always_present", ok, json.dumps(a.get("usage")))


def test_aggregate_merges_fragmented_tool_name():
    """分叉点②：旧 codearts 用 += 拼接、旧 qoder 用 merge_tool_name。统一后者。"""
    frames = [
        _chunk(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "get_", "arguments": "{\"a\""}}]),
        _chunk(tool_calls=[{"index": 0, "function": {"name": "time", "arguments": ":1}"}}]),
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": ""}}]),
        _chunk(finish_reason="tool_calls"),
    ]
    m = C.aggregate_sse(frames, "m")["choices"][0]["message"]
    tc = m["tool_calls"][0]
    ok = (tc["function"]["name"] == "get_time"
          and json.loads(tc["function"]["arguments"]) == {"a": 1} and tc["id"] == "c1")
    _record("aggregate_merges_fragmented_tool_name", ok, json.dumps(tc, ensure_ascii=False))


def test_aggregate_two_none_entrypoints_differ_only_on_junk():
    """两个公开入口的唯一区别：拼不出东西时给空响应，还是给 None（让调用方透传原始字节）。"""
    junk = ["not json", '{"choices":[]}', 42, None]
    a = C.aggregate_sse(junk, "m")
    b = C.aggregate_sse_or_none(junk, "m")
    ok = (isinstance(a, dict) and a["choices"][0]["message"]["content"] is None
          and a["choices"][0]["finish_reason"] == "stop" and b is None)
    _record("aggregate_two_none_entrypoints_differ_only_on_junk", ok, "%s / %s" % (type(a).__name__, b))


def test_aggregate_error_frame_both_flavours():
    """分叉点③：错误帧检测旧版一个在聚合器内、一个在外。现在一处实现，认两种格式。"""
    openai_err = [_chunk(content="x"), {"error": {"message": "限流", "type": "rate_limit"}}]
    huawei_err = [_chunk(content="x"), {"error_code": "TM.00001041", "error_msg": "并发会话数已达上限"}]
    a = C.aggregate_sse(openai_err, "m")
    b = C.aggregate_sse_or_none(huawei_err, "m")
    ok = (a.get("error") == openai_err[1] and b.get("error") == huawei_err[1]
          and C.error_of(huawei_err[1])["code"] == "TM.00001041"
          and "上限" in C.error_of(huawei_err[1])["message"])
    _record("aggregate_error_frame_both_flavours", ok, C.error_of(huawei_err[1]).get("code"))


def test_aggregate_passes_through_whole_completion_frame():
    """伪流式上游一帧就把 chat.completion 吐完 → 原样返回，不重排字段。"""
    whole = {"id": "chatcmpl-1", "object": "chat.completion", "created": 1,
             "model": "glm", "choices": [{"index": 0, "message": {"role": "assistant",
                                                                  "content": "hi"},
                                          "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    ok = C.aggregate_sse([json.dumps(whole)], "m") == whole
    _record("aggregate_passes_through_whole_completion_frame", ok)


def test_aggregate_keeps_role_and_order_by_index():
    """工具按上游 index 归位，乱序到达也要按 index 排回去。"""
    frames = [_chunk(tool_calls=[{"index": 1, "id": "b", "function": {"name": "second", "arguments": "{}"}}]),
              _chunk(tool_calls=[{"index": 0, "id": "a", "function": {"name": "first", "arguments": "{}"}}]),
              _chunk(role="assistant", finish_reason="tool_calls")]
    m = C.aggregate_sse(frames, "m")["choices"][0]["message"]
    names = [t["function"]["name"] for t in m["tool_calls"]]
    ok = names == ["first", "second"] and m["role"] == "assistant"
    _record("aggregate_keeps_role_and_order_by_index", ok, names)


# ── 入站转换 ──────────────────────────────────────────────────────────────────
def test_responses_parallel_function_calls_append():
    """分叉点④：一条 assistant 消息里的多个 function_call，旧 qoder 只会留最后一个。"""
    body = {"input": [
        {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": "{}"},
        {"type": "function_call", "call_id": "c2", "name": "get_time", "arguments": "{}"},
    ]}
    msgs = C.responses_to_openai(body)["messages"]
    ok = (len(msgs) == 1 and len(msgs[0]["tool_calls"]) == 2
          and [t["function"]["name"] for t in msgs[0]["tool_calls"]] == ["get_weather", "get_time"])
    _record("responses_parallel_function_calls_append", ok, json.dumps(msgs)[:120])


def test_responses_reasoning_emits_both_shapes():
    """qoder 读 oa['reasoning']，codearts 读 oa['reasoning_effort']：core 两个都给。"""
    oa = C.responses_to_openai({"input": "hi", "reasoning": {"effort": "high", "summary": "auto"}})
    ok = oa["reasoning"] == {"effort": "high", "summary": "auto"} and oa["reasoning_effort"] == "high"
    ok = ok and oa["stream"] is False and "max_output_tokens" not in oa.get("messages")[0]
    _record("responses_reasoning_emits_both_shapes", ok, oa.get("reasoning_effort"))


def test_responses_function_call_output_shapes():
    """function_call_output 三种形态：字符串照用、[{type:text}] 拼接、其它序列化。"""
    body = {"input": [
        {"type": "function_call_output", "call_id": "c1", "output": "纯文本"},
        {"type": "function_call_output", "call_id": "c2", "output": [{"type": "text", "text": "A"},
                                                                    {"type": "text", "text": "B"}]},
        {"type": "function_call_output", "call_id": "c3", "output": {"code": 0}},
    ]}
    msgs = C.responses_to_openai(body)["messages"]
    ok = ([m["content"] for m in msgs] == ["纯文本", "AB", '{"code": 0}']
          and [m["tool_call_id"] for m in msgs] == ["c1", "c2", "c3"])
    _record("responses_function_call_output_shapes", ok, [m["content"] for m in msgs])


def test_anthropic_image_policy_is_the_only_backend_difference():
    """分叉点⑤：qoder 上游只吃字符串 content（给数组会 400），华为 snap-access 支持多模态。

    这是真实的上游能力差异，所以保留成一个显式参数，而不是"取更好的一边"。
    """
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "看这张图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]}]}
    flat = C.anthropic_to_openai(body)["messages"]
    keep = C.anthropic_to_openai(body, image_policy="preserve")["messages"]
    ok = (len(flat) == 2 and flat[1]["content"] == C.IMAGE_PLACEHOLDER
          and isinstance(keep[0]["content"], list)
          and keep[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,AA==")
    url_body = {"messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]}
    url = C.anthropic_to_openai(url_body, image_policy="preserve")["messages"]
    ok = ok and url[0]["content"][0]["image_url"]["url"] == "https://x/y.png"
    _record("anthropic_image_policy_is_the_only_backend_difference", ok, flat[1]["content"])


def test_anthropic_thinking_drops_temperature_and_gives_both_shapes():
    """分叉点⑥：Anthropic 硬约束（thinking 时 temperature 必须 1）旧版只有 codearts 处理。"""
    body = {"max_tokens": 100, "temperature": 0.3, "thinking": {"type": "enabled", "budget_tokens": 5}
            }
    oa = C.anthropic_to_openai(body)
    ok = ("temperature" not in oa and oa["reasoning_effort"] == "high"
          and oa["thinking"] == body["thinking"])
    disabled = C.anthropic_to_openai({"temperature": 0.7, "thinking": {"type": "disabled"}})
    ok = ok and disabled.get("temperature") == 0.7 and "reasoning_effort" not in disabled
    _record("anthropic_thinking_drops_temperature_and_gives_both_shapes", ok, sorted(oa))


def test_anthropic_tool_result_carries_name_and_joins_parts():
    body = {"messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "name": "get_time",
         "content": [{"type": "text", "text": "12:00"}, {"type": "image", "source": {}}]}]}]}
    m = C.anthropic_to_openai(body)["messages"][0]
    ok = m["role"] == "tool" and m["name"] == "get_time" and m["content"] == "12:00"
    _record("anthropic_tool_result_carries_name_and_joins_parts", ok, m)


def test_tools_and_tool_choice_three_shapes_each():
    """tools/tool_choice 各家形状：OpenAI 嵌套、Responses 扁平、Anthropic input_schema/tool。"""
    tools = C.tools_to_openai([{"type": "function", "function": {"name": "n1"}},
                               {"name": "n2", "input_schema": {"type": "object"}},
                               {"name": "n3", "parameters": {"type": "object"}}])
    choices = [C.tool_choice_to_openai("any", "anthropic"),
               C.tool_choice_to_openai({"type": "tool", "name": "n1"}, "anthropic"),
               C.tool_choice_to_openai({"type": "function", "name": "n2"}, "responses"),
               C.tool_choice_to_openai("required", "responses")]
    ok = ([t["function"]["name"] for t in tools] == ["n1", "n2", "n3"]
          and tools[1]["function"]["parameters"] == {"type": "object"}
          and choices == ["required", {"type": "function", "function": {"name": "n1"}},
                          {"type": "function", "function": {"name": "n2"}}, "required"])
    _record("tools_and_tool_choice_three_shapes_each", ok, choices)


# ── 出站与流式 ────────────────────────────────────────────────────────────────
def _oa_obj(content="hi", reasoning=None, tools=None, finish="stop", usage=None):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    if tools:
        msg["tool_calls"] = tools
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7,
                               "completion_tokens_details": {"reasoning_tokens": 2}}}


def test_openai_to_responses_adds_output_text_and_reasoning_tokens():
    """分叉点⑦：output_text 和 reasoning_tokens 旧版只有 qoder 给（codex-cli 会读 output_text）。"""
    r = C.openai_to_responses(_oa_obj(reasoning="think"), "req-model")
    ok = (r["output_text"] == "hi" and r["model"] == "req-model"
          and r["usage"]["output_tokens_details"]["reasoning_tokens"] == 2
          and [i["type"] for i in r["output"]] == ["reasoning", "message"])
    cut = C.openai_to_responses(_oa_obj(finish="length"), "m")
    ok = ok and cut["status"] == "incomplete" and cut["incomplete_details"]["reason"] == "max_output_tokens"
    _record("openai_to_responses_adds_output_text_and_reasoning_tokens", ok, r["output_text"])


def test_openai_to_anthropic_never_returns_empty_content():
    """分叉点⑧：空正文时旧 codearts 给 content=[]，Anthropic 客户端拿不到任何块。"""
    empty = C.openai_to_anthropic({"choices": [{"index": 0, "message": {"role": "assistant",
                                                                       "content": None}}]}, "m")
    bad = C.openai_to_anthropic(_oa_obj(content="", finish="tool_calls",
                                        tools=[{"id": "", "function": {"name": "f",
                                                                       "arguments": "坏JSON"}}]), "m")
    ok = (empty["content"] == [{"type": "text", "text": ""}] and empty["stop_reason"] == "end_turn"
          and bad["content"][0]["input"] == {} and bad["content"][0]["id"].startswith("toolu_")
          and bad["stop_reason"] == "tool_use")
    filt = C.openai_to_anthropic(_oa_obj(finish="content_filter"), "m")
    ok = ok and filt["stop_reason"] == "content_filter"
    _record("openai_to_anthropic_never_returns_empty_content", ok, empty["content"])


def test_responses_stream_has_both_head_and_part_done():
    """分叉点⑨：response.in_progress 只有 qoder 发，reasoning_summary_part.done 只有 codearts 发。"""
    st = C.ResponsesStreamState("m")
    evs = []
    evs += [e[0] for e in st.feed(_chunk(reasoning_content="r"))]
    evs += [e[0] for e in st.feed(_chunk(content="hi", finish_reason="stop"))]
    evs += [e[0] for e in st.finish()]
    ok = (evs[0] == "response.created" and evs[1] == "response.in_progress"
          and "response.reasoning_summary_part.done" in evs and evs[-1] == "response.completed")
    _record("responses_stream_has_both_head_and_part_done", ok, evs[:3] + evs[-3:])


def test_anthropic_stream_two_parallel_tool_calls_do_not_crash():
    """真 bug：旧实现 finish() 里 sorted(dict_values()) 比大小，两个并发 tool_use 必抛 TypeError。

    在改之前的 qoder_proxy 上这段就是炸的（TypeError: '<' not supported between instances
    of 'dict' and 'dict'），客户端表现为流式回复到一半断掉。
    """
    st = C.AnthropicStreamState("m")
    st.feed(_chunk(tool_calls=[{"index": 1, "id": "b", "function": {"name": "second"}}]))
    st.feed(_chunk(tool_calls=[{"index": 1, "function": {"arguments": "{\"x\":2}"}}]))
    st.feed(_chunk(tool_calls=[{"index": 0, "id": "a", "function": {"name": "first"}}]))
    st.feed(_chunk(tool_calls=[{"index": 0, "function": {"arguments": "{\"x\":1}"}}]))
    evs = st.finish()
    starts = [json.loads(d) for e, d in evs if e == "content_block_start"]
    tools = [s["content_block"] for s in starts if s["content_block"]["type"] == "tool_use"]
    ok = ([t["name"] for t in tools] == ["first", "second"]
          and [t["id"] for t in tools] == ["a", "b"]
          and json.dumps([e[0] for e in evs]).count("content_block_stop") == 2)
    _record("anthropic_stream_two_parallel_tool_calls_do_not_crash", ok, [t["name"] for t in tools])


def test_anthropic_stream_buffers_name_before_block_start():
    """工具块必须带完整 name 才发 content_block_start（旧 codearts 就地发，name 常是空串）。"""
    st = C.AnthropicStreamState("m")
    frames = [
        _chunk(tool_calls=[{"index": 0, "id": "c", "function": {"arguments": "{\"a\""}}]),
        _chunk(tool_calls=[{"index": 0, "function": {"name": "get_", "arguments": ":1}"}}]),
        _chunk(tool_calls=[{"index": 0, "function": {"name": "time"}}]),
    ]
    mid = []
    for f in frames:
        mid += [e[0] for e in st.feed(f)]
    fin = st.finish()
    starts = [json.loads(d) for e, d in fin if e == "content_block_start"]
    deltas = [json.loads(d) for e, d in fin if e == "content_block_delta"]
    blk = starts[0]["content_block"]
    args = "".join(x["delta"]["partial_json"] for x in deltas)
# ── 防分叉守卫 ───────────────────────────────────────────────────────────────
CORE_NAMES = {
    "convert_openai_to_responses": "openai_to_responses",
    "convert_openai_to_anthropic": "openai_to_anthropic",
    "parse_anthropic_text": "anthropic_text",
    "_ResponsesStreamState": "ResponsesStreamState",
    "_AnthropicStreamState": "AnthropicStreamState",
}


def test_both_proxies_bind_to_the_same_core_objects():
    """别名必须真的指向 core 的同一对象——复制粘贴回来就是分叉的开始。"""
    bad = []
    for mod in (Q, A):
        for local, remote in CORE_NAMES.items():
            if getattr(mod, local) is not getattr(C, remote):
                bad.append("%s.%s != C.%s" % (mod.__name__, local, remote))
        if getattr(mod, "_rand") is not C.new_id:
            bad.append("%s._rand 不是 C.new_id" % mod.__name__)
    if Q.aggregate_sse is not C.aggregate_sse:
        bad.append("Q.aggregate_sse 应取恒返回 dict 的入口")
    if A.aggregate_sse is not C.aggregate_sse_or_none:
        bad.append("A.aggregate_sse 应取可返回 None 的入口（原始字节直通）")
    _record("both_proxies_bind_to_the_same_core_objects", not bad, bad)


def test_anthropic_wrapper_only_differs_by_image_policy():
    """两个后端唯一的入站差异是图片策略，而且必须是"参数不同"而不是"代码不同"。"""
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "图"}, {"type": "image", "source": {"type": "url", "url": "u"}}]}]}
    same = (A.convert_anthropic_to_openai(body)
            == C.anthropic_to_openai(body, image_policy="preserve"))
    differs = (Q.convert_anthropic_to_openai(body)
               == C.anthropic_to_openai(body, image_policy="placeholder"))
    _record("anthropic_wrapper_only_differs_by_image_policy", same and differs,
            "A=%s Q=%s" % (A.convert_anthropic_to_openai(body)["messages"][0]["content"],
                           Q.convert_anthropic_to_openai(body)["messages"][0]["content"]))


def test_codearts_wrappers_drop_only_core_added_keys():
    """后端只在 wrapper 边界删自己上游不认的键——不能扩到出站 payload 层，
    否则直连 OpenAI 协议的客户端自带 reasoning 也会被吃掉（真出过这个回归）。"""
    anth = {"thinking": {"type": "enabled", "budget_tokens": 2000}, "temperature": 0.3,
            "messages": [{"role": "user", "content": "hi"}]}
    resp = {"model": "m", "input": "hi", "reasoning": {"effort": "low", "summary": "auto"}}
    a1, q1 = A.convert_anthropic_to_openai(anth), Q.convert_anthropic_to_openai(anth)
    a2, q2 = A.convert_responses_to_openai(resp), Q.convert_responses_to_openai(resp)
    bad = []
    if "thinking" in a1:
        bad.append("A 没丢掉 thinking 形态，snap-access 白名单会拒")
    if a1.get("reasoning_effort") != "high" or q1.get("reasoning_effort") != "high":
        bad.append("两家都该从 thinking=enabled 推出 reasoning_effort=high")
    if "temperature" in a1 or "temperature" in q1:
        bad.append("thinking 开启时 temperature 必须丢，这是 Anthropic 硬约束")
    if q1.get("thinking") != anth["thinking"]:
        bad.append("Q 该保留 thinking 原文（qoder 上游认这个）")
    if "reasoning" in a2:
        bad.append("A 没丢掉 responses 的 reasoning 对象")
    if a2.get("reasoning_effort") != "low" or q2.get("reasoning_effort") != "low":
        bad.append("reasoning_effort 两家都要留下")
    if q2.get("reasoning") != resp["reasoning"]:
        bad.append("Q 该保留 reasoning 原文")
    if not (Q.convert_anthropic_to_openai is C.anthropic_to_openai
            and Q.convert_responses_to_openai is C.responses_to_openai):
        bad.append("Q 侧应是 core 的直接别名，不再加本地加工")
    if A.convert_anthropic_to_openai is C.anthropic_to_openai:
        bad.append("A 侧必须是 wrapper，不是别名")

    import ast
    tree = ast.parse(io.open("codearts_proxy.py", encoding="utf-8").read())
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    # for _k in ("thinking", "reasoning"): req.pop(_k, None) 这种写法，键是变量不是字面量，
    # 只按字面量匹配会漏掉——先把这类循环变量的取值集合还原出来。
    loop_keys = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            vals = {e.value for e in node.iter.elts if isinstance(e, ast.Constant)}
            if vals & {"thinking", "reasoning"}:
                loop_keys[node.target.id] = vals
    allowed = {"convert_anthropic_to_openai", "convert_responses_to_openai"}

    def pop_key(call):
        if not call.args:
            return None
        a = call.args[0]
        if isinstance(a, ast.Constant):
            return a.value
        if isinstance(a, ast.Name):
            return next((v for v in loop_keys.get(a.id, ()) if v in ("thinking", "reasoning")), None)
        return None

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "pop" and pop_key(node) in ("thinking", "reasoning")):
            up, owner = node, None
            while up is not None:
                if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = up.name
                    break
                up = parent.get(up)
            if owner not in allowed:
                bad.append("codearts_proxy.py:%s 在 %s 里 pop 了协议键" % (node.lineno, owner or "模块顶层"))
    _record("codearts_wrappers_drop_only_core_added_keys", not bad, bad)


def test_proxies_no_longer_define_protocol_layer_locally():
    """两个代理源码里不许再出现协议层的 def——这条是防"顺手在本地改一份"的硬线。"""
    import ast
    banned = set(CORE_NAMES) | {"aggregate_sse", "_merge_tool_name", "_error_of"}
    bad = []
    for path, mod in (("qoder_proxy.py", Q), ("codearts_proxy.py", A)):
        tree = ast.parse(io.open(path, encoding="utf-8").read())
        local = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        for name in sorted(banned & local):
            bad.append("%s 又自己定义了 %s" % (path, name))
    _record("proxies_no_longer_define_protocol_layer_locally", not bad, bad)


if __name__ == "__main__":
    for name in sorted(globals()):
        fn = globals()[name]
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                _record(name, False, "异常: %r" % (e,))
    fails = [n for n, ok, d in _results if not ok]
    print("\n==== 总结: %d/%d 通过 ====" % (len(_results) - len(fails), len(_results)))
    if fails:
        print("失败项:")
        for n, ok, d in _results:
            if not ok:
                print("  - %s: %s" % (n, d))
        sys.exit(1)
    print("OK")



