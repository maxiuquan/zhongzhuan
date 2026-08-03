"""T17 acceptance tests for the Responses turn bridge (门面 + 组合).

Covers the v3-specific acceptance criteria that are not exercised by the
legacy ``test_responses.py``:

* ③ 流结束后 reasoning buffer 字段清空（R-P1-04 内存增长测试）；
* ④ ``reasoning_event_mode`` 三档各断言下游事件类型（Q1）；
* ⑤ mock 上游立即断开，事件序列首个为 ``response.created``。
"""
import json
import re

import pytest

from zhongzhuan.proxy.protocol.responses_bridge import ResponsesTurnBridge
from zhongzhuan.proxy.protocol.responses_models import ReasoningEventMode


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _event_names(text: str) -> list[str]:
    return [ln.split("event:", 1)[1].strip() for ln in text.splitlines() if ln.startswith("event:")]


async def _run(chunks: list[bytes], **kw):
    tr = ResponsesTurnBridge(**kw)
    out: list[bytes] = []
    for c in chunks:
        out.extend(await tr.feed(c))
    out.extend(await tr.afinish())
    return tr, b"".join(out).decode()


class TestReasoningBufferReleased:
    async def test_reasoning_buffer_cleared_after_finish(self):
        """③ R-P1-04: reasoning text is released at turn end."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {
                "reasoning_content": "think step 1"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {
                "reasoning_content": " think step 2"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "answer"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        tr, _ = await _run(chunks)
        # After finish, the ephemeral reasoning accumulator is released (R-P1-04).
        assert tr._acc.reasoning is None
        # Reasoning text no longer retained anywhere on the turn.
        assert tr._acc.reasoning is None or tr._acc.reasoning.text == ""

    async def test_no_reasoning_leak_into_next_turn(self):
        """A fresh bridge must not retain the previous turn's reasoning."""
        tr = ResponsesTurnBridge(model="m")
        await tr.feed(_sse({"id": "c1", "choices": [{"index": 0, "delta": {
            "reasoning_content": "secret"}}]}))
        await tr.afinish()
        # New bridge -> no reasoning state at all.
        tr2 = ResponsesTurnBridge(model="m")
        assert tr2._acc.reasoning is None


class TestReasoningEventMode:
    async def test_summary_text_mode(self):
        """④ default SUMMARY_TEXT -> reasoning_summary_text.* family."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {
                "reasoning_content": "t"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run(chunks)
        names = _event_names(text)
        assert "response.reasoning_summary_text.delta" in names
        assert "response.reasoning_summary_text.done" in names
        assert "response.reasoning_summary_part.added" in names
        assert "response.reasoning_summary_part.done" in names
        assert "response.reasoning_text.delta" not in names
        assert "response.reasoning_text_part.added" not in names

    async def test_text_mode(self):
        """④ reasoning_text mode -> reasoning_text.* family."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {
                "reasoning_content": "t"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run(
            chunks, model="m", reasoning_event_mode=ReasoningEventMode.TEXT.value
        )
        names = _event_names(text)
        assert "response.reasoning_text.delta" in names
        assert "response.reasoning_text.done" in names
        assert "response.reasoning_text_part.added" in names
        assert "response.reasoning_text_part.done" in names
        assert "response.reasoning_summary_text.delta" not in names

    async def test_disabled_mode(self):
        """④ disabled -> no reasoning events at all."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {
                "reasoning_content": "t"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"},
                                           "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run(
            chunks, model="m", reasoning_event_mode=ReasoningEventMode.DISABLED.value
        )
        names = _event_names(text)
        assert not any("reasoning" in n for n in names)
        # Message still flows.
        assert "response.output_text.delta" in names


class TestUpstreamDisconnect:
    async def test_first_event_is_response_created(self):
        """⑤ upstream dies immediately; first event is response.created."""
        tr = ResponsesTurnBridge(model="m")
        # No chunks at all -> upstream 0 bytes, immediate disconnect.
        out = tr.finish_safely()
        text = b"".join(out).decode()
        names = _event_names(text)
        assert names[0] == "response.created"
        assert "response.in_progress" in names
        assert "response.completed" in names
        assert text.rstrip().endswith("data: [DONE]")

    async def test_immediate_disconnect_with_emitter(self):
        """⑤ bridge with a partial chunk then disconnect still starts cleanly."""
        tr = ResponsesTurnBridge(model="m")
        feed_out = await tr.feed(_sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "x"}}]}))
        out = await tr.afinish()
        text = b"".join(feed_out + out).decode()
        names = _event_names(text)
        assert names[0] == "response.created"
        assert "response.completed" in names
        assert "data: [DONE]" in text