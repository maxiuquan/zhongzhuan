"""T02: real ``ProxyServer.app()`` integration tests for v3 **streaming** create.

These boot the production aiohttp app and drive a real
``POST /v1/responses {"stream": true}`` through the single v2/v3 fork point
against the programmable mock upstream.  Unlike ``test_proxy_v3_create.py``
(which covers the non-stream shape), every assertion here is about the *wire*:
the response is a genuine ``text/event-stream``, the SSE lifecycle is emitted
exactly once by the pipeline, and the two-phase commit line holds — a Phase A
failure is a JSON error with **zero** SSE bytes, never "HTTP 200 + a sad event".

Acceptance mapping:

===========  ==================================================================
AC-1.1       Phase A failure returns JSON; the body contains no ``event:`` line
AC-1.2       ``stream=true`` really streams (``Content-Type: text/event-stream``)
AC-1.3       The last frame on the wire is ``data: [DONE]``
AC-1.4       Every lifecycle event type appears at most once
AC-1.5       The non-stream path is unchanged (that suite stays green)
AC-2.4       The streamed terminal is persisted and retrievable under our id
AC-3.5       Malformed tool arguments never produce a whitewashed ``completed``
AC-7.4       The pipeline timeouts come from ``responses_bridge.timeout.*``
AC-7.5       Values wider than 铁律 5 are clamped, not obeyed
AC-8.1       Startup writes the five-field v3 switch audit line
===========  ==================================================================

Honest labeling: these are ``mock回放`` results driven by
``tests/support/mock_responses_upstream.py``, not a live-OpenAI ``真机`` run.
"""

from __future__ import annotations

import json
import socket

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from support.mock_responses_upstream import (
    MockUpstream,
    UpstreamBehavior,
    anthropic_text_stream,
    by_n_bytes,
    openai_error_json,
    openai_text_stream,
    openai_tool_stream,
    random_split,
)
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.upstream import UpstreamClient


