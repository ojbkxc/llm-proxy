"""内部 OpenAI chat.completion → Responses / Anthropic 响应（两个代理共用）。

签名统一成 `(openai_obj, req_model: str)`：回填给客户端的模型名一律用"客户端请求里的
原始名"，不是上游解析后的真实 id（两家原本都是这个语义，只是 codearts 传的是整个
req_body，得由调用方自己取 model）。
"""
import json
import time

from .ids import new_id

ANTHROPIC_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def finish_to_anthropic(finish):
    return ANTHROPIC_STOP.get(finish, finish or "end_turn")


def openai_to_responses(openai_obj, req_model):
    """OpenAI chat.completion → Responses API 响应。"""
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    output = []
    if message.get("reasoning_content"):
        output.append({"type": "reasoning", "id": "rs_" + new_id(),
                       "summary": [{"type": "summary_text", "text": message["reasoning_content"]}]})
    if message.get("content"):
        output.append({"type": "message", "id": "msg_" + new_id(), "role": "assistant",
                       "status": "completed",
                       "content": [{"type": "output_text", "text": message["content"],
                                    "annotations": []}]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({"type": "function_call", "id": "fc_" + new_id(),
                       "call_id": tc.get("id", ""), "name": fn.get("name", ""),
                       "arguments": fn.get("arguments") or "{}", "status": "completed"})
    incomplete = choice.get("finish_reason") == "length"
    text = "".join(seg.get("text", "") for item in output if item.get("type") == "message"
                   for seg in (item.get("content") or []) if seg.get("type") == "output_text")
    return {
        "id": "resp_" + new_id(), "object": "response", "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "model": req_model, "output": output, "output_text": text,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0),
                  "output_tokens_details": {"reasoning_tokens": (
                      (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))}},
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None,
    }


def openai_to_anthropic(openai_obj, req_model):
    """OpenAI chat.completion → Anthropic Messages 响应。"""
    choice = (openai_obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    usage = openai_obj.get("usage") or {}
    content = []
    if message.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": message["reasoning_content"]})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except Exception:
            inp = {}
        content.append({"type": "tool_use", "id": tc.get("id") or ("toolu_" + new_id()),
                        "name": fn.get("name", ""), "input": inp})
    return {
        "id": "msg_" + new_id(), "type": "message", "role": "assistant",
        "model": req_model, "content": content or [{"type": "text", "text": ""}],
        "stop_reason": finish_to_anthropic(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }
