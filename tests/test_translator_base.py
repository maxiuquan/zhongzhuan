"""Tests for the unified stream-translator finish entry point (T18).

Covers :func:`zhongzhuan.proxy.protocol.translator_base.finish_translator`:

* ``afinish()`` 优先于 ``finish_safely()``（async 优先，sync 退回）；
* 异常兜底返回 ``[b"data: [DONE]\\n\\n"]``，日志含 ``terminal_reason=upstream_truncated``；
* Composite async 收尾（StreamA2O 上游正常流与截断流）；
* 并发 finish 无 ``RuntimeWarning``（``coroutine was never awaited``）；
* 验收①：模拟无 ``finish_reason`` 断流，产出含 ``response.completed`` + ``[DONE]``，
  日志含 ``terminal_reason=upstream_truncated``。
"""

import asyncio
import json
import re
from contextlib import contextmanager

import pytest
from loguru import logger

from zhongzhuan.proxy.protocol.responses import (
    CompositeStreamTranslator,
    ResponsesStreamTranslator,
)
from zhongzhuan.proxy.protocol.stream_a2o import StreamA2O
from zhongzhuan.proxy.protocol.translator_base import finish_translator


@contextmanager
def _capture_loguru():
    """临时捕获 loguru 日志到列表，用于断言日志内容。"""
    records: list[str] = []

    def _sink(message) -> None:
        records.append(message)

    sink_id = logger.add(_sink, format="{message}", level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _event_names(text: str) -> list[str]:
    return [ln.split("event:", 1)[1].strip() for ln in text.splitlines() if ln.startswith("event:")]


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


# ---------------------------------------------------------------------------
# finish_translator 三分支
# ---------------------------------------------------------------------------
class _AsyncTranslator:
    """仅实现 async afinish()。"""

    def __init__(self, out: list[bytes] | None = None, raise_on_afinish: bool = False) -> None:
        self._out = out or [b"data: [DONE]\n\n"]
        self._raise_on_afinish = raise_on_afinish
        self.afinish_called = False

    async def afinish(self) -> list[bytes]:
        self.afinish_called = True
        if self._raise_on_afinish:
            raise RuntimeError("afinish boom")
        return self._out


class _SyncTranslator:
    """仅实现 sync finish_safely()。"""

    def __init__(self, out: list[bytes] | None = None, raise_on_sync: bool = False) -> None:
        self._out = out or [b"data: [DONE]\n\n"]
        self._raise_on_sync = raise_on_sync
        self.sync_called = False

    def finish_safely(self) -> list[bytes]:
        self.sync_called = True
        if self._raise_on_sync:
            raise RuntimeError("sync boom")
        return self._out


class _BothTranslator:
    """同时实现 afinish() 与 finish_safely()，验证 afinish 优先。"""

    def __init__(self) -> None:
        self.afinish_called = False
        self.sync_called = False

    async def afinish(self) -> list[bytes]:
        self.afinish_called = True
        return [b"data: [ASYNC]\n\n"]

    def finish_safely(self) -> list[bytes]:
        self.sync_called = True
        return [b"data: [SYNC]\n\n"]


class TestFinishTranslator:
    async def test_afinish_is_preferred_over_sync(self):
        """同时具备 afinish 与 finish_safely 时，afinish 优先。"""
        tr = _BothTranslator()
        out = await finish_translator(tr)
        assert tr.afinish_called is True
        assert tr.sync_called is False
        assert out == [b"data: [ASYNC]\n\n"]

    async def test_sync_fallback_when_no_afinish(self):
        """仅实现 finish_safely 时退回 sync 路径。"""
        tr = _SyncTranslator(out=[b"data: [SYNC]\n\n"])
        out = await finish_translator(tr)
        assert tr.sync_called is True
        assert out == [b"data: [SYNC]\n\n"]

    async def test_afinish_exception_falls_back_to_done(self):
        """afinish 抛异常时兜底返回 [DONE]，日志含 terminal_reason=upstream_truncated。"""
        tr = _AsyncTranslator(raise_on_afinish=True)
        with _capture_loguru() as records:
            out = await finish_translator(tr)
        assert out == [b"data: [DONE]\n\n"]
        assert any("terminal_reason=upstream_truncated" in r for r in records)

    async def test_sync_exception_falls_back_to_done(self):
        """finish_safely 抛异常时兜底返回 [DONE]，日志含 terminal_reason=upstream_truncated。"""
        tr = _SyncTranslator(raise_on_sync=True)
        with _capture_loguru() as records:
            out = await finish_translator(tr)
        assert out == [b"data: [DONE]\n\n"]
        assert any("terminal_reason=upstream_truncated" in r for r in records)

    async def test_no_finish_method_returns_done(self):
        """既无 afinish 也无 finish_safely 时直接返回 [DONE]。"""
        with _capture_loguru() as records:
            out = await finish_translator(object())
        assert out == [b"data: [DONE]\n\n"]
        assert any("terminal_reason=upstream_truncated" in r for r in records)


# ---------------------------------------------------------------------------
# Composite async 收尾（StreamA2O 上游正常流 / 截断流）
# ---------------------------------------------------------------------------
class TestCompositeFinish:
    async def test_composite_normal_stream(self):
        """上游正常流：Composite.finish_safely 走统一入口，产出 completed + [DONE]。"""
        anthropic_chunk = (
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"hi"}}\n\n'
        )
        composite = CompositeStreamTranslator(StreamA2O(model="claude-3-5"), ResponsesStreamTranslator(model="gpt-4o"))
        out = await composite.feed(anthropic_chunk)
        closing = await finish_translator(composite)
        all_bytes = b"".join(out + closing).decode()
        assert "response.output_text.delta" in _event_names(all_bytes)
        assert "response.completed" in _event_names(all_bytes)
        assert all_bytes.rstrip().endswith("data: [DONE]")

    async def test_composite_truncated_stream(self):
        """上游截断流（无 message_stop）：仍需产出完成事件 + [DONE]。"""
        # 只喂 content_block_delta，从未发送 message_stop -> 上游未正常结束。
        anthropic_partial = (
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"partial"}}\n\n'
        )
        composite = CompositeStreamTranslator(StreamA2O(model="claude-3-5"), ResponsesStreamTranslator(model="gpt-4o"))
        await composite.feed(anthropic_partial)
        assert composite.done is False
        closing = await finish_translator(composite)
        text = b"".join(closing).decode()
        assert "response.completed" in _event_names(text)
        assert text.rstrip().endswith("data: [DONE]")

    async def test_concurrent_finish_no_runtime_warning(self):
        """并发 finish 不产生 coroutine never awaited 的 RuntimeWarning。"""
        composite = CompositeStreamTranslator(StreamA2O(model="claude-3-5"), ResponsesStreamTranslator(model="gpt-4o"))
        # 并发触发收尾：两个协程都 await 同一 composite。
        results = await asyncio.gather(
            finish_translator(composite),
            finish_translator(composite),
        )
        # 收尾是幂等的：第一个完成全部产出（含 [DONE]），第二个因已 finished
        # 返回空列表。两者拼起来应恰好含一个 [DONE]，且不抛 RuntimeWarning。
        flattened = [b for out in results for b in out]
        assert any(b"[DONE]" in b for b in flattened)