# ---------------------------------------------------------------------------
# Harness (mirrors test_proxy_v3_create.py so both suites boot the same app)
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _start_proxy(upstream_url: str, store, *, token: str = "", protocol: str = "openai"):
    """Boot the production app against ``upstream_url``.

    Args:
        upstream_url: Base URL of the mock upstream.
        store: The async store fixture.
        token: Reuse an existing proxy access token; minted when empty.
        protocol: Upstream wire protocol to pin on the key. ``"openai"`` keeps
            the historical behaviour (``ProxyServer`` synthesises a fallback
            key); ``"anthropic"`` builds an **explicit** key so the request
            really travels the translate path to ``/v1/messages``. Feeding an
            Anthropic-shaped payload to an ``openai`` key would only prove the
            OpenAI adapter ignores it — not that the Anthropic one normalises.

    Returns:
        ``(port, runner, upstream_client, token)``.
    """
    import os

    os.environ["ZHONGZHUAN_PROXY_AUTH"] = "true"
    if not token:
        from zhongzhuan.store.access_tokens import create_token

        token = (await create_token(store, label="stream-token", quota_tokens=100000)).token
    upstream = UpstreamClient(base_url=upstream_url, timeout=10.0)
    await upstream.start()
    keys: list = []
    if protocol != "openai":
        from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow

        keys = [
            KeyHealth(
                key_id=0,
                api_key="sk-upstream",
                window=SlidingWindow(60, 1000),
                upstream_base=upstream_url,
                upstream_protocol=protocol,
            )
        ]
    proxy = ProxyServer(
        upstream_clients={upstream_url: upstream},
        api_key="sk-upstream",
        keys=keys,
        proxy_timeout=10.0,
        store=store,
        responses_bridge=None,  # default enabled
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return port, runner, upstream, token


@pytest_asyncio.fixture
async def astore(tmp_path, monkeypatch):
    """Async SQLite store fixture (mirrors conftest)."""
    for var in ("HOST", "PORT", "USER", "PASSWORD", "DATABASE"):
        monkeypatch.delenv(f"ZHONGZHUAN_TIDB_{var}", raising=False)
    from zhongzhuan.config import default_config
    from zhongzhuan.store.store import create_store

    cfg = default_config()
    cfg.storage.backend = "sqlite"
    cfg.storage.db_path = str(tmp_path / "test.db")
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    s = await create_store(cfg)
    try:
        yield s
    finally:
        await s.close()


async def _stream(port: int, body: dict, token: str) -> tuple[int, str, bytes]:
    """POST a streaming create; return ``(status, content_type, raw_body)``.

    The raw bytes are returned deliberately: the SSE framing (blank-line
    separators, the exact ``data: [DONE]`` sentinel) is part of the contract
    and would be destroyed by a helpful line-parsing helper.
    """
    async with ClientSession() as sess:
        async with sess.post(
            f"http://127.0.0.1:{port}/v1/responses",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            data=json.dumps(body),
        ) as resp:
            raw = await resp.read()
            return resp.status, resp.headers.get("Content-Type", ""), raw


def _events(raw: bytes) -> list[dict]:
    """Parse every JSON ``data:`` payload on the wire, in order."""
    events: list[dict] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _event_types(raw: bytes) -> list[str]:
    """Extract the ``"type"`` of every JSON ``data:`` payload, in order."""
    return [e["type"] for e in _events(raw) if isinstance(e.get("type"), str)]


def _joined_deltas(raw: bytes, event_type: str) -> str:
    """Reassemble the ``delta`` fragments of one event type.

    Deltas are *fragments* by definition — asserting on a contiguous substring
    of the raw SSE would only pass by accident (and silently break the moment
    the upstream chunks differently).
    """
    return "".join(str(e.get("delta") or "") for e in _events(raw) if e.get("type") == event_type)


def _response_id(raw: bytes) -> str:
    """The response id announced by ``response.created``."""
    for event in _events(raw):
        if event.get("type") == "response.created":
            return str(event.get("response", {}).get("id") or "")
    raise AssertionError(f"no response.created in stream:\n{raw.decode(errors='replace')}")


#: The lifecycle events that may appear **at most once** per response (AC-1.4).
_LIFECYCLE_ONCE = (
    "response.created",
    "response.in_progress",
    "response.completed",
    "response.failed",
    "response.incomplete",
    "response.cancelled",
)


def _assert_lifecycle_unique(raw: bytes) -> list[str]:
    """AC-1.4 / 铁律 3: no lifecycle event type is ever emitted twice."""
    types = _event_types(raw)
    for name in _LIFECYCLE_ONCE:
        assert types.count(name) <= 1, f"{name} emitted {types.count(name)}x:\n{raw.decode(errors='replace')}"
    terminals = [t for t in types if t in _LIFECYCLE_ONCE[2:]]
    assert len(terminals) == 1, f"expected exactly one terminal, got {terminals}"
    return types


async def _workspace(store, token: str) -> str:
    from zhongzhuan.store.access_tokens import get_token_by_value

    at = await get_token_by_value(store, token)
    assert at is not None
    return f"token:{at.id}"


# ---------------------------------------------------------------------------
# AC-1.2 / AC-1.3 / AC-1.4 / AC-2.4: the happy streaming path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_text_is_real_sse_and_persists(astore):
    """A text stream is real SSE, ends in ``[DONE]``, and leaves a terminal row."""
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=("Hello", ", ", "world", "!")),
            chunk_strategy=by_n_bytes(17),  # framing-agnostic parsing (§9.3)
        )
    )
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(port, {"model": "gpt-4o", "input": "hi", "stream": True}, token)

        # AC-1.2: a genuine event-stream, not a JSON body with a stream flag.
        assert status == 200, raw
        assert ctype.startswith("text/event-stream"), ctype
        # AC-1.3: the sentinel is the LAST thing on the wire.
        assert raw.rstrip().endswith(b"data: [DONE]"), raw[-200:]

        types = _assert_lifecycle_unique(raw)
        assert types[0] == "response.created"
        assert "response.completed" in types
        # The text actually made it through the adapter -> pipeline chain.
        assert _joined_deltas(raw, "response.output_text.delta") == "Hello, world!"

        # The upstream really was called, in translated Chat form.
        assert up.request_count == 1
        assert up.requests[0].path == "/v1/chat/completions"
        assert json.loads(up.requests[0].body).get("stream") is True

        # AC-2.4: the streamed response is retrievable under OUR id.
        ws = await _workspace(astore, token)
        rid = _response_id(raw)
        rec = await ResponseStore(astore).get_response(rid, workspace_id=ws)
        assert rec is not None
        assert rec.status == "completed"
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_stream_survives_adversarial_chunking(astore):
    """Random TCP splits must not change a single emitted event (§9.3)."""
    payload = openai_text_stream(pieces=("a", "b", "c"))
    seen: list[list[str]] = []
    for strategy in (by_n_bytes(1), random_split(seed=7), by_n_bytes(4096)):
        up = MockUpstream()
        up.set_behavior(UpstreamBehavior(stream_payload=payload, chunk_strategy=strategy))
        await up.start()
        port, runner, upstream, token = await _start_proxy(up.url, astore)
        try:
            status, ctype, raw = await _stream(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
            assert status == 200
            assert ctype.startswith("text/event-stream")
            assert raw.rstrip().endswith(b"data: [DONE]")
            seen.append(_assert_lifecycle_unique(raw))
        finally:
            await runner.cleanup()
            await upstream.close()
            await up.stop()
    # Byte framing is a transport detail: the event sequence must be identical.
    assert seen[0] == seen[1] == seen[2], seen


@pytest.mark.asyncio
async def test_stream_anthropic_upstream_normalises_to_responses_events(astore):
    """An Anthropic-shaped upstream still yields Responses-vocabulary events.

    The key is pinned to ``upstream_protocol="anthropic"`` on purpose. Without
    it ``ProxyServer`` synthesises an ``openai`` fallback key, the request goes
    to ``/v1/chat/completions``, and the OpenAI adapter simply *discards* the
    Anthropic frames it cannot read — the test would then pass while proving
    nothing about normalisation. Asserting the upstream path and a non-empty
    reassembled text closes that blind spot.
    """
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=anthropic_text_stream()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore, protocol="anthropic")
    try:
        status, ctype, raw = await _stream(
            port,
            {"model": "claude-3-5-sonnet", "input": "hi", "stream": True},
            token,
        )
        assert status == 200, raw
        assert ctype.startswith("text/event-stream")
        # The translate path really ran: Anthropic keys speak /v1/messages.
        assert [r.path for r in up.requests] == ["/v1/messages"], [r.path for r in up.requests]
        types = _assert_lifecycle_unique(raw)
        # No Anthropic wire vocabulary may leak to the client.
        assert not [t for t in types if t.startswith(("message_", "content_block_"))], types
        # Normalisation is not "swallow everything": the text must survive the
        # Anthropic -> unified -> Responses hop intact.
        assert "response.output_text.delta" in types, types
        assert _joined_deltas(raw, "response.output_text.delta") != ""
        assert raw.rstrip().endswith(b"data: [DONE]")
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# 证明 1: v3 生产 SSE 路径（stream=true 走真实 HTTP 生产路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_true_enters_v3_production_sse_path(astore):
    """证明 1: stream=true 真实进入 v3 HTTP SSE 生产路径。

    经真实 ``POST /v1/responses`` 驱动 aiohttp handler（不是对 pipeline 的单元
    测试），断言响应头 ``text/event-stream``、首帧 ``response.created``、末帧
    ``[DONE]``。v3 生产路径只使用 ``ResponsePipeline``，绝不触达 legacy bridge。
    """
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream(pieces=("real", "sse"))))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(port, {"model": "gpt-4o", "input": "hi", "stream": True}, token)

        # 真实 SSE 生产路径：HTTP 头 + 首帧 + 末帧。
        assert status == 200, raw
        assert ctype.startswith("text/event-stream"), ctype
        types = _event_types(raw)
        assert types[0] == "response.created", types
        assert "response.in_progress" in types
        assert raw.rstrip().endswith(b"data: [DONE]"), raw[-200:]

        # 首帧 response.created 携带统一的 id。
        assert _response_id(raw).startswith("resp_")
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# 证明 2: previous_response_id 恢复链写进上游 payload
# ---------------------------------------------------------------------------


