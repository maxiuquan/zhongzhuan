"""Multi-key rotation + 429 retry tests."""
import socket
import time

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.upstream import UpstreamClient


@pytest_asyncio.fixture
async def mock_upstream_429():
    state = {"calls_b": 0}

    async def handler(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer sk-a":
            return web.Response(status=429, text="rate limited")
        state["calls_b"] += 1
        return web.json_response({"ok": True, "by": "b"})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", state
    await runner.cleanup()


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


@pytest.mark.asyncio
async def test_proxy_rotates_key_on_429(mock_upstream_429):
    upstream_url, state = mock_upstream_429
    upstream = UpstreamClient(base_url=upstream_url, timeout=5.0)
    await upstream.start()
    keys = [
        KeyHealth(key_id=1, api_key="sk-a", window=SlidingWindow(60, 1000), rpm_limit=1000, upstream_base=upstream_url),
        KeyHealth(key_id=2, api_key="sk-b", window=SlidingWindow(60, 1000), rpm_limit=1000, upstream_base=upstream_url),
    ]
    proxy = ProxyServer(upstream_clients={upstream_url: upstream}, keys=keys, proxy_timeout=5.0)
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                data='{"model":"x"}',
            ) as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body == {"ok": True, "by": "b"}
        assert state["calls_b"] == 1
        assert time.time() < keys[0].cooldown_until
    finally:
        await runner.cleanup()
        await upstream.close()