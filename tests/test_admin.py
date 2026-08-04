"""Admin API tests."""

import os

os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

import socket

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.admin import AdminServer
from zhongzhuan.admin import api_service
from zhongzhuan.store.logs import get_usage_stats
from zhongzhuan.store.store import Store


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.mark.asyncio
async def test_list_models_empty(store):
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/api/models") as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body == {"data": []}
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_create_and_list_models(store):
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/api/models",
                json={"name": "m1", "upstream_base": "http://x", "upstream_model": "m1"},
            ) as resp:
                body = await resp.json()
                assert resp.status == 201
                assert body["name"] == "m1"
            async with sess.get(f"http://127.0.0.1:{port}/api/models") as resp:
                body = await resp.json()
                assert len(body["data"]) == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_create_key_api(store):
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/api/models",
                json={"name": "m1", "upstream_base": "http://x", "upstream_model": "m1"},
            ) as resp:
                m = await resp.json()
            async with sess.post(
                f"http://127.0.0.1:{port}/api/keys",
                json={"model_id": m["id"], "label": "test", "key_value": "sk-test123"},
            ) as resp:
                body = await resp.json()
                assert resp.status == 201
                assert "***" in body["key_masked"]
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_stats_empty(store):
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/api/stats?range=1h") as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body["success_rate"] == 1.0
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_usage_stats_groups_rows_into_utc_days(store, monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr(Store, "now", staticmethod(lambda: now))
    day = 86400
    rows = [
        (now - day + 10, "model-a", 200, 10, "r1", 10, 20, 0.1),
        (now - day + 20, "model-a", 200, 10, "r2", 30, 40, 0.2),
        (now + 10, "model-b", 200, 10, "r3", 50, 60, 0.3),
    ]
    for ts, model, status, latency, rid, tokens_in, tokens_out, cost in rows:
        await store.execute(
            "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id, "
            "tokens_in, tokens_out, cost) VALUES(?,?,?,?,?,?,?,?)",
            (ts, model, status, latency, rid, tokens_in, tokens_out, cost),
        )

    result = await get_usage_stats(store, days=7)

    assert len(result["daily"]) == 2
    assert [item["requests"] for item in result["daily"]] == [2, 1]
    assert result["totals"] == {"requests": 3, "tokens_in": 90, "tokens_out": 120, "cost": 0.6}


def test_usage_stats_mysql_day_bucket_and_decimal_results_are_json_safe():
    from decimal import Decimal

    class RecordingStore:
        dialect = "mysql"

        def __init__(self):
            self.queries = []
            self.fetchall_calls = 0

        async def fetchall(self, sql, params=None):
            self.queries.append(sql)
            self.fetchall_calls += 1
            if self.fetchall_calls == 1:
                return [(Decimal("1999900800"), 2, Decimal("40"), Decimal("60"), Decimal("0.3"))]
            return [("model-a", 2, Decimal("40"), Decimal("60"), Decimal("0.3"))]

        async def fetchone(self, sql, params=None):
            return (2, Decimal("40"), Decimal("60"), Decimal("0.3"))

    async def run():
        store = RecordingStore()
        result = await get_usage_stats(store, days=7)
        return store, result

    import asyncio
    import json

    store, result = asyncio.run(run())
    assert "CAST(FLOOR(ts / 86400) * 86400 AS SIGNED)" in store.queries[0]
    assert result["totals"] == {"requests": 2, "tokens_in": 40, "tokens_out": 60, "cost": 0.3}
    json.dumps(result)


def test_service_status_does_not_call_sc_on_linux(monkeypatch):
    monkeypatch.setattr(api_service.sys, "platform", "linux")
    monkeypatch.setattr(api_service, "_sc", lambda *_args: pytest.fail("sc.exe must not run on Linux"))

    assert api_service._service_status("Zhongzhuan") == {
        "status": "running",
        "control_supported": False,
    }


@pytest.mark.asyncio
async def test_ui_serves(store):
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Zhongzhuan" in text
    finally:
        await runner.cleanup()
