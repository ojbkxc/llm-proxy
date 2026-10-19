"""上游 SSE 帧 → 内部 chat.completion（两个代理共用）。

这里只放"与后端无关"的部分：把 OpenAI 风格的 chat.completion.chunk 序列拼成一个
完整响应。各家上游自己的怪癖（伪流式整帧下发、错误帧格式）在下面以显式分支处理，
不再各写一份 aggregate_sse —— 历史上两侧同名函数已经漂到 usage 键有无、工具名拼接
方式都不一致，客户端打两个后端会看到两种行为。

两个公开入口的区别只有一件事：拼不出任何东西时，`aggregate_sse` 照样返回一个空的
chat.completion（qoder 的调用方直接 `merged["choices"][0]`，不能给它 None），
`aggregate_sse_or_none` 返回 None（codearts 要靠这个 None 走"原始字节直通"）。
"""
import json
import time

from .ids import new_id


def merge_tool_name(cur, frag):
    """上游可能把工具名分片下发（"get_" + "time"），也可能整名重发；两种都要拼对。"""
    if not frag:
        return cur
    if not cur:
        return frag
    return cur if frag == cur else cur + frag


def error_of(obj):
    """chunk/响应里的错误 → dict；没有错误返回 None。"""
    if not isinstance(obj, dict):
        return None
    if obj.get("error"):
        err = obj["error"]
        if isinstance(err, str):
            return {"message": err}
        return err or {}
    if obj.get("error_code"):
        return {"message": obj.get("error_msg") or obj.get("message") or "",
                "code": obj["error_code"]}
    return None


def _as_obj(frame):
    if isinstance(frame, dict):
        return frame
    if isinstance(frame, (str, bytes)):
        try:
            obj = json.loads(frame)
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _aggregate(frames, model_name):
    """→ chat.completion dict / `{"error": 原始帧}` / None（什么都没攒到）。"""
    objs = [o for o in (_as_obj(f) for f in (frames or [])) if o]
    if not objs:
        return None
    if objs[0].get("object") == "chat.completion":
        return objs[0]

    text, reasoning = [], []
    tools = {}
    usage, finish, role = None, None, "assistant"
    cid, created, model = "chatcmpl-" + new_id(), int(time.time()), model_name
    got = False

    for c in objs:
        if error_of(c):
            return {"error": c}
        if c.get("usage"):
            usage, got = c["usage"], True
        if c.get("id"):
            cid = c["id"]
        if c.get("created"):
            created = c["created"]
        if c.get("model"):
            model = c["model"]
        for ch in c.get("choices") or []:
            got = True
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            d = ch.get("delta") or {}
            if d.get("role"):
                role = d["role"]
            if d.get("content"):
                text.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                slot = tools.setdefault(tc.get("index") or 0,
                                        {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["name"] = merge_tool_name(slot["name"], fn.get("name"))
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]

    if not got:
        return None

    message = {"role": role, "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tools:
        message["tool_calls"] = [{
            "id": s["id"] or ("call_" + new_id()), "type": "function",
            "function": {"name": s["name"], "arguments": s["args"] or "{}"},
        } for _, s in sorted(tools.items())]
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def aggregate_sse(frames, model_name=""):
    """恒返回 chat.completion dict（拼不出东西时给一个空正文的合法响应）。

    调用方可以无条件解引用 `["choices"][0]` / `["usage"]`。撞到错误帧时返回
    `{"error": <原始帧>}`，由调用方翻成 HTTP 错误。
    """
    out = _aggregate(frames, model_name)
    if out is not None:
        return out
    return {
        "id": "chatcmpl-" + new_id(), "object": "chat.completion",
        "created": int(time.time()), "model": model_name,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": None},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def aggregate_sse_or_none(frames, model_name=""):
    """同上，但没有任何可拼的帧时返回 None —— 调用方据此把原始字节原样透传。"""
    return _aggregate(frames, model_name)