async def _seed_parent_turn(astore, *, workspace_id: str, response_id: str, text: str) -> None:
    """Seed a completed parent turn with a tool exchange.

    The parent carries: an ancestor user message, a tool call (function_call
    with legal arguments), a tool output (function_call_output), and a reasoning
    item so the test proves it is excluded from the replay.
    """
    rs = ResponseStore(astore)
    await rs.create_response(
        response_id=response_id,
        workspace_id=workspace_id,
        model="gpt-4o",
        status="completed",
        request={
            "model": "gpt-4o",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
        },
    )
    await rs.update_status(
        response_id,
        "completed",
        workspace_id=workspace_id,
        output=[
            # Reasoning must be excluded from the upstream replay (铁律 1).
            {
                "id": f"rs_{response_id}",
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "SECRET-COT-PARENT"}],
            },
            {
                "id": f"msg_{response_id}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "let me check the weather"}],
            },
            {
                "id": f"fc_{response_id}",
                "type": "function_call",
                "call_id": f"call_{response_id}",
                "name": "get_weather",
                "arguments": '{"city": "Beijing"}',
                "status": "completed",
            },
            {
                "id": f"fo_{response_id}",
                "type": "function_call_output",
                "call_id": f"call_{response_id}",
                "output": '{"temp": 25}',
            },
        ],
    )


