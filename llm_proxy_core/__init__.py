"""llm_proxy_core — 两个本地代理（codearts_proxy.py / qoder_proxy.py）共用的协议层。

按"与后端无关"的方式拆分：
    ids.py       随机对外 id
    frames.py    SSE 帧聚合 / 错误帧识别 / 分片工具名拼接
    convert.py   Responses / Anthropic → OpenAI 请求
    outbound.py  OpenAI → Responses / Anthropic 响应
    streams.py   Responses / Anthropic 流式状态机

两个代理按老函数名从这里做别名绑定（`aggregate_sse`、`convert_*`、`_*StreamState`），
禁止在代理文件里再 `def` 一份——`test_core_protocols.py` 有守卫盯着这件事。
"""
from .convert import (
    IMAGE_PLACEHOLDER,
    TEXT_PART_TYPES,
    anthropic_text,
    anthropic_to_openai,
    responses_items_to_messages,
    responses_to_openai,
    tool_choice_to_openai,
    tools_to_openai,
)
from .frames import (
    aggregate_sse,
    aggregate_sse_or_none,
    error_of,
    merge_tool_name,
)
from .ids import new_id
from .outbound import (
    ANTHROPIC_STOP,
    finish_to_anthropic,
    openai_to_anthropic,
    openai_to_responses,
)
from .streams import (
    AnthropicStreamState,
    ResponsesStreamState,
)

__all__ = [
    "IMAGE_PLACEHOLDER",
    "TEXT_PART_TYPES",
    "ANTHROPIC_STOP",
    "new_id",
    "merge_tool_name",
    "error_of",
    "aggregate_sse",
    "aggregate_sse_or_none",
    "tools_to_openai",
    "tool_choice_to_openai",
    "responses_items_to_messages",
    "responses_to_openai",
    "anthropic_to_openai",
    "anthropic_text",
    "finish_to_anthropic",
    "openai_to_responses",
    "openai_to_anthropic",
    "ResponsesStreamState",
    "AnthropicStreamState",
]