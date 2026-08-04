"""T28 tests: heartbeat / client-cancel / timeout classification / dual-mode abort.

Acceptance mapping
------------------
① R-P0-21  silent 120s -> >=7 heartbeats, state STREAMING .. test_silent_upstream_120s_heartbeats
② R-P1-24  client disconnect -> upstream closed <=1s ........ test_client_cancel_closes_upstream_quickly
③ R-P1-25  cancel skips mark_failure, health unchanged ...... test_client_cancel_does_not_mark_failure
④ R-P1-26  four timeout terminal_reasons distinct .......... test_four_timeout_reasons_distinct
⑤ R-P1-27  SSE not gzipped + heartbeat gap <=16s ........... test_gzip_skips_sse / test_heartbeat_gap_cap
⑥ R-P1-22  compat truncation (text + tool args) ........... test_text_truncation_compat / test_tool_truncation_compat
⑦ R-P1-23  strict truncation, [DONE] last ................. test_text_truncation_strict / test_tool_truncation_strict
⑧ R-P0-21  deployment doc proxy_read_timeout > read timeout  test_deployment_doc_proxy_read_timeout

All timing is event / injectable-clock driven -- no real 120s / 2s waits.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from zhongzhuan.config import default_config
from zhongzhuan.proxy.protocol.responses_models import (
    SSE_DONE_FRAME,
    SSE_HEARTBEAT_FRAME,
    TIMEOUT_REASONS,
    Capability,
    TerminalReason,
)
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.responses_v3.pipeline import (
    PipelineConfig,
    ResponsePipeline,
)
from zhongzhuan.responses_v3.hosted_tools import (
    HostedToolRecognizer,
    HostedToolValidator,
    hosted_tool_emulated_capabilities,
    resolve_mcp_executor,
)
from zhongzhuan.store.response_store import ResponseStore

ROOT = Path(__file__).resolve().parent.parent


def _rs(store) -> ResponseStore:
    """Wrap the shared SqliteStore in the ResponseStore the pipeline expects."""
    return ResponseStore(store)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable monotonic-shaped clock; tests advance it by hand."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_fake_sleep(clock: FakeClock):
    """An injectable sleep that advances ``clock`` instantly, no real wait."""

    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)  # yield so sibling tasks can run

    return _sleep


class SilentAfterFirstUpstream:
    """Yields one text delta, then stays silent forever (stream stays open)."""

    def __init__(self) -> None:
        self._sent = False
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._sent:
            self._sent = True
            return {"type": "text", "delta": "hi"}
        await asyncio.Event().wait()  # block forever; cancelled on teardown
        raise StopAsyncIteration  # pragma: no cover - unreachable

    async def aclose(self) -> None:
        self.closed = True


class BlockingUpstream:
    """Never yields; records aclose.  Used for cancel-propagation tests."""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()  # block forever
        raise StopAsyncIteration  # pragma: no cover - unreachable

    async def aclose(self) -> None:
        self.closed = True


def _parse_frames(frames: list[bytes]) -> list[tuple[str, dict]]:
    """Parse SSE frames into [(event_type, data)]; heartbeats are skipped."""
    events: list[tuple[str, dict]] = []
    for frame in frames:
        text = frame.decode("utf-8")
        if text == "data: [DONE]\n\n":
            events.append(("[DONE]", {}))
            continue
        if text.startswith(":"):  # comment heartbeat
            continue
        lines = text.splitlines()
        event_type = None
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        if event_type is None:
            continue
        data = json.loads("\n".join(data_lines)) if data_lines else {}
        events.append((event_type, data))
    return events


def _done_items(events: list[tuple[str, dict]]) -> list[dict]:
    """All output_item.done payloads (for the "safe close" assertion)."""
    return [data for ev, data in events if ev == "response.output_item.done"]


def _event_types(events: list[tuple[str, dict]]) -> list[str]:
    return [ev for ev, _ in events]


# ---------------------------------------------------------------------------
# ① R-P0-21 -- silent 120s still streams, >=7 heartbeats, state STREAMING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silent_upstream_120s_heartbeats(store):
    clock = FakeClock()
    sleeper = _make_fake_sleep(clock)
    upstream = SilentAfterFirstUpstream()
    pipeline = ResponsePipeline(
        "resp_hb",
        workspace_id="t1",
        store=_rs(store),
        config=PipelineConfig(heartbeat_seconds=15),
    )
    gen = pipeline.run(upstream, clock=clock, sleep=sleeper)

    heartbeats = 0
    frames: list[bytes] = []
    try:
        while heartbeats < 8:  # 8 * 15s = 120s simulated
            # Safety timeout: in normal operation the injectable clock makes
            # each step instant, so this only fires on a broken heartbeat loop.
            frame = await asyncio.wait_for(gen.__anext__(), timeout=10.0)
            frames.append(frame)
            if frame == SSE_HEARTBEAT_FRAME:
                heartbeats += 1
    finally:
        await gen.aclose()

    # Simulated wall clock reached ~120s of streaming.
    assert clock.t >= 120.0
    assert heartbeats >= 7
    # Heartbeats never transition the state machine (B8 / R-P0-21).
    assert pipeline.state == "streaming"
    events = _parse_frames(frames)
    assert "[DONE]" not in _event_types(events)  # stream is still alive
    assert pipeline.stats.heartbeats >= 7


# ---------------------------------------------------------------------------
# ② R-P1-24 -- client disconnect closes the upstream in <=1s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_cancel_closes_upstream_quickly(store):
    clock = FakeClock()
    sleeper = _make_fake_sleep(clock)
    cancel = asyncio.Event()
    upstream = BlockingUpstream()
    pipeline = ResponsePipeline(
        "resp_c2",
        workspace_id="t1",
        store=_rs(store),
        config=PipelineConfig(heartbeat_seconds=15),
    )
    gen = pipeline.run(upstream, client_cancelled=cancel, clock=clock, sleep=sleeper)

    async def drain():
        async for _ in gen:
            pass

    task = asyncio.create_task(drain())
    await asyncio.sleep(0)  # let the producer start reading
    # The client "disconnects" after 2s of silence (simulated, no real wait).
    clock.advance(2.0)
    started = time.monotonic()
    cancel.set()
    await asyncio.wait_for(task, timeout=1.0)
    elapsed = time.monotonic() - started

    assert upstream.closed
    assert elapsed <= 1.0
    assert pipeline.stats.client_disconnects == 1


# ---------------------------------------------------------------------------
# ③ R-P1-25 -- cancel never calls mark_failure; key health dims unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_cancel_does_not_mark_failure(store):
    key = KeyHealth(key_id=1, api_key="sk-test", window=SlidingWindow(0, 0))
    before = (
        key.status,
        key.total_failures,
        key.consecutive_failures,
        key.cooldown_until,
        key.recent_429_count,
        key.success_count,
    )

    cancel = asyncio.Event()
    upstream = BlockingUpstream()
    pipeline = ResponsePipeline("resp_c3", workspace_id="t1", store=_rs(store))
    gen = pipeline.run(upstream, client_cancelled=cancel, key_health=key)

    async def drain():
        async for _ in gen:
            pass

    task = asyncio.create_task(drain())
    await asyncio.sleep(0)
    cancel.set()
    await asyncio.wait_for(task, timeout=1.0)

    after = (
        key.status,
        key.total_failures,
        key.consecutive_failures,
        key.cooldown_until,
        key.recent_429_count,
        key.success_count,
    )
    # Positive assertion: NOTHING about the key changed (a mark_failure call
    # would have flipped status/total_failures/cooldown and this test would
    # fail) -- not just "the counter went up".
    assert after == before
    assert pipeline.stats.client_disconnects == 1
    # The metric name from §10.5 is responses_client_disconnect_total.
    assert pipeline.stats.client_disconnects == 1


# ---------------------------------------------------------------------------
# ④ R-P1-26 -- four timeout terminal_reasons are mutually distinct
# ---------------------------------------------------------------------------


def test_four_timeout_reasons_distinct():
    values = [r.value for r in TIMEOUT_REASONS]
    assert len(TIMEOUT_REASONS) == 4
    assert len(set(values)) == 4, values
    assert TerminalReason.UPSTREAM_CONNECT.value in values
    assert TerminalReason.FIRST_TOKEN_TIMEOUT.value in values
    assert TerminalReason.READ_IDLE_TIMEOUT.value in values
    assert TerminalReason.MAX_RESPONSE_TIME.value in values


# ---------------------------------------------------------------------------
# ⑤ R-P1-27 -- SSE is never gzipped; heartbeat gap is capped at 16s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gzip_skips_sse():
    from zhongzhuan.proxy.server import _make_gzip_middleware

    mw = _make_gzip_middleware(min_size=8)

    async def sse_handler(request):
        return web.Response(body=b"x" * 100, content_type="text/event-stream")

    req = make_mocked_request(
        "GET",
        "/v1/responses",
        headers={"Accept-Encoding": "gzip"},
    )
    resp = await mw(req, sse_handler)
    assert "Content-Encoding" not in resp.headers
    assert resp.body == b"x" * 100


@pytest.mark.asyncio
async def test_gzip_still_compresses_json_control():
    from zhongzhuan.proxy.server import _make_gzip_middleware

    mw = _make_gzip_middleware(min_size=8)

    async def json_handler(request):
        return web.Response(
            body=json.dumps({"msg": "y" * 100}).encode(),
            content_type="application/json",
        )

    req = make_mocked_request(
        "GET",
        "/v1/chat/completions",
        headers={"Accept-Encoding": "gzip"},
    )
    resp = await mw(req, json_handler)
    assert resp.headers.get("Content-Encoding") == "gzip"


def test_heartbeat_gap_cap():
    cfg = PipelineConfig()
    # Default heartbeat interval (15s) must stay under the 16s cap.
    assert cfg.heartbeat_seconds <= cfg.max_heartbeat_gap_seconds
    assert cfg.heartbeat_seconds <= 16.0


# ---------------------------------------------------------------------------
# ⑥ R-P1-22 -- compatibility mode truncation (text + tool args)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_truncation_compat(store):
    async def upstream():
        yield {"type": "text", "delta": "hel"}
        yield {"type": "text", "delta": "lo"}
        raise ConnectionError("upstream broke mid-text")

    pipeline = ResponsePipeline("resp_t6", workspace_id="t1", store=_rs(store))
    frames = [f async for f in pipeline.run(upstream())]
    events = _parse_frames(frames)
    types = _event_types(events)

    # 1. safely closed the open item
    done_items = _done_items(events)
    assert done_items, "expected an output_item.done (safe close)"
    assert all(it["item"]["status"] == "incomplete" for it in done_items)
    # 2. no partial arguments done (trivially true for a text stream)
    assert "response.function_call_arguments.done" not in types
    # 3. completed + [DONE]
    assert "response.completed" in types
    assert frames[-1] == SSE_DONE_FRAME
    # 4. terminal_reason + incomplete_details on the terminal event
    completed = next(data for ev, data in events if ev == "response.completed")
    assert completed["response"]["terminal_reason"] == TerminalReason.UPSTREAM_TRUNCATED.value
    assert completed["response"]["incomplete_details"]["reason"] == TerminalReason.UPSTREAM_TRUNCATED.value
    assert pipeline.state == "completed"
    assert pipeline.stats.terminal_reason == TerminalReason.UPSTREAM_TRUNCATED.value


@pytest.mark.asyncio
async def test_tool_truncation_compat(store):
    async def upstream():
        yield {"type": "tool_call", "call_id": "call_1", "name": "web_search", "arguments": '{"query": "北'}
        yield {"type": "tool_call", "call_id": "call_1", "name": "web_search", "arguments": '京"}'}
        raise ConnectionError("upstream broke mid-arguments")

    pipeline = ResponsePipeline("resp_t6b", workspace_id="t1", store=_rs(store))
    frames = [f async for f in pipeline.run(upstream())]
    events = _parse_frames(frames)
    types = _event_types(events)

    # 1. safely closed the function_call item as incomplete
    done_items = _done_items(events)
    assert done_items
    call_done = [it for it in done_items if it["item"]["type"] == "function_call"]
    assert call_done and call_done[0]["item"]["status"] == "incomplete"
    # 2. NEVER emitted arguments.done for a partial call (R-P1-22 core)
    assert "response.function_call_arguments.done" not in types
    # 3. completed + [DONE]
    assert "response.completed" in types
    assert frames[-1] == SSE_DONE_FRAME
    # 4. terminal_reason + incomplete_details
    completed = next(data for ev, data in events if ev == "response.completed")
    assert completed["response"]["terminal_reason"] == TerminalReason.UPSTREAM_TRUNCATED.value
    assert completed["response"]["incomplete_details"]["reason"] == TerminalReason.UPSTREAM_TRUNCATED.value


# ---------------------------------------------------------------------------
# ⑦ R-P1-23 -- strict mode: failed/incomplete, [DONE] still last
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_truncation_strict(store):
    async def upstream():
        yield {"type": "text", "delta": "hello"}
        raise ConnectionError("upstream broke mid-text")

    pipeline = ResponsePipeline(
        "resp_t7",
        workspace_id="t1",
        store=_rs(store),
        config=PipelineConfig(strict_terminal=True),
    )
    frames = [f async for f in pipeline.run(upstream())]
    events = _parse_frames(frames)
    types = _event_types(events)

    assert "response.completed" not in types
    assert "response.failed" in types or "response.incomplete" in types
    # [DONE] is still the very last frame (direct frame-sequence assertion).
    assert frames[-1] == SSE_DONE_FRAME
    terminal = events[-1] if events[-1][0] != "[DONE]" else events[-2]
    assert terminal[0] in ("response.failed", "response.incomplete")
    assert terminal[1]["response"]["terminal_reason"] == TerminalReason.UPSTREAM_TRUNCATED.value


@pytest.mark.asyncio
async def test_tool_truncation_strict(store):
    async def upstream():
        yield {"type": "tool_call", "call_id": "call_1", "name": "web_search", "arguments": '{"query": "北'}
        raise ConnectionError("upstream broke mid-arguments")

    pipeline = ResponsePipeline(
        "resp_t7b",
        workspace_id="t1",
        store=_rs(store),
        config=PipelineConfig(strict_terminal=True),
    )
    frames = [f async for f in pipeline.run(upstream())]
    events = _parse_frames(frames)
    types = _event_types(events)

    # Partial arguments never produce arguments.done in strict mode either.
    assert "response.function_call_arguments.done" not in types
    assert "response.completed" not in types
    assert frames[-1] == SSE_DONE_FRAME
    terminal = events[-1] if events[-1][0] != "[DONE]" else events[-2]
    assert terminal[0] in ("response.failed", "response.incomplete")


# ---------------------------------------------------------------------------
# ⑧ R-P0-21 -- deployment doc requires proxy_read_timeout > app read timeout
# ---------------------------------------------------------------------------


def test_deployment_doc_proxy_read_timeout():
    doc = ROOT / "docs" / "zhongzhuan Responses Bridge v3 开发文档 2f2b9eee3f75420f92230f37e125ace1.md"
    assert doc.exists(), "deployment doc missing"
    text = doc.read_text(encoding="utf-8")
    # The requirement is stated and the "why" explains the terminal_reason
    # loss when the reverse proxy cuts the connection first.
    assert "proxy_read_timeout" in text
    assert "terminal_reason" in text
    assert "连接重置" in text or "ConnectionReset" in text


# ---------------------------------------------------------------------------
# T27 leftover opt-in: mcp executor is off by default, reachable when enabled
# ---------------------------------------------------------------------------


def test_mcp_optin_default_off_returns_unsupported_tool():
    cfg = default_config()
    assert cfg.hosted_tools.mcp_enabled is False
    assert Capability.REMOTE_MCP not in hosted_tool_emulated_capabilities(cfg)
    assert resolve_mcp_executor(cfg) is None

    # The validator treats `mcp` as unservable by default -> 400 unsupported_tool.
    validator = HostedToolValidator(emulated=hosted_tool_emulated_capabilities(cfg))

    payload = {"tools": [{"type": "mcp", "name": "notes", "server_url": "http://localhost:8080"}]}
    specs = HostedToolRecognizer().recognize(payload)
    assert specs and specs[0].required_capability is Capability.REMOTE_MCP
    err = validator.validate(specs, available=frozenset())
    assert err is not None
    assert err.http_status == 400
    status, body = err.to_response()
    assert status == 400
    assert body["error"]["code"] == "unsupported_tool"


def test_mcp_optin_enabled_reaches_executor():
    cfg = default_config()
    cfg.hosted_tools.mcp_enabled = True
    assert Capability.REMOTE_MCP in hosted_tool_emulated_capabilities(cfg)

    # Same request now passes validation (servable)...
    validator = HostedToolValidator(emulated=hosted_tool_emulated_capabilities(cfg))

    payload = {"tools": [{"type": "mcp", "name": "notes", "server_url": "http://localhost:8080"}]}
    specs = HostedToolRecognizer().recognize(payload)
    assert validator.validate(specs, available=frozenset()) is None

    # ...and the executor (T27 McpClient) is actually resolvable.
    executor = resolve_mcp_executor(cfg)
    assert executor is not None
    from zhongzhuan.responses_v3.mcp_client import McpClient

    assert isinstance(executor, McpClient)
