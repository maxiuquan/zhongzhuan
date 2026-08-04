"""T33 判据①② — 分层健康检查 + 响应体脱敏（R-P2-07 / R-P2-08）。

判据①：迁移未完成时 readiness 返回 503；三层（liveness / readiness /
dependency）各断言字段。
判据②：响应体正则断言无 URL / 密钥模式（R-P2-08）。

同时覆盖判据③的 `/metrics` 可被 Prometheus 抓取（R-P2-09 前半）。
"""

from __future__ import annotations

import re
import socket

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.proxy.server import ProxyServer
from zhongzhuan.upstream import UpstreamClient

from zhongzhuan.observability.health import (
    build_dependency_status,
    build_liveness,
    build_readiness,
    dependency_item,
    find_leaks,
    migration_status,
    sanitize_health_payload,
    sanitize_health_text,
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _start_proxy(store=None, *, with_keys: bool = True):
    """启动一个 ProxyServer，返回 (base_url, runner, upstream)。"""
    import asyncio

    from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow

    upstream = UpstreamClient(base_url="http://127.0.0.1:19999", timeout=5.0)
    await upstream.start()
    keys = []
    if with_keys:
        keys = [
            KeyHealth(
                key_id=1,
                api_key="sk-test",
                window=SlidingWindow(60, 1000),
                upstream_base="http://127.0.0.1:19999",
            )
        ]
    proxy = ProxyServer(
        upstream_clients={"http://127.0.0.1:19999": upstream},
        keys=keys,
        proxy_timeout=5.0,
        store=store,
    )
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    return base, runner, upstream


# ---------------------------------------------------------------------------
# 判据① 三层字段断言
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveness_fields_and_status():
    """liveness：进程活着就 200，字段 = status/component/timestamp。"""
    base, runner, upstream = await _start_proxy(store=None)
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/healthz/live") as resp:
                assert resp.status == 200
                body = await resp.json()
        assert set(body) == {"status", "component", "timestamp"}
        assert body["status"] == "ok"
        assert body["component"] == "liveness"
        assert isinstance(body["timestamp"], int)
    finally:
        await runner.cleanup()
        await upstream.close()


@pytest.mark.asyncio
async def test_readiness_fields_and_503_when_migration_incomplete(store):
    """readiness：迁移未完成（无 store）→ 503；字段含 status/checks 三层子项。"""
    base, runner, upstream = await _start_proxy(store=None)
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/healthz/ready") as resp:
                assert resp.status == 503
                body = await resp.json()
    finally:
        await runner.cleanup()
        await upstream.close()
    assert body["status"] == "not_ready"
    assert set(body) == {"status", "checks"}
    assert set(body["checks"]) == {"migration", "routes", "worker"}
    assert body["checks"]["migration"]["ok"] is False


@pytest.mark.asyncio
async def test_readiness_200_when_all_dependencies_ready(store):
    """readiness：store 已迁移 + 路由可用 + worker 启动 → 200。"""
    base, runner, upstream = await _start_proxy(store=store)
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/healthz/ready") as resp:
                assert resp.status == 200
                body = await resp.json()
    finally:
        await runner.cleanup()
        await upstream.close()
    assert body["status"] == "ready"
    assert body["checks"]["migration"]["ok"] is True
    assert body["checks"]["routes"]["ok"] is True


@pytest.mark.asyncio
async def test_dependency_fields_each_item(store):
    """dependency status：dependencies 逐项 {name/status/detail}。"""
    base, runner, upstream = await _start_proxy(store=store)
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/healthz/deps") as resp:
                assert resp.status == 200
                body = await resp.json()
    finally:
        await runner.cleanup()
        await upstream.close()
    assert set(body) == {"status", "dependencies"}
    assert body["status"] in ("ok", "degraded")
    assert body["dependencies"]
    for item in body["dependencies"]:
        assert set(item) == {"name", "status", "detail"}
        assert item["name"] in ("store", "upstream", "tool_executor")
        assert item["status"] in ("ok", "down", "optional_unavailable")


@pytest.mark.asyncio
async def test_legacy_healthz_returns_200():
    """旧 `/healthz` 端点保持 200（兼容现有探活）。"""
    base, runner, upstream = await _start_proxy(store=None)
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/healthz") as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()
        await upstream.close()


# ---------------------------------------------------------------------------
# 判据① 单元：迁移状态判定
# ---------------------------------------------------------------------------


class _FakeStore:
    """可注入行的假 store，用于直接测 migration_status。"""

    def __init__(self, rows=None, *, error=False):
        self._rows = rows
        self._error = error

    async def fetchall(self, sql, params=None):
        if self._error:
            raise RuntimeError("db down")
        return self._rows


@pytest.mark.asyncio
async def test_migration_status_incomplete_returns_false():
    """schema_migrations 只有部分版本 → 未完成。"""
    ok, detail = await migration_status(_FakeStore(rows=[(1,), (3,)]))
    assert ok is False
    assert "migration incomplete" in detail


@pytest.mark.asyncio
async def test_migration_status_complete_returns_true():
    """schema_migrations 达到注册表最大版本 → 完成。"""
    from zhongzhuan.store.migrations import MIGRATIONS

    top = max(m.version for m in MIGRATIONS)
    rows = [(v,) for v in (1, 3, 4, 5, 6, top)]
    ok, detail = await migration_status(_FakeStore(rows=rows))
    assert ok is True
    assert "complete" in detail


@pytest.mark.asyncio
async def test_migration_status_no_store_or_error():
    """store=None 或查询异常 → 未就绪（绝不抛异常）。"""
    ok, detail = await migration_status(None)
    assert ok is False
    assert "store unavailable" in detail
    ok2, detail2 = await migration_status(_FakeStore(error=True))
    assert ok2 is False


# ---------------------------------------------------------------------------
# 判据② 响应体正则：无 URL / 密钥模式
# ---------------------------------------------------------------------------


def test_find_leaks_detects_url_and_keys():
    """find_leaks 能抓到 URL / sk-key / api_key 模式。"""
    leaks = find_leaks(
        "db at https://internal.example.com:3306/zhongzhuan key sk-proj-AAAAAAAAAA api_key=secretvalue123"
    )
    assert any(l.startswith("url:") for l in leaks)
    assert any(l.startswith("key:") for l in leaks)
    assert any(l.startswith("secret:") for l in leaks)


def test_find_leaks_clean_text_empty():
    assert find_leaks("status ok, migrations complete (v7)") == []


def test_sanitize_health_text_removes_url_and_keys():
    text = sanitize_health_text("upstream https://upstream.example.com/v1 sk-ABC12345 api_key=xyzzz")
    assert "[REDACTED]" in text
    assert find_leaks(text) == []


def test_sanitize_health_payload_recursive():
    payload = {
        "checks": {
            "migration": {"detail": "https://internal.db:4000 schema"},
            "routes": {"detail": "key sk-ABC12345"},
        },
        "dependencies": [{"detail": "api_key=secretvalue1"}],
        "status": "ok",
    }
    clean = sanitize_health_payload(payload)
    assert find_leaks(clean["checks"]["migration"]["detail"]) == []
    assert find_leaks(clean["checks"]["routes"]["detail"]) == []
    assert find_leaks(clean["dependencies"][0]["detail"]) == []


@pytest.mark.asyncio
async def test_health_endpoints_response_has_no_leaks(store):
    """三层端点响应体全文正则断言：零 URL / 密钥模式（R-P2-08）。"""
    base, runner, upstream = await _start_proxy(store=store)
    try:
        async with ClientSession() as sess:
            for path in ("/healthz/live", "/healthz/ready", "/healthz/deps"):
                async with sess.get(f"{base}{path}") as resp:
                    body = await resp.text()
                assert find_leaks(body) == [], f"leak in {path}: {find_leaks(body)}"
    finally:
        await runner.cleanup()
        await upstream.close()


# ---------------------------------------------------------------------------
# 判据③ /metrics 可被 Prometheus 抓取
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_endpoint_prometheus_scrapeable(store):
    """/metrics 返回标准 Prometheus 文本格式（R-P2-09）。"""
    base, runner, upstream = await _start_proxy(store=store)
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/metrics") as resp:
                assert resp.status == 200
                content_type = resp.headers.get("Content-Type", "")
                body = await resp.text()
    finally:
        await runner.cleanup()
        await upstream.close()
    assert "text/plain" in content_type
    assert "# HELP responses_requests_total" in body
    assert "# TYPE responses_requests_total counter" in body
    # 13 个指标全在（T29 保证）。
    from zhongzhuan.observability.metrics import ALL_METRICS

    for metric in ALL_METRICS:
        assert f"# HELP {metric.name}" in body, metric.name
