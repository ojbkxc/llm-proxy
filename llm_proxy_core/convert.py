"""外部协议请求 → 内部 OpenAI Chat 请求（两个代理共用）。

`image_policy` 是唯一一处后端差异，而且是真实差异不是历史偶然：
  - `"placeholder"`：图片折成一句占位文本。qoder 上游（agent_chat_generation）走的是
    客户端自己的会话体，实测只吃字符串 content，给它 content 数组会被 400。
  - `"preserve"`：保留成 OpenAI 的 `image_url` 多模态 parts。华为 snap-access 上游支持。
默认取保守的那个（placeholder），要支持图片的后端自己声明。
"""
import json

TEXT_PART_TYPES = ("input_text", "output_text", "text")
IMAGE_PLACEHOLDER = "[图片输入暂不支持]"


def tools_to_openai(tools):
    """Responses / Anthropic / OpenAI 三种 tools 形状 → OpenAI 形状。"""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("function"):
            out.append({"type": "function", "function": t["function"]})
        elif t.get("name"):
            out.append({"type": "function", "function": {
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or t.get("input_schema") or {"type": "object"},
            }})
    return out


def tool_choice_to_openai(choice, protocol):
    if choice is None:
        return None
    if isinstance(choice, str):
        if protocol == "anthropic":
            return {"any": "required", "auto": "auto", "none": "none"}.get(choice, "auto")
        return choice
    if not isinstance(choice, dict):
        return None
    if choice.get("type") == "function" and choice.get("function"):
        return choice
    if choice.get("type") in ("function", "tool") and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    return choice


def _parts_text(parts):
    """Responses 的 content 数组 → 一段文本（图片一律折成占位文本）。"""
    text = ""
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") in TEXT_PART_TYPES:
            text += part.get("text") or ""
        elif part.get("type") in ("input_image", "image", "image_url"):
            text += IMAGE_PLACEHOLDER
    return text


