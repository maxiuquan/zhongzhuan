"""Tests for RequestContext (T02 / R-P1-63 first half, R-P0-22 prereq).

Verifies the single-parse preamble: body is parsed exactly once, protocol
detection and model extraction behave identically to the old inline logic,
and ``InboundProtocol`` is a ``str`` subclass so legacy string comparisons
keep working.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import make_mocked_request

from zhongzhuan.proxy.auth import make_proxy_auth_middleware
from zhongzhuan.proxy.context import RequestContextBuilder
from zhongzhuan.proxy.protocol.responses_models import InboundProtocol

JSON_BODY = b'{"model": "gpt-4o", "stream": true, "messages": [{"role": "user", "content": "hi"}]}'


def _request(method="POST", path="/v1/chat/completions", body=JSON_BODY, headers=None):
    proto = Mock()
    proto.is_connected = lambda: True
    sr = StreamReader(protocol=proto, limit=2**16)
    if body:
        sr.feed_data(body)
    sr.feed_eof()
    return make_mocked_request(
        method=method,
        path=path,
        headers=headers or {"Content-Type": "application/json"},
        payload=sr,
    )


@pytest.mark.asyncio
async def test_body_parsed_exactly_once():
    """A single request must result in exactly one json.loads call."""
    builder = RequestContextBuilder()
    req = _request()
    with patch("zhongzhuan.proxy.context.json.loads", wraps=__import__("json").loads) as m:
        ctx = await builder.build(req)
    assert ctx.body is not None
    assert ctx.body["model"] == "gpt-4o"
    assert m.call_count == 1


@pytest.mark.asyncio
async def test_auth_and_context_share_one_json_parse():
    """Auth quota checks and the handler context consume one cached body."""
    req = _request(headers={"Authorization": "Bearer access-token"})
    token = Mock(id=17, quota_tokens=100)
    token.check_quota.return_value = (True, "")
    downstream = AsyncMock()

    async def build_context(request):
        ctx = await RequestContextBuilder().build(request)
        downstream(ctx)
        return web.Response(status=204)

    middleware = make_proxy_auth_middleware(Mock())
    with (
        patch("zhongzhuan.proxy.auth.proxy_auth_enabled", return_value=True),
        patch("zhongzhuan.proxy.auth.get_token_by_value", AsyncMock(return_value=token)),
        patch("zhongzhuan.proxy.context.json.loads", wraps=__import__("json").loads) as loads,
    ):
        response = await middleware(req, build_context)

    assert response.status == 204
    assert loads.call_count == 1
    assert req["token_id"] == 17
    ctx = downstream.call_args.args[0]
    assert ctx.body == {
        "model": "gpt-4o",
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.mark.asyncio
async def test_detect_openai():
    ctx = await RequestContextBuilder().build(_request(path="/v1/chat/completions"))
    assert ctx.inbound_protocol == InboundProtocol.OPENAI
    assert ctx.inbound_protocol == "openai"  # str subclass equality
    assert ctx.requested_model == "gpt-4o"
    assert ctx.is_stream is True


@pytest.mark.asyncio
async def test_detect_anthropic_by_path():
    ctx = await RequestContextBuilder().build(
        _request(path="/v1/messages", headers={"x-api-key": "k", "anthropic-version": "2023-06-01"})
    )
    assert ctx.inbound_protocol == "anthropic"


@pytest.mark.asyncio
async def test_detect_anthropic_by_header():
    ctx = await RequestContextBuilder().build(_request(headers={"x-api-key": "secret"}))
    assert ctx.inbound_protocol == "anthropic"


@pytest.mark.asyncio
async def test_detect_responses():
    ctx = await RequestContextBuilder().build(_request(path="/v1/responses"))
    assert ctx.inbound_protocol == "responses"
    assert ctx.inbound_protocol == InboundProtocol.RESPONSES


@pytest.mark.asyncio
async def test_malformed_body_yields_none():
    ctx = await RequestContextBuilder().build(_request(body=b"not actually json{"))
    assert ctx.body is None
    assert ctx.requested_model == ""
    assert ctx.is_stream is False


@pytest.mark.asyncio
async def test_empty_body():
    ctx = await RequestContextBuilder().build(_request(body=b""))
    assert ctx.body is None
    assert ctx.content_length is None


@pytest.mark.asyncio
async def test_non_stream_default():
    ctx = await RequestContextBuilder().build(_request(body=b'{"model": "gpt-4o", "messages": []}'))
    assert ctx.is_stream is False


@pytest.mark.asyncio
async def test_metadata_fields():
    ctx = await RequestContextBuilder().build(_request())
    assert ctx.path == "/v1/chat/completions"
    assert ctx.method == "POST"
    assert ctx.content_length == len(JSON_BODY)
    assert ctx.remote == ""
    # v3 placeholders inert until T12.
    assert ctx.endpoint is None
    assert ctx.v3_enabled is False


@pytest.mark.asyncio
async def test_inbound_protocol_is_str_enum():
    assert InboundProtocol.OPENAI == "openai"
    assert InboundProtocol.ANTHROPIC == "anthropic"
    assert InboundProtocol.RESPONSES == "responses"
    assert isinstance(InboundProtocol.RESPONSES, str)
