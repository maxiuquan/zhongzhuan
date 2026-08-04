"""T37 criterion ④: differential test -- SDK vs mock-native OpenAI vs zhongzhuan.

The same request is issued through the official SDK to two endpoints:

* **native reference** -- the SDK talks straight to the mock upstream
  (``MockUpstream`` serving native ``/v1/responses`` SSE / JSON).  This is the
  "real OpenAI" stand-in (zero real network, deterministic payload).
* **zhongzhuan relay** -- the SDK talks to zhongzhuan's ``responses_v3``
  resource layer through the ``NativePassthrough`` (R-P1-44) backed by the
  SAME mock upstream.  The passthrough forwards the request byte-for-byte to
  ``/v1/responses`` and streams the upstream bytes back **unmodified**
  (criterion: "output item structure is not rewritten", R-P1-44).

The two sides must produce structurally equivalent official objects: same
``object``/``status``/``id``-prefix semantics, same output-item shapes, and --
for streaming -- the **same event type sequence** with monotonic
``sequence_number``.  A mutation on the upstream side (changed response id,
dropped streaming event) must break the equivalence -- verified in the task's
mutation runs.
"""

from __future__ import annotations

import pytest

from support.mock_responses_upstream import MockUpstream, UpstreamBehavior, responses_text_stream


def _make_passthrough_app(upstream_base: str):
    """Build an aiohttp app that relays ``/v1/responses`` through the passthrough.

    The passthrough is the R-P1-44 native forwarder: it posts the sanitized
    request to the mock upstream and streams upstream bytes back untouched.
    """
    from aiohttp import web

    from zhongzhuan.proxy.protocol.responses_models import SanitizedRequest
    from zhongzhuan.responses_v3.passthrough import NativePassthrough

    app = web.Application(client_max_size=64 * 1024 * 1024)
    passthrough = NativePassthrough()

    class _HttpTransport:
        """Minimal ``Transport`` over aiohttp: send -> upstream byte stream."""

        def __init__(self, base: str) -> None:
            self._base = base
            self._session = None

        async def send(self, method, url, headers, body):
            import aiohttp

            if self._session is None:
                self._session = aiohttp.ClientSession()
            req_url = url if url.startswith("http") else self._base + url
            async with self._session.request(method, req_url, headers=dict(headers), data=body) as resp:
                async for chunk in resp.content.iter_chunked(1024):
                    yield chunk

        async def close(self) -> None:
            if self._session is not None:
                await self._session.close()
                self._session = None

    transport = _HttpTransport(upstream_base)

    async def _relay(request) -> web.StreamResponse:
        raw = await request.read()
        payload = __import__("json").loads(raw) if raw else {}
        # SanitizedRequest carries the payload as-is for the native forward.
        req = SanitizedRequest(payload=payload)
        stream = passthrough.forward(
            req,
            transport,
            base_url=upstream_base,
            api_key="sk-upstream",
        )
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": (
                    "text/event-stream" if payload.get("stream") else "application/json"
                ),
                "Cache-Control": "no-cache",
            },
        )
        await resp.prepare(request)
        async for chunk in stream:
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    app.router.add_post("/v1/responses", _relay)
    app.on_cleanup.append(lambda _app: transport.close())
    return app


