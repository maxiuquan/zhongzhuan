"""Tests for x-api-key auth in proxy auth middleware."""

import pytest
import pytest_asyncio
from aiohttp import web

from zhongzhuan.proxy.auth import make_proxy_auth_middleware, proxy_auth_enabled


class _FakeStore:
    """Minimal store stub for the auth middleware.

    get_token_by_value() 查询 9 列: id, token, label, enabled, quota_tokens,
    used_tokens, model_whitelist, expires_at, created_at。返回完整元组以兼容。
    """

    def __init__(self, valid_tokens: set[str] | None = None):
        self._valid = valid_tokens or set()
        self._next_id = 1

    async def fetchone(self, query: str, params: tuple = ()):
        token = params[0] if params else ""
        if token in self._valid:
            # (id, token, label, enabled, quota_tokens, used_tokens,
            #  model_whitelist, expires_at, created_at)
            return (self._next_id, token, "test", 1, -1, 0, "", 0, 0)
        return None


@pytest.fixture(autouse=True)
def _enable_proxy_auth(monkeypatch):
    """Force proxy auth on for all tests in this module."""
    monkeypatch.setenv("ZHONGZHUAN_PROXY_AUTH", "true")
    yield
    monkeypatch.delenv("ZHONGZHUAN_PROXY_AUTH", raising=False)


def test_proxy_auth_enabled_reads_env():
    assert proxy_auth_enabled() is True


def test_proxy_auth_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ZHONGZHUAN_PROXY_AUTH", raising=False)
    assert proxy_auth_enabled() is False


@pytest_asyncio.fixture
async def app_with_auth():
    store = _FakeStore(valid_tokens={"sk-valid-token"})

    async def hello(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[make_proxy_auth_middleware(store)])
    # /v1/* endpoints require auth
    app.router.add_post("/v1/chat/completions", hello)
    app.router.add_post("/v1/messages", hello)
    app.router.add_get("/v1/models", hello)
    # Non-/v1 endpoints should NOT require auth
    app.router.add_get("/healthz", hello)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    yield base
    await runner.cleanup()


@pytest.mark.asyncio
async def test_x_api_key_accepted(app_with_auth):
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        async with sess.post(
            f"{app_with_auth}/v1/messages",
            headers={"x-api-key": "sk-valid-token", "Content-Type": "application/json"},
            data='{"model":"m","messages":[]}',
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body == {"ok": True}


@pytest.mark.asyncio
async def test_bearer_token_accepted(app_with_auth):
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        async with sess.post(
            f"{app_with_auth}/v1/chat/completions",
            headers={"Authorization": "Bearer sk-valid-token", "Content-Type": "application/json"},
            data='{"model":"m","messages":[]}',
        ) as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_invalid_token_returns_401(app_with_auth):
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        async with sess.post(
            f"{app_with_auth}/v1/messages",
            headers={"x-api-key": "sk-wrong", "Content-Type": "application/json"},
            data='{"model":"m","messages":[]}',
        ) as resp:
            assert resp.status == 401
            body = await resp.json()
            assert body["error"]["type"] == "unauthorized"


@pytest.mark.asyncio
async def test_missing_token_returns_401(app_with_auth):
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        async with sess.post(
            f"{app_with_auth}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            data='{"model":"m","messages":[]}',
        ) as resp:
            assert resp.status == 401


@pytest.mark.asyncio
async def test_v1_models_no_auth_required(app_with_auth):
    """GET /v1/models is publicly accessible for model discovery."""
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        async with sess.get(f"{app_with_auth}/v1/models") as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_non_v1_path_no_auth_required(app_with_auth):
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        async with sess.get(f"{app_with_auth}/healthz") as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_x_api_key_takes_precedence(app_with_auth):
    """When both headers are present, x-api-key is checked first."""
    from aiohttp import ClientSession

    async with ClientSession() as sess:
        # valid x-api-key, invalid Bearer — should succeed.
        async with sess.post(
            f"{app_with_auth}/v1/messages",
            headers={
                "x-api-key": "sk-valid-token",
                "Authorization": "Bearer sk-wrong",
                "Content-Type": "application/json",
            },
            data='{"model":"m","messages":[]}',
        ) as resp:
            assert resp.status == 200


@pytest.mark.asyncio
async def test_disabled_auth_passes_through(monkeypatch):
    """When ZHONGZHUAN_PROXY_AUTH is not 'true', all requests pass through."""
    monkeypatch.setenv("ZHONGZHUAN_PROXY_AUTH", "false")
    store = _FakeStore(valid_tokens=set())

    async def hello(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[make_proxy_auth_middleware(store)])
    app.router.add_post("/v1/messages", hello)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    from aiohttp import ClientSession

    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/messages",
                headers={"Content-Type": "application/json"},
                data='{"model":"m","messages":[]}',
            ) as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()
