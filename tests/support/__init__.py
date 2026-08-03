"""测试基建包。

包含两块与被测代码完全解耦的支撑设施：

* :mod:`tests.support.sse_assert` —— SSE 字节级分帧、生命周期断言、golden 归一化。
* :mod:`tests.support.mock_responses_upstream` —— 可编程 mock 上游（分片 / 延迟 /
  断流 / 错误注入，覆盖 OpenAI Chat Completions、Anthropic Messages、OpenAI
  Responses 三种上游形态）。

这两个模块**不得** import ``zhongzhuan`` 下的任何东西，以保证它们可以作为
「外部观察者」对被测代码做黑盒断言。
"""
from __future__ import annotations

from .sse_assert import (  # noqa: F401
    PROTOCOL_ANTHROPIC,
    PROTOCOL_OPENAI,
    PROTOCOL_RESPONSES,
    LifecycleReport,
    SseFrame,
    SseLifecycleError,
    assert_lifecycle,
    frame_events,
    iter_data_json,
    normalize_for_golden,
    parse_sse_bytes,
)
from .mock_responses_upstream import (  # noqa: F401
    ChunkStrategy,
    MockUpstream,
    RecordedRequest,
    UpstreamBehavior,
    anthropic_error_json,
    anthropic_text_json,
    anthropic_text_stream,
    anthropic_tool_stream,
    by_line,
    by_n_bytes,
    openai_error_json,
    openai_text_json,
    openai_text_stream,
    openai_tool_json,
    openai_tool_stream,
    random_split,
    responses_text_stream,
    whole,
)

__all__ = [
    # sse_assert
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_OPENAI",
    "PROTOCOL_RESPONSES",
    "LifecycleReport",
    "SseFrame",
    "SseLifecycleError",
    "assert_lifecycle",
    "frame_events",
    "iter_data_json",
    "normalize_for_golden",
    "parse_sse_bytes",
    # mock_responses_upstream
    "ChunkStrategy",
    "MockUpstream",
    "RecordedRequest",
    "UpstreamBehavior",
    "anthropic_error_json",
    "anthropic_text_json",
    "anthropic_text_stream",
    "anthropic_tool_stream",
    "by_line",
    "by_n_bytes",
    "openai_error_json",
    "openai_text_json",
    "openai_text_stream",
    "openai_tool_json",
    "openai_tool_stream",
    "random_split",
    "responses_text_stream",
    "whole",
]