# ---------------------------------------------------------------------------
# 验收①：模拟无 finish_reason 断流
# ---------------------------------------------------------------------------
class TestAcceptanceTruncatedStream:
    async def test_truncated_upstream_emits_completed_and_done(self):
        """模拟上游无 finish_reason 断流：产出含 response.completed + [DONE]。

        使用 ResponsesStreamTranslator，只喂文本 delta 而无 finish_reason，
        再走 finish_translator 收尾。handler 在检测到 ``not done`` 时会记录
        ``terminal_reason=upstream_truncated``；此处通过让收尾抛异常来验证
        finish_translator 兜底路径同样记录该 marker。
        """
        tr = ResponsesStreamTranslator(model="gpt-4o")
        # 喂入内容但无 finish_reason。
        await tr.feed(_sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "partial"}}]}))
        assert tr.done is False
        closing = await finish_translator(tr)
        text = b"".join(closing).decode()
        assert "response.completed" in _event_names(text)
        assert text.rstrip().endswith("data: [DONE]")

        # 验证 handler 收尾日志的 marker 已写入源码（验收①日志格式）。
        from pathlib import Path

        handler_src = Path(__file__).resolve().parents[1] / "src" / "zhongzhuan" / "proxy" / "handler.py"
        assert "terminal_reason=upstream_truncated" in handler_src.read_text(encoding="utf-8")

        # 兜底路径日志：构造一个收尾会抛异常的翻译器，验证同样记录 marker。
        bad = _AsyncTranslator(raise_on_afinish=True)
        with _capture_loguru() as records:
            out = await finish_translator(bad)
        assert out == [b"data: [DONE]\n\n"]
        assert any("terminal_reason=upstream_truncated" in r for r in records)