def _tool_result_text(content):
    """Anthropic tool_result.content：字符串照用，[{type:text}] 拼接，其它序列化。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [p.get("text") for p in content
                 if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
        if texts:
            return "".join(texts)
    return json.dumps(content, ensure_ascii=False) if content else ""


def responses_to_openai(body):
    """/v1/responses 请求体 → OpenAI chat.completions 请求体。"""
    oa = {}
    for src, dst in (("model", "model"), ("max_output_tokens", "max_tokens"),
                     ("stream", "stream"), ("temperature", "temperature"),
                     ("top_p", "top_p"), ("top_logprobs", "top_logprobs"),
                     ("stop", "stop"), ("seed", "seed")):
        if body.get(src) is not None:
            oa[dst] = body[src]
    if oa.get("stream") is None:
        oa["stream"] = False

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
                text = c if isinstance(c, str) else _parts_text(c or [])
                if text:
                    msgs.append({"role": role, "content": text})
            elif t == "function_call":
                tc = {"id": item.get("call_id", ""), "type": "function",
                      "function": {"name": item.get("name", ""),
                                   "arguments": item.get("arguments") or "{}"}}
                last = msgs[-1] if msgs else {}
                if last.get("role") == "assistant" and isinstance(last.get("tool_calls"), list):
                    last["tool_calls"].append(tc)
                else:
                    msgs.append({"role": "assistant", "content": None, "tool_calls": [tc]})
            elif t == "function_call_output":
                msgs.append({"role": "tool", "tool_call_id": item.get("call_id", ""),
                             "content": _tool_result_text(item.get("output"))})
    oa["messages"] = msgs or [{"role": "user", "content": ""}]

    tools = tools_to_openai(body.get("tools"))
    if tools:
        oa["tools"] = tools
    tc = tool_choice_to_openai(body.get("tool_choice"), "responses")
    if tc is not None:
        oa["tool_choice"] = tc
    if isinstance(body.get("reasoning"), dict):
        oa["reasoning"] = body["reasoning"]
        if body["reasoning"].get("effort"):
            oa["reasoning_effort"] = body["reasoning"]["effort"]
    return oa


def anthropic_to_openai(body, image_policy="placeholder"):
    """/v1/messages 请求体 → OpenAI chat.completions 请求体。

    thinking 开启时按 Anthropic 的硬约束丢掉 temperature（客户端传了也会被上游拒），
    同时给出 `thinking` 原文和 `reasoning_effort` 两种形态，由各后端取自己能用的。
    """
    oa = {}
    for src, dst in (("model", "model"), ("max_tokens", "max_tokens"), ("stream", "stream"),
                     ("temperature", "temperature"), ("top_p", "top_p"),
                     ("stop_sequences", "stop")):
        if body.get(src) is not None:
            oa[dst] = body[src]

    thinking = body.get("thinking")
    if isinstance(thinking, dict):
        oa["thinking"] = thinking
        if thinking.get("type") == "enabled":
            # Anthropic 硬约束：thinking 开启时 temperature 必须是 1，传别的会被上游拒。
            oa.pop("temperature", None)
            oa["reasoning_effort"] = "high"

    msgs = []
    sys_ = body.get("system")
    if isinstance(sys_, str) and sys_:
        msgs.append({"role": "system", "content": sys_})
    elif isinstance(sys_, list):
        text = "\n".join(p.get("text", "") for p in sys_
                         if isinstance(p, dict) and p.get("type") == "text")
        if text.strip():
            msgs.append({"role": "system", "content": text.strip()})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role, c = msg.get("role"), msg.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        if not isinstance(c, list):
            continue
        if role == "assistant":
            texts, calls = [], []
            for part in c:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    texts.append(part.get("text") or "")
                elif part.get("type") == "tool_use":
                    calls.append({"id": part.get("id", ""), "type": "function",
                                  "function": {"name": part.get("name", ""),
                                               "arguments": json.dumps(part.get("input") or {},
                                                                       ensure_ascii=False)}})
            am = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                am["tool_calls"] = calls
            msgs.append(am)
            continue

        for part in c:
            if isinstance(part, dict) and part.get("type") == "tool_result":
                tm = {"role": "tool", "tool_call_id": part.get("tool_use_id", ""),
                      "content": _tool_result_text(part.get("content"))}
                if part.get("name"):
                    tm["name"] = part["name"]
                msgs.append(tm)
        if image_policy == "preserve":
            parts = []
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text" and p.get("text"):
                    parts.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image":
                    src = p.get("source") or {}
                    if src.get("type") == "url" and src.get("url"):
                        parts.append({"type": "image_url", "image_url": {"url": src["url"]}})
                    elif src.get("data"):
                        parts.append({"type": "image_url", "image_url": {
                            "url": "data:%s;base64,%s" % (src.get("media_type") or "image/png",
                                                          src["data"])}})
            if parts:
                msgs.append({"role": "user", "content": parts})
        else:
            texts = [p.get("text", "") for p in c
                     if isinstance(p, dict) and p.get("type") == "text"]
            if any(t.strip() for t in texts):
                msgs.append({"role": "user", "content": "\n".join(texts)})
            if any(isinstance(p, dict) and p.get("type") == "image" for p in c):
                msgs.append({"role": "user", "content": IMAGE_PLACEHOLDER})

    oa["messages"] = msgs or [{"role": "user", "content": ""}]
    tools = tools_to_openai(body.get("tools"))
    if tools:
        oa["tools"] = tools
    tc = tool_choice_to_openai(body.get("tool_choice"), "anthropic")
    if tc is not None:
        oa["tool_choice"] = tc
    return oa


def anthropic_text(body):
    """Anthropic 请求里的全部文本（count_tokens 本地估算用）。"""
    texts = []
    sys_ = body.get("system")
    if isinstance(sys_, str):
        texts.append(sys_)
    elif isinstance(sys_, list):
        for p in sys_:
            if isinstance(p, dict) and p.get("type") == "text":
                texts.append(p.get("text", ""))
    for msg in body.get("messages") or []:
        c = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            for p in c:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "tool_use":
                    texts.append(json.dumps(p, ensure_ascii=False))
                elif p.get("type") == "tool_result":
                    rc = p.get("content")
                    if isinstance(rc, (str, list)):
                        texts.append(_tool_result_text(rc))
                    else:
                        texts.append(json.dumps(p, ensure_ascii=False))
    return texts

