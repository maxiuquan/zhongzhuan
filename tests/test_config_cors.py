"""T32 判据③ — CORS allowlist 内/外来源各 1 例（R-P2-01）。"""
from __future__ import annotations

import pytest
from aiohttp import web

from zhongzhuan.proxy.cors import make_cors_middleware


def _build_app(allow_origins=None) -> web.Application:
    app = web.Application(middlewares=[make_cors_middleware(allow_origins)])

    async def hello(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/v1/models", hello)
    return app


async def _request(method: str, origin: str | None, app: web.Application):
    from aiohttp import ClientSession

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession() as sess:
            headers = {"Origin": origin} if origin else {}
            async with sess.request(method, f"{base}/v1/models", headers=headers) as resp:
                acao = resp.headers.get("Access-Control-Allow-Origin")
                body = await resp.text()
                return resp.status, acao, body
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_allowlisted_origin_gets_headers():
    """判据③：allowlist 内来源 → 回显 Origin。"""
    app = _build_app(allow_origins=["http://a.example", "http://b.example"])
    status, acao, _ = await _request("GET", "http://a.example", app)
    assert status == 200
    assert acao == "http://a.example"


@pytest.mark.asyncio
async def test_outside_origin_denied():
    """判据③：allowlist 外来源 → 无 ACAO 头，浏览器拒绝跨域。"""
    app = _build_app(allow_origins=["http://a.example"])
    status, acao, _ = await _request("GET", "http://evil.example", app)
    assert status == 200
    assert acao is None


@pytest.mark.asyncio
async def test_empty_allowlist_denies_all():
    """生产模式空 allowlist 拒绝跨域。"""
    app = _build_app(allow_origins=[])
    _, acao, _ = await _request("GET", "http://a.example", app)
    assert acao is None


@pytest.mark.asyncio
async def test_wildcard_allows_any():
    app = _build_app(allow_origins=["*"])
    _, acao, _ = await _request("GET", "http://a.example", app)
    assert acao == "*"


@pytest.mark.asyncio
async def test_no_origin_gets_no_cors_headers():
    """非浏览器请求（无 Origin）不加 CORS 头。"""
    app = _build_app(allow_origins=["http://a.example"])
    _, acao, _ = await _request("GET", None, app)
    assert acao is None


@pytest.mark.asyncio
async def test_preflight_allowed_origin_204():
    app = _build_app(allow_origins=["http://a.example"])
    status, acao, _ = await _request("OPTIONS", "http://a.example", app)
    assert status == 204
    assert acao == "http://a.example"


@pytest.mark.asyncio
async def test_preflight_disallowed_origin_403():
    app = _build_app(allow_origins=["http://a.example"])
    status, acao, _ = await _request("OPTIONS", "http://evil.example", app)
    assert status == 403
    assert acao is None


@pytest.mark.asyncio
async def test_env_var_allowlist(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_CORS_ALLOW_ORIGINS", "http://x.example, http://y.example")
    app = _build_app()  # allow_origins=None → 读 env
    _, acao, _ = await _request("GET", "http://x.example", app)
    assert acao == "http://x.example"
    _, acao2, _ = await _request("GET", "http://z.example", app)
    assert acao2 is None
