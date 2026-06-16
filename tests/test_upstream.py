"""UpstreamClient tests."""
import pytest
import pytest_asyncio
from aiohttp import web

from zhongzhuan.upstream import UpstreamClient


@pytest_asyncio.fixture
async def mock_server():
    async def handler(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        return web.json_response({"auth": auth, "ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.mark.asyncio
async def test_request_passes_authorization(mock_server: str):
    client = UpstreamClient(base_url=mock_server, timeout=5.0)
    await client.start()
    try:
        resp = await client.request(
            "POST", "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
            content=b'{"model":"x"}',
        )
        body = resp.json()
        assert resp.status_code == 200
        assert body["auth"] == "Bearer sk-test"
    finally:
        await client.close()