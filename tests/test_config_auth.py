"""T32 — admin/auth.py 生产 fail-closed、secret 轮换、R-P2-03 接口测试。

判据④（接口层）：production 下管理端鉴权默认开启。
判据⑤（轮换）：轮换后旧 token 在宽限期内仍有效，宽限期外失效。
R-P2-03：无 CSRF token 的写操作 403；连续登录失败触发限速；安全响应头断言。
判据⑦（接口层）：默认配置下 fallback 刷新被拒（未注册）。
"""

from __future__ import annotations

import socket
import time

import jwt as pyjwt
import pytest
import pytest_asyncio
from aiohttp import ClientSession, CookieJar, web

from zhongzhuan.admin import AdminServer
from zhongzhuan.admin.auth import (
    ConfigError,
    auth_enabled,
    create_token,
    init_jwt_secret,
    reset_login_failures,
    verify_token,
)
from zhongzhuan.config import default_config


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _signed(username: str, secret: str, iat: int) -> str:
    return pyjwt.encode(
        {"sub": username, "iat": iat, "exp": iat + 86400},
        secret,
        algorithm="HS256",
    )


@pytest.fixture(autouse=True)
def _clean_login_state():
    reset_login_failures()
    yield
    reset_login_failures()


@pytest_asyncio.fixture
async def prod_admin(store, monkeypatch):
    """生产模式 AdminServer：鉴权开、JWT secret 已配置。"""
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "test-secret-0123456789")
    monkeypatch.setenv("ZHONGZHUAN_ADMIN_AUTH", "true")
    admin = AdminServer(store=store)
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _free_port())
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    yield admin, base, runner
    await runner.cleanup()


# ---------------------------------------------------------------------------
# 判据⑤ secret 轮换：宽限期内旧 token 有效 / 宽限期外失效
# ---------------------------------------------------------------------------


def test_jwt_rotation_old_token_valid_within_grace():
    init_jwt_secret(secret="secret-A")
    old_token = _signed("admin", "secret-A", iat=int(time.time()) - 60)
    init_jwt_secret(secret="secret-B", previous=["secret-A"], grace=3600)
    assert verify_token(old_token) == "admin"
    # 新 secret 签发的新 token 也正常。
    assert verify_token(create_token("admin")) == "admin"


def test_jwt_rotation_old_token_invalid_after_grace():
    init_jwt_secret(secret="secret-A")
    old_token = _signed("admin", "secret-A", iat=int(time.time()) - 7200)
    init_jwt_secret(secret="secret-B", previous=["secret-A"], grace=3600)
    assert verify_token(old_token) is None


def test_jwt_rotation_grace_zero_rejects_immediately():
    init_jwt_secret(secret="secret-A")
    old_token = _signed("admin", "secret-A", iat=int(time.time()) - 1)
    init_jwt_secret(secret="secret-B", previous=["secret-A"], grace=0)
    assert verify_token(old_token) is None


def test_jwt_rotation_prev_list_accepts_comma_string():
    init_jwt_secret(secret="secret-A")
    old_token = _signed("admin", "secret-A", iat=int(time.time()) - 30)
    init_jwt_secret(secret="secret-B", previous="secret-A, secret-C", grace=300)
    assert verify_token(old_token) == "admin"


# ---------------------------------------------------------------------------
# 判据⑤ 生产模式缺 secret → init_jwt_secret fail closed
# ---------------------------------------------------------------------------


def test_init_jwt_secret_fails_closed_in_production(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.delenv("ZHONGZHUAN_JWT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="JWT_SECRET"):
        init_jwt_secret()


def test_init_jwt_secret_dev_generates_random(monkeypatch):
    monkeypatch.delenv("ZHONGZHUAN_ENV", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_JWT_SECRET", raising=False)
    init_jwt_secret()
    assert create_token("admin")


# ---------------------------------------------------------------------------
# 判据④ 生产模式鉴权默认开启（接口层）
# ---------------------------------------------------------------------------


def test_auth_enabled_production_default(monkeypatch):
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.delenv("ZHONGZHUAN_ADMIN_AUTH", raising=False)
    assert auth_enabled() is True


def test_auth_enabled_dev_default(monkeypatch):
    monkeypatch.delenv("ZHONGZHUAN_ENV", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_ADMIN_AUTH", raising=False)
    assert auth_enabled() is False


# ---------------------------------------------------------------------------
# R-P2-03 接口测试：CSRF / 限速 / 安全响应头
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_without_csrf_returns_403(prod_admin):
    """无 CSRF token 的写操作 → 403。"""
    _, base, _ = prod_admin
    async with ClientSession() as sess:
        async with sess.post(f"{base}/api/models", json={"name": "m1"}) as resp:
            assert resp.status == 403
            body = await resp.json()
            assert body["type"] == "csrf_required"


@pytest.mark.asyncio
async def test_write_with_csrf_and_jwt_allowed(prod_admin):
    """携带 CSRF token + 有效 JWT → 写操作放行。"""
    _, base, _ = prod_admin
    token = create_token("admin")
    # unsafe=True：允许 127.0.0.1 这类 IP 主机存 cookie（aiohttp 默认拒绝）。
    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as sess:
        # 先 GET 拿 CSRF cookie（double-submit）。
        async with sess.get(f"{base}/") as resp:
            assert resp.status == 200
        cookies = sess.cookie_jar.filter_cookies(base)
        csrf = cookies.get("zhongzhuan_csrf").value
        assert csrf
        async with sess.post(
            f"{base}/api/models",
            headers={"X-CSRF-Token": csrf, "Authorization": f"Bearer {token}"},
            json={"name": "m1", "upstream_base": "http://x", "upstream_model": "m1"},
        ) as resp:
            assert resp.status == 201


@pytest.mark.asyncio
async def test_login_rate_limit(prod_admin):
    """连续登录失败触发限速 → 429。"""
    _, base, _ = prod_admin
    async with ClientSession() as sess:
        statuses = []
        for _ in range(11):
            async with sess.post(
                f"{base}/api/auth/login",
                json={"username": "x", "password": "wrong"},
            ) as resp:
                statuses.append(resp.status)
                await resp.text()
    assert statuses[:10] == [401] * 10
    assert statuses[10] == 429


@pytest.mark.asyncio
async def test_security_headers_present(prod_admin):
    """安全响应头断言：HSTS / X-Content-Type-Options / X-Frame-Options / CSP。"""
    _, base, _ = prod_admin
    async with ClientSession() as sess:
        async with sess.get(f"{base}/") as resp:
            assert resp.headers.get("Strict-Transport-Security")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "Content-Security-Policy" in resp.headers


# ---------------------------------------------------------------------------
# 判据⑦ 默认配置下 fallback 未注册（接口层：刷新被拒）
# ---------------------------------------------------------------------------


def test_default_fallback_disabled_opt_in():
    assert default_config().fallback.enabled is False


@pytest.mark.asyncio
async def test_fallback_refresh_denied_by_default(store):
    """默认配置 fallback.enabled=False → /api/fallback/refresh 返回 400。"""
    admin = AdminServer(store=store, config=default_config())
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", _free_port())
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as sess:
            async with sess.post(f"http://127.0.0.1:{port}/api/fallback/refresh") as resp:
                assert resp.status == 400
                body = await resp.json()
                assert body["error"]["type"] == "disabled"
    finally:
        await runner.cleanup()
