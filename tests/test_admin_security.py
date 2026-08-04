"""T33 判据④ — 管理端安全（R-P2-03）。

**哪些是 T32 已提交覆盖的**（tests/test_config_auth.py）：
* 无 CSRF token 的写操作 403（``test_write_without_csrf_returns_403``）
* 携带 CSRF + JWT 的写操作放行（``test_write_with_csrf_and_jwt_allowed``）
* 连续登录失败触发限速 429（``test_login_rate_limit``）
* 安全响应头 HSTS / X-Content-Type-Options / X-Frame-Options / CSP
  （``test_security_headers_present``）

**本文件 T33 新增**：
* 限速改为**可注入时钟**（``auth.set_login_clock``）——判据④「测试零真实等待」
* 窗口过期后恢复登录（假时钟推进窗口，断言可再次登录）
* HTTP 层：限速命中返回 429 且带 ``Retry-After``
* CSRF / 安全响应头的复验（标注来源）
"""

from __future__ import annotations

import socket
import time

import pytest
import pytest_asyncio
from aiohttp import ClientSession, CookieJar, web

from zhongzhuan.admin import AdminServer
from zhongzhuan.admin.auth import (
    _check_login_rate,
    _record_login_failure,
    create_token,
    init_jwt_secret,
    reset_login_failures,
    set_login_clock,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeClock:
    """单调形状的可注入时钟：``advance()`` 推进时间。"""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _clean_login_state():
    reset_login_failures()
    yield
    reset_login_failures()
    set_login_clock(None)  # 还原真实时钟


@pytest_asyncio.fixture
async def prod_admin(store, monkeypatch):
    """生产模式 AdminServer（同 T32 fixture 形态）。"""
    monkeypatch.setenv("ZHONGZHUAN_ENV", "production")
    monkeypatch.setenv("ZHONGZHUAN_JWT_SECRET", "t33-test-secret-0123456789")
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
# T33 新增：限速可注入时钟（判据④零真实等待）
# ---------------------------------------------------------------------------


def test_login_rate_limit_uses_injectable_clock():
    """限速判定走可注入时钟：同一逻辑下假时钟推进即模拟窗口流逝。"""
    clock = FakeClock()
    set_login_clock(clock)
    req = _FakeLoginRequest("10.0.0.1")
    window = int(300)  # 默认窗口 300s（用环境默认）
    for _ in range(10):
        _record_login_failure(req)
    # 达到上限（max=10）。
    assert _check_login_rate(req) is False
    # 假时钟推进超过窗口 → 判定恢复（不需要真 sleep）。
    clock.advance(window + 1)
    assert _check_login_rate(req) is True


def test_login_rate_limit_partial_window_counts():
    """窗口内的失败次数精确累计；窗口外的不计入。"""
    clock = FakeClock()
    set_login_clock(clock)
    req = _FakeLoginRequest("10.0.0.2")
    for _ in range(5):
        _record_login_failure(req)
    assert _check_login_rate(req) is True  # 5 < 10
    clock.advance(10)  # 窗口内继续失败
    for _ in range(5):
        _record_login_failure(req)
    assert _check_login_rate(req) is False  # 10 次达上限
    clock.advance(400)  # 全部过期
    assert _check_login_rate(req) is True


class _FakeLoginRequest:
    """限速逻辑只读 ``request.remote`` —— 最小假对象即可。"""

    def __init__(self, remote: str) -> None:
        self.remote = remote


@pytest.mark.asyncio
async def test_login_rate_limit_http_429_with_retry_after(prod_admin):
    """HTTP 层：连续失败触限速返回 429 + Retry-After 头。"""
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
        # 第 11 次的响应带 Retry-After。
        async with sess.post(
            f"{base}/api/auth/login",
            json={"username": "x", "password": "wrong"},
        ) as resp:
            assert resp.status == 429
            assert "Retry-After" in resp.headers


# ---------------------------------------------------------------------------
# T33 复验（T32 已测，此处标注来源并复验）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_without_csrf_returns_403(prod_admin):
    """判据④：无 CSRF token 的写操作 403（T32 test_write_without_csrf_returns_403 复验）。"""
    _, base, _ = prod_admin
    async with ClientSession() as sess:
        async with sess.post(f"{base}/api/models", json={"name": "m1"}) as resp:
            assert resp.status == 403
            body = await resp.json()
            assert body["type"] == "csrf_required"


@pytest.mark.asyncio
async def test_security_headers_present(prod_admin):
    """判据④：HSTS / X-Content-Type-Options / X-Frame-Options / CSP 响应头
    （T32 test_security_headers_present 复验）。"""
    _, base, _ = prod_admin
    async with ClientSession() as sess:
        async with sess.get(f"{base}/") as resp:
            assert resp.headers.get("Strict-Transport-Security")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert "Content-Security-Policy" in resp.headers