@pytest.mark.asyncio
async def test_previous_response_id_restore_chain_reaches_upstream(astore):
    """证明 2: previous_response_id 恢复链写进上游实际收到的 payload。

    捕获 mock 上游**实际收到**的 body，断言其 ``input``（responsive 链经
    ChainResolver / build_upstream_input 展平后的 history）包含祖先消息、工具
    调用（function_call + 合法 arguments）、工具输出（function_call_output），
    且**排除 reasoning**。
    """
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream(pieces=("ok",))))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        ws = await _workspace(astore, token)
        parent_id = "resp_parent_0001"
        await _seed_parent_turn(astore, workspace_id=ws, response_id=parent_id, text="What's the weather?")

        status, ctype, raw = await _stream(
            port,
            {
                "model": "gpt-4o",
                "input": "and tomorrow?",
                "stream": True,
                "previous_response_id": parent_id,
            },
            token,
        )
        assert status == 200, raw
        assert ctype.startswith("text/event-stream")
        assert raw.rstrip().endswith(b"data: [DONE]")

        # 捕获上游**实际收到**的请求体（Chat Completions 翻译后的 messages）。
        assert up.request_count >= 1
        req_body = up.requests[0].json()
        assert req_body is not None
        messages = req_body.get("messages") or []
        blob = json.dumps(messages, ensure_ascii=False)

        # 祖先消息（user turn）被恢复进上游 payload。
        assert any(m.get("role") == "user" and "What's the weather?" in json.dumps(m) for m in messages)
        assert "What's the weather?" in blob
        # 当前 turn 的输入也应在。
        assert "and tomorrow?" in blob

        # 工具调用 + 工具输出被恢复（经 Chat 转换后成为 assistant tool_calls + tool 消息）。
        assert any(
            m.get("role") == "assistant"
            and any(tc.get("function", {}).get("name") == "get_weather" for tc in (m.get("tool_calls") or []))
            for m in messages
        )
        assert any(
            m.get("role") == "tool"
            and m.get("tool_call_id") == f"call_{parent_id}"
            and "25" in json.dumps(m.get("content"))
            for m in messages
        )
        # 合法 arguments 被完整保留（Chat 转换后 arguments 是 JSON 字符串，
        # 直接对 blob 做子串匹配会因转义失败，改为结构化断言）。
        tool_calls_flat = [
            tc.get("function", {}).get("arguments", "") for m in messages for tc in (m.get("tool_calls") or [])
        ]
        assert any(json.loads(a) == {"city": "Beijing"} for a in tool_calls_flat if a)

        # 排除 reasoning：铁律 1 —— reasoning 文本绝不进入上游 payload。
        assert "SECRET-COT-PARENT" not in blob
        assert "reasoning" not in blob
        assert not any("summary" in str(m).lower() and "SECRET" in blob for m in messages)
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# AC-3.5: tool arguments (P0-3 / 铁律 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_tool_call_emits_exactly_one_done_per_call(astore):
    """A well-formed tool call is announced, streamed and closed exactly once."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_tool_stream(), chunk_strategy=by_n_bytes(23)))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, _ctype, raw = await _stream(
            port,
            {
                "model": "gpt-4o",
                "input": "weather?",
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    }
                ],
            },
            token,
        )
        assert status == 200, raw
        types = _assert_lifecycle_unique(raw)
        # 铁律 2: one added / one done per call, and the arguments are complete.
        assert types.count("response.output_item.added") == 1
        assert types.count("response.function_call_arguments.done") == 1
        assert types.count("response.output_item.done") == 1
        # The fragmented arguments reassemble into the exact upstream JSON.
        assert _joined_deltas(raw, "response.function_call_arguments.delta") == '{"city": "Beijing"}'
        # The done event must not precede its own deltas.
        assert types.index("response.function_call_arguments.done") > types.index("response.output_item.added")
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_stream_invalid_tool_arguments_is_never_whitewashed(astore):
    """AC-3.5 / U1: broken tool JSON forces a strict terminal, even in compat mode.

    Compatibility mode may soften a *truncated text* stream into ``completed``;
    it must never do so for a tool call whose arguments do not parse, because
    the client would then execute a half-decoded function.
    """
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            # Arguments that stop mid-object: valid framing, invalid JSON.
            stream_payload=openai_tool_stream(arg_pieces=('{"cit', 'y": "Bei')),
        )
    )
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, _ctype, raw = await _stream(
            port,
            {
                "model": "gpt-4o",
                "input": "weather?",
                "stream": True,
                "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
            },
            token,
        )
        assert status == 200, raw
        types = _assert_lifecycle_unique(raw)
        assert "response.completed" not in types, f"whitewashed a broken tool call: {types}"
        assert types[-1] in ("response.failed", "response.incomplete"), types
        assert raw.rstrip().endswith(b"data: [DONE]")

        # The persisted row must agree with the wire (no "completed" in store).
        ws = await _workspace(astore, token)
        rid = _response_id(raw)
        rec = await ResponseStore(astore).get_response(rid, workspace_id=ws)
        assert rec is not None
        assert rec.status in ("failed", "incomplete"), rec.status
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# AC-1.1: two-phase commit — Phase A failures are JSON with zero SSE bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_capability_error_is_json_not_sse(astore):
    """A hosted tool with no executor fails in Phase A: JSON 400, no SSE."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(
            port,
            {
                "model": "gpt-4o",
                "input": "search",
                "stream": True,
                "tools": [{"type": "web_search", "name": "web_search"}],
            },
            token,
        )
        assert status == 400, raw
        assert "text/event-stream" not in ctype
        assert b"event:" not in raw and b"data:" not in raw
        assert json.loads(raw)["error"]["code"] == "unsupported_tool"
        assert up.request_count == 0
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_stream_chain_error_is_json_not_sse(astore):
    """A dangling ``previous_response_id`` fails in Phase A with a JSON 400."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(
            port,
            {
                "model": "gpt-4o",
                "input": "follow up",
                "stream": True,
                "previous_response_id": "resp_does_not_exist",
            },
            token,
        )
        assert status == 400, raw
        assert "text/event-stream" not in ctype
        assert b"data:" not in raw
        assert "previous_response_id" in json.loads(raw)["error"].get("param", "")
        assert up.request_count == 0
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_stream_upstream_4xx_is_json_not_fake_200(astore):
    """An upstream 4xx before the first byte stays a 4xx (never 200 + failed)."""
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            status=400,
            error_body=openai_error_json(message="bad stream input"),
            force_stream=False,
        )
    )
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
        assert status == 400, raw
        assert "text/event-stream" not in ctype
        assert b"data:" not in raw
        assert "bad stream input" in raw.decode()
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# P0-2: EOF vs truncation (the fix that stopped calling every normal finish
# a truncation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_normal_finish_without_done_sentinel_is_completed(astore):
    """An upstream that finishes cleanly but never sends ``[DONE]`` is complete.

    P0-2: "produced some chunks" is the single most common *successful* shape;
    using it as the truncation criterion mislabelled every healthy stream.
    """
    payload = openai_text_stream(pieces=("ok",), include_usage=False)
    payload = payload.replace(b"data: [DONE]\n\n", b"")  # clean EOF, no sentinel
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=payload))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, _ctype, raw = await _stream(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
        assert status == 200, raw
        types = _assert_lifecycle_unique(raw)
        # ``finish_reason: stop`` arrived, so this is a completion, not a cut.
        assert "response.completed" in types, types
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_stream_truncated_upstream_is_not_completed(astore):
    """A stream cut mid-flight (no finish signal) must not report success.

    P0-2 / 铁律 2. Counting terminals alone is not enough — a whitewashed
    ``response.completed`` is also exactly one terminal, so the old assertion
    would have happily passed on the very bug it was written to catch. The
    terminal *type* is the contract: a cut stream is ``incomplete`` (or
    ``failed``), never ``completed``.
    """
    up = MockUpstream()
    up.set_behavior(
        UpstreamBehavior(
            stream_payload=openai_text_stream(pieces=("a", "b", "c", "d")),
            chunk_strategy=by_n_bytes(64),
            truncate_after_chunks=2,  # abort before finish_reason arrives
        )
    )
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, _ctype, raw = await _stream(port, {"model": "gpt-4o", "input": "x", "stream": True}, token)
        assert status == 200, raw  # Phase B: HTTP status is already committed
        types = _event_types(raw)
        terminals = [t for t in types if t in _LIFECYCLE_ONCE[2:]]
        assert len(terminals) == 1, terminals
        assert terminals[0] in ("response.incomplete", "response.failed"), (
            f"truncated stream whitewashed to {terminals[0]}:\n{raw.decode(errors='replace')}"
        )
        # The persisted record must tell the same story as the wire (AC-2.4):
        # a client that reconnects and GETs the id may not see "completed".
        rid = _response_id(raw)
        assert rid
        rec = await ResponseStore(astore).get_response(rid, workspace_id=await _workspace(astore, token))
        assert rec is not None
        assert rec.status in ("incomplete", "failed"), rec.status
        assert raw.rstrip().endswith(b"data: [DONE]")
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# U2 / P0-6: background + stream is rejected up front
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_background_plus_stream_is_400(astore):
    """U2: ``background=true`` with ``stream=true`` is a 400 in GA v1.

    Streaming a background job would need a second delivery channel that GA
    does not ship; answering 400 is honest, whereas silently dropping one of
    the two flags would surprise the caller either way.
    """
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        status, ctype, raw = await _stream(
            port,
            {"model": "gpt-4o", "input": "x", "stream": True, "background": True},
            token,
        )
        assert status == 400, raw
        assert "text/event-stream" not in ctype
        body = json.loads(raw)
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["param"] == "stream"
        assert up.request_count == 0
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


@pytest.mark.asyncio
async def test_background_enqueue_returns_202_queued(astore):
    """P0-6: ``background=true`` returns 202 + a queued, retrievable resource."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
    await up.start()
    port, runner, upstream, token = await _start_proxy(up.url, astore)
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/responses",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                data=json.dumps({"model": "gpt-4o", "input": "slow job", "background": True}),
            ) as resp:
                assert resp.status == 202, await resp.text()
                obj = await resp.json()
        assert obj["status"] == "queued"
        rid = obj["id"]
        assert rid.startswith("resp_")

        # Enqueue does no network I/O: the upstream is still untouched.
        assert up.request_count == 0

        # The queued resource is immediately retrievable through the real app.
        async with ClientSession() as sess:
            async with sess.get(
                f"http://127.0.0.1:{port}/v1/responses/{rid}",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                assert resp.status == 200
                assert (await resp.json())["status"] == "queued"
    finally:
        await runner.cleanup()
        await upstream.close()
        await up.stop()


# ---------------------------------------------------------------------------
# AC-7.4 / AC-7.5: timeout config pass-through + 铁律 5 clamping
# ---------------------------------------------------------------------------


def test_pipeline_config_defaults_are_the_law():
    """AC-7.5 / 铁律 5: shipped defaults are 300 / 300 / 900."""
    from zhongzhuan.responses_v3.pipeline import PipelineConfig

    cfg = PipelineConfig()
    assert cfg.first_token_seconds == 300.0
    assert cfg.read_idle_seconds == 300.0
    assert cfg.total_seconds == 900.0


def test_pipeline_config_clamps_values_wider_than_the_law():
    """AC-7.3 / AC-7.5: a too-generous config is clamped, not obeyed or rejected."""
    from zhongzhuan.responses_v3.pipeline import PipelineConfig

    cfg = PipelineConfig(first_token_seconds=600.0, read_idle_seconds=600.0, total_seconds=1800.0)
    assert cfg.first_token_seconds == 300.0
    assert cfg.read_idle_seconds == 300.0
    assert cfg.total_seconds == 900.0


def test_pipeline_config_from_config_reads_responses_bridge_timeout():
    """AC-7.4: stricter values from ``responses_bridge.timeout.*`` are honoured."""
    from zhongzhuan.config.config import (
        ResponsesBridgeConfig,
        ResponsesTimeoutConfig,
    )
    from zhongzhuan.responses_v3.pipeline import PipelineConfig

    bridge = ResponsesBridgeConfig(
        timeout=ResponsesTimeoutConfig(
            first_token_seconds=30.0,
            read_idle_seconds=45.0,
            total_seconds=120.0,
            connect_seconds=5.0,
        )
    )
    cfg = PipelineConfig.from_config(bridge)
    assert cfg.first_token_seconds == 30.0
    assert cfg.read_idle_seconds == 45.0
    assert cfg.total_seconds == 120.0
    assert cfg.connect_seconds == 5.0

    # A ``None`` config must fall back to the law, never raise.
    assert PipelineConfig.from_config(None).total_seconds == 900.0


def test_handler_pipeline_config_is_wired_from_feature_flags():
    """AC-7.4 end-to-end: the handler builds its config from the bridge section."""
    from zhongzhuan.config.config import ResponsesBridgeConfig, ResponsesTimeoutConfig
    from zhongzhuan.proxy.feature_flags import ResponsesFeatureFlags
    from zhongzhuan.proxy.handler import make_handler

    bridge = ResponsesBridgeConfig(timeout=ResponsesTimeoutConfig(read_idle_seconds=12.0))
    handler = make_handler(
        upstream_clients={},
        keys=[],
        proxy_timeout=5.0,
        feature_flags=ResponsesFeatureFlags(bridge, environ={}),
    )
    cfg = handler._v3_pipeline_config()
    assert cfg.read_idle_seconds == 12.0
    # Cached: the same object is returned on the next call.
    assert handler._v3_pipeline_config() is cfg


# ---------------------------------------------------------------------------
# AC-8.1: startup switch audit (P0-8)
# ---------------------------------------------------------------------------


def test_startup_audit_line_has_all_five_fields():
    """AC-8.1: the boot line carries operator/timestamp/reason/version/source."""
    from zhongzhuan.config.config import ResponsesBridgeConfig
    from zhongzhuan.proxy.feature_flags import AUDIT_FIELDS, ResponsesFeatureFlags

    flags = ResponsesFeatureFlags(ResponsesBridgeConfig(enabled=True), environ={})
    record = flags.audit_record()
    assert set(record) == set(AUDIT_FIELDS)
    assert record["operator"] == "startup"
    assert record["reason"] == "boot"
    assert record["effective_version"] == "v3"
    assert record["source"] == "config:responses_bridge.enabled"
    assert record["timestamp"].endswith("Z")


def test_startup_audit_reports_v2_emergency_and_env_source():
    """The env hard override is named explicitly so ops can find the lever."""
    from zhongzhuan.proxy.feature_flags import ResponsesFeatureFlags

    flags = ResponsesFeatureFlags(None, environ={"ZHONGZHUAN_RESPONSES_BRIDGE_V3": "0"})
    record = flags.audit_record(reason="incident rollback", operator="oncall")
    assert record["effective_version"] == "v2_emergency"
    assert record["source"] == "env:ZHONGZHUAN_RESPONSES_BRIDGE_V3"
    assert record["reason"] == "incident rollback"
    assert record["operator"] == "oncall"


def test_server_startup_writes_the_audit_line():
    """AC-8.1: ``ProxyServer`` emits the audit through its own logger hook."""
    from zhongzhuan.config.config import ResponsesBridgeConfig

    lines: list[str] = []

    class _Recorder:
        def info(self, message: str) -> None:
            lines.append(str(message))

    proxy = ProxyServer(
        upstream_clients={},
        api_key="sk-x",
        responses_bridge=ResponsesBridgeConfig(enabled=True),
    )
    record = proxy._audit_startup(logger=_Recorder())
    assert record is not None
    assert proxy.startup_audit == record
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("[v3-switch] ")
    for field in ("operator=startup", "reason=boot", "effective_version=v3"):
        assert field in line, line


def test_startup_audit_logging_stays_bounded():
    """Startup logging must stay bounded — a verbose dump deadlocks boot.

    Regression guard: an earlier revision had ``_audit_startup`` also call
    ``log_effective_config``, which emits one line per config leaf.  A
    supervisor that starts the proxy with ``stdout=PIPE, stderr=PIPE`` and
    only drains the pipes *after* the health probe (exactly what
    ``tests/test_lifecycle.py`` does) then filled the OS pipe buffer, the
    child blocked mid-write, and the port was never bound.  The audit is
    one line; the verbose dump belongs to whoever explicitly asks for it.
    """
    from zhongzhuan.config.config import ResponsesBridgeConfig

    lines: list[str] = []

    class _Recorder:
        def info(self, message: str) -> None:
            lines.append(str(message))

    proxy = ProxyServer(
        upstream_clients={},
        api_key="sk-x",
        responses_bridge=ResponsesBridgeConfig(enabled=True),
        store=None,
    )
    proxy._audit_startup(logger=_Recorder())
    assert len(lines) == 1, f"startup must log exactly one line, got {len(lines)}"
    assert lines[0].startswith("[v3-switch] ")


def test_effective_config_dump_carries_the_v3_switch_section():
    """T04: the v3 switch verdict is visible in an explicit config dump.

    The section lives in the rendered output rather than in
    ``effective_config_snapshot`` because it is a *derived* value (env hard
    override folded over ``responses_bridge.enabled``), not a config leaf.
    """
    from zhongzhuan.config.config import Config
    from zhongzhuan.config.effective import format_effective_config

    rendered = "\n".join(format_effective_config(Config()))
    assert "responses_v3.enabled" in rendered
    assert "responses_v3.effective_version" in rendered
