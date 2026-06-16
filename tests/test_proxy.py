"""Proxy pass-through tests."""
import socket

import pytest
import pytest_asyncio
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient


@pytest_asyncio.fixture
async def mock_upstream():
    async def handler(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer sk-1":
            return web.Response(status=401, text="bad auth")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


@pytest.mark.asyncio
async def test_proxy_passes_through_chat_completions(mock_upstream: str):
    upstream = UpstreamClient(base_url=mock_upstream, timeout=5.0)
    await upstream.start()
    proxy = ProxyServer(upstream_clients={mock_upstream: upstream}, api_key="sk-1", proxy_timeout=5.0)
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
                data='{"model":"x","messages":[]}',
            ) as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body == {"ok": True}
    finally:
        await runner.cleanup()
        await upstream.close()


@pytest.mark.asyncio
async def test_healthz(mock_upstream: str):
    upstream = UpstreamClient(base_url=mock_upstream, timeout=5.0)
    await upstream.start()
    proxy = ProxyServer(upstream_clients={mock_upstream: upstream}, api_key="sk-1", proxy_timeout=5.0)
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/healthz") as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()
        await upstream.close()