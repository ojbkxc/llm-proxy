"""流式状态机：内部 OpenAI chat chunk → Responses / Anthropic SSE 事件序列。

两个代理以前各写一份，事件序列已经不一致（qoder 有 response.in_progress、缺
reasoning_summary_part.done；codearts 反之），同一客户端打两个后端会收到不同的事件流。
这里是两者的并集。
"""
import json
import time

from .ids import new_id
from .frames import merge_tool_name


class ResponsesStreamState:
    """chat chunk → Responses 协议事件序列（codex-cli 依赖 response.completed 等）。"""

    def __init__(self, model):
        self.model = model
        self.resp_id = "resp_" + new_id()
        self.created = int(time.time())
        self.started = False
        self.finished = False
        self.finish_reason = None
        self.text = ""
        self.reasoning = ""
        self.tool_name = ""
        self.tool_id = ""
        self.tool_args = ""
        self.usage = None
        self.text_item_id = "msg_" + new_id()
        self.reason_item_id = "rs_" + new_id()
        self.reason_opened = False
        self.text_opened = False
        self.reason_index = None
        self.text_index = None
        self.next_index = 0

    def _head_events(self):
        """response.created + response.in_progress（OpenAI 官方序列的开头两帧）。"""
        resp = {"id": self.resp_id, "object": "response", "created_at": self.created,
                "status": "in_progress", "model": self.model, "output": []}
        return [("response.created", json.dumps({"type": "response.created", "response": resp},
                                                 ensure_ascii=False)),
                ("response.in_progress", json.dumps({"type": "response.in_progress",
                                                     "response": resp}, ensure_ascii=False))]

    def feed(self, chunk):
        out = []
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}

        if not self.started:
            self.started = True
            out.extend(self._head_events())

        rc = delta.get("reasoning_content")
        if rc:
            self.reasoning += rc
            if not self.reason_opened:
                self.reason_opened = True
                self.reason_index = self.next_index
                self.next_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": self.reason_index,
                    "item": {"type": "reasoning", "id": self.reason_item_id, "summary": []},
                }, ensure_ascii=False)))
                out.append(("response.reasoning_summary_part.added", json.dumps({
                    "type": "response.reasoning_summary_part.added", "item_id": self.reason_item_id,
                    "output_index": self.reason_index, "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                }, ensure_ascii=False)))
            out.append(("response.reasoning_summary_text.delta", json.dumps({
                "type": "response.reasoning_summary_text.delta", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "delta": rc,
            }, ensure_ascii=False)))

        content = delta.get("content")
        if content:
            self.text += content
            if not self.text_opened:
                self.text_opened = True
                self.text_index = self.next_index
                self.next_index += 1
                out.append(("response.output_item.added", json.dumps({
                    "type": "response.output_item.added", "output_index": self.text_index,
                    "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                             "status": "in_progress", "content": []},
                }, ensure_ascii=False)))
                out.append(("response.content_part.added", json.dumps({
                    "type": "response.content_part.added", "item_id": self.text_item_id,
                    "output_index": self.text_index, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }, ensure_ascii=False)))
            out.append(("response.output_text.delta", json.dumps({
                "type": "response.output_text.delta", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "delta": content,
            }, ensure_ascii=False)))

        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if tc.get("id"):
                self.tool_id = tc["id"]
            self.tool_name = merge_tool_name(self.tool_name, fn.get("name"))
            if fn.get("arguments"):
                self.tool_args += fn["arguments"]
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        if not self.started:
            out.extend(self._head_events())
        if self.reason_opened:
            out.append(("response.reasoning_summary_text.done", json.dumps({
                "type": "response.reasoning_summary_text.done", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0, "text": self.reasoning,
            }, ensure_ascii=False)))
            out.append(("response.reasoning_summary_part.done", json.dumps({
                "type": "response.reasoning_summary_part.done", "item_id": self.reason_item_id,
                "output_index": self.reason_index, "summary_index": 0,
                "part": {"type": "summary_text", "text": self.reasoning},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": self.reason_index,
                "item": {"type": "reasoning", "id": self.reason_item_id,
                         "summary": [{"type": "summary_text", "text": self.reasoning}]},
            }, ensure_ascii=False)))
        if self.text_opened:
            out.append(("response.output_text.done", json.dumps({
                "type": "response.output_text.done", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0, "text": self.text,
            }, ensure_ascii=False)))
            out.append(("response.content_part.done", json.dumps({
                "type": "response.content_part.done", "item_id": self.text_item_id,
                "output_index": self.text_index, "content_index": 0,
                "part": {"type": "output_text", "text": self.text, "annotations": []},
            }, ensure_ascii=False)))
            out.append(("response.output_item.done", json.dumps({
                "type": "response.output_item.done", "output_index": self.text_index,
                "item": {"type": "message", "id": self.text_item_id, "role": "assistant",
                         "status": "completed",
                         "content": [{"type": "output_text", "text": self.text,
                                      "annotations": []}]},
            }, ensure_ascii=False)))
        items = []
        if self.reasoning:
            items.append({"type": "reasoning", "id": self.reason_item_id,
                          "summary": [{"type": "summary_text", "text": self.reasoning}]})
        if self.text:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": self.text,
                                       "annotations": []}]})
        if self.tool_name:
            items.append({"type": "function_call", "id": "fc_" + new_id(),
                          "call_id": self.tool_id, "name": self.tool_name,
                          "arguments": self.tool_args, "status": "completed"})
        if not items:
            items.append({"type": "message", "id": self.text_item_id, "role": "assistant",
                          "status": "completed",
                          "content": [{"type": "output_text", "text": "", "annotations": []}]})
        usage = self.usage or {}
        out.append(("response.completed", json.dumps({
            "type": "response.completed",
            "response": {"id": self.resp_id, "object": "response", "created_at": self.created,
                         "status": "completed" if self.finish_reason != "length" else "incomplete",
                         "model": self.model, "output": items,
                         "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                                   "output_tokens": usage.get("completion_tokens", 0),
                                   "total_tokens": usage.get("total_tokens", 0)},
                         "error": None,
                         "incomplete_details": {"reason": "max_output_tokens"}
                         if self.finish_reason == "length" else None},
        }, ensure_ascii=False)))
        return out


class AnthropicStreamState:
    """chat chunk → Anthropic Messages SSE 事件序列。

    tool_use 的 name/arguments 在上游是分片到达的（如 "get_" + "time"），
    content_block_start 又必须带完整 name，所以工具块先缓冲、finish() 时整块吐出。
    代价是工具参数没有增量流式，换来的是 name 一定是完整的——
    另一半实现曾就地发 content_block_start，第一帧常常只有 id 没有 name，
    客户端就收到 name="" 的 tool_use。
    """

    def __init__(self, model):
        self.model = model
        self.msg_id = "msg_" + new_id()
        self.started = False
        self.finished = False
        self.thinking_block = False
        self.text_block = False
        self.block_index = 0
        self.text = ""
        self.thinking = ""
        self.tool_blocks = {}      # 上游 index → {index, id, name, args}
        self.usage = None

    def _start(self, out):
        if self.started:
            return
        self.started = True
        out.append(("message_start", json.dumps({
            "type": "message_start",
            "message": {"id": self.msg_id, "type": "message", "role": "assistant",
                        "content": [], "model": self.model, "stop_reason": None,
                        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}},
        }, ensure_ascii=False)))

    def _stop_block(self, out):
        if self.thinking_block or self.text_block:
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": self.block_index - 1},
                ensure_ascii=False)))
            self.thinking_block = False
            self.text_block = False

    def feed(self, chunk):
        out = []
        self._start(out)
        choice = (chunk.get("choices") or [{}])[0]
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        delta = choice.get("delta") or {}
        rc = delta.get("reasoning_content")
        if rc:
            if not self.thinking_block:
                self._stop_block(out)
                self.thinking_block = True
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.block_index - 1,
                    "content_block": {"type": "thinking", "thinking": ""},
                }, ensure_ascii=False)))
            self.thinking += rc
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "thinking_delta", "thinking": rc},
            }, ensure_ascii=False)))
        content = delta.get("content")
        if content:
            if not self.text_block:
                self._stop_block(out)
                self.text_block = True
                self.block_index += 1
                out.append(("content_block_start", json.dumps({
                    "type": "content_block_start", "index": self.block_index - 1,
                    "content_block": {"type": "text", "text": ""},
                }, ensure_ascii=False)))
            self.text += content
            out.append(("content_block_delta", json.dumps({
                "type": "content_block_delta", "index": self.block_index - 1,
                "delta": {"type": "text_delta", "text": content},
            }, ensure_ascii=False)))
        for i, tc in enumerate(delta.get("tool_calls") or []):
            fn = tc.get("function") or {}
            key = tc.get("index", i)
            blk = self.tool_blocks.get(key)
            if blk is None:
                blk = {"index": len(self.tool_blocks), "id": tc.get("id") or "",
                       "name": fn.get("name") or "", "args": ""}
                self.tool_blocks[key] = blk
            elif tc.get("id"):
                blk["id"] = tc["id"]
            blk["name"] = merge_tool_name(blk["name"], fn.get("name"))
            if fn.get("arguments"):
                blk["args"] += fn["arguments"]
        return out

    def finish(self):
        if self.finished:
            return []
        self.finished = True
        out = []
        self._start(out)
        self._stop_block(out)
        # 按上游 index 排序；不能 sorted(values())——那是拿 dict 比大小，
        # 两个并发 tool_use 会 TypeError。
        for _, blk in sorted(self.tool_blocks.items(), key=lambda kv: kv[0]):
            index = self.block_index + 1
            self.block_index = index
            out.append(("content_block_start", json.dumps({
                "type": "content_block_start", "index": index - 1,
                "content_block": {"type": "tool_use", "id": blk["id"] or ("toolu_" + new_id()),
                                  "name": blk["name"], "input": {}},
            }, ensure_ascii=False)))
            if blk["args"]:
                out.append(("content_block_delta", json.dumps({
                    "type": "content_block_delta", "index": index - 1,
                    "delta": {"type": "input_json_delta", "partial_json": blk["args"]},
                }, ensure_ascii=False)))
            out.append(("content_block_stop", json.dumps(
                {"type": "content_block_stop", "index": index - 1}, ensure_ascii=False)))
        usage = self.usage or {}
        stop_reason = "tool_use" if self.tool_blocks else "end_turn"
        out.append(("message_delta", json.dumps({
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                      "output_tokens": usage.get("completion_tokens", 0)},
        }, ensure_ascii=False)))
        out.append(("message_stop", json.dumps({"type": "message_stop"}, ensure_ascii=False)))
        return out