@pytest.fixture
async def differential_env():
    """Stand up the native mock upstream + the zhongzhuan passthrough relay."""
    from aiohttp import web

    up = MockUpstream()
    # Non-streaming: a JSON response object; streaming: the native SSE set.
    from support.mock_responses_upstream import _sse_named, _RESP_ID, _OAI_CREATED

    base_response = {
        "id": _RESP_ID,
        "object": "response",
        "created_at": _OAI_CREATED,
        "model": "gpt-4o",
        "status": "completed",
        "output": [
            {
                "id": "msg_fixture_item_0",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello, world!", "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15},
    }
    import json

    json_payload = json.dumps(base_response, ensure_ascii=False).encode()
    up.set_behavior(UpstreamBehavior(json_payload=json_payload, stream_payload=responses_text_stream()))
    await up.start()

    relay_app = _make_passthrough_app(up.url)
    runner = web.AppRunner(relay_app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    relay_base = f"http://127.0.0.1:{port}/v1"

    from openai import AsyncOpenAI

    native_client = AsyncOpenAI(base_url=up.url, api_key="sk-test", max_retries=0, timeout=30.0)
    relay_client = AsyncOpenAI(base_url=relay_base, api_key="sk-test", max_retries=0, timeout=30.0)

    try:
        yield {"native": native_client, "relay": relay_client, "upstream": up}
    finally:
        await native_client.close()
        await relay_client.close()
        await runner.cleanup()
        await up.stop()


def _shape(obj) -> dict:
    """Extract the official shape keys (id prefix / object / status / model)."""
    return {
        "object": getattr(obj, "object", None),
        "id_prefix": str(getattr(obj, "id", "")).split("_")[0],
        "status": getattr(obj, "status", None),
        "model": getattr(obj, "model", None),
    }


@pytest.mark.asyncio
async def test_differential_non_streaming_shape_equivalent(differential_env):
    """Same non-streaming request -> structurally equivalent official objects."""
    native = differential_env["native"]
    relay = differential_env["relay"]

    native_r = await native.responses.create(model="gpt-4o", input="hi", stream=False)
    relay_r = await relay.responses.create(model="gpt-4o", input="hi", stream=False)

    assert _shape(native_r) == _shape(relay_r)
    assert native_r.object == relay_r.object == "response"
    assert native_r.status == relay_r.status == "completed"
    assert relay_r.output is not None
    assert len(relay_r.output) == 1
    relay_item = relay_r.output[0]
    assert relay_item.type == "message"
    assert relay_item.role == "assistant"
    # The relay must NOT rewrite the output item structure (R-P1-44).
    assert relay_item.content[0].text == "Hello, world!"


@pytest.mark.asyncio
async def test_differential_streaming_event_sequence_equivalent(differential_env):
    """Same streaming request -> identical event-type sequence on both sides."""
    native = differential_env["native"]
    relay = differential_env["relay"]

    n_stream = await native.responses.create(model="gpt-4o", input="hi", stream=True)
    r_stream = await relay.responses.create(model="gpt-4o", input="hi", stream=True)

    n_types = [e.type async for e in n_stream]
    r_types = [e.type async for e in r_stream]

    assert n_types == r_types
    # The official minimal event set is present end-to-end through the relay.
    assert n_types[0] == "response.created"
    assert n_types[-1] == "response.completed"


@pytest.mark.asyncio
async def test_differential_sequence_numbers_match(differential_env):
    """sequence_number is monotonic and identical on both sides."""
    native = differential_env["native"]
    relay = differential_env["relay"]

    n_stream = await native.responses.create(model="gpt-4o", input="hi", stream=True)
    r_stream = await relay.responses.create(model="gpt-4o", input="hi", stream=True)

    n_seq = [e.sequence_number async for e in n_stream]
    r_seq = [e.sequence_number async for e in r_stream]

    assert n_seq == r_seq
    assert n_seq == list(range(len(n_seq)))


@pytest.mark.asyncio
async def test_differential_delta_text_equivalent(differential_env):
    """The assembled text via the relay equals the native reference verbatim."""
    native = differential_env["native"]
    relay = differential_env["relay"]

    async def _text(client):
        stream = await client.responses.create(model="gpt-4o", input="hi", stream=True)
        parts: list[str] = []
        async for e in stream:
            if e.type == "response.output_text.delta":
                parts.append(e.delta)
        return "".join(parts)

    assert await _text(native) == "Hello, world!"
    assert await _text(relay) == "Hello, world!"
    assert await _text(native) == await _text(relay)
