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


@pytest.mark.asyncio
async def test_usage_stats_filters_unconfigured_probe_models(store, monkeypatch):
    """仪表盘模型分布必须过滤掉未配置的探测模型（如 'x'），不误伤正常模型。"""
    from zhongzhuan.store.models import create_model, Model

    now = Store.now()
    await create_model(store, Model(name="mf", upstream_base="http://x", upstream_model="mf"))
    await create_model(store, Model(name="agnes", upstream_base="http://x", upstream_model="agnes"))

    # 正常模型请求（resolved_model_id 为空是生产常态——代理不写该列）
    for ts, model, rid in [
        (now - 3600, "mf", "r1"),
        (now - 3600, "agnes", "r2"),
        (now - 3600, "x", "r3"),  # 部署探测的脏数据
    ]:
        await store.execute(
            "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id, "
            "tokens_in, tokens_out, cost) VALUES(?,?,?,?,?,?,?,?)",
            (ts, model, 200, 10, rid, 10, 20, 0.1),
        )

    result = await get_usage_stats(store, days=7)
    names = [m["model_name"] for m in result["by_model"]]
    assert "mf" in names
    assert "agnes" in names
    assert "x" not in names
    # totals 是全部成功请求（不受展示过滤影响）
    assert result["totals"]["requests"] == 3


@pytest.mark.asyncio
async def test_usage_stats_by_model_strips_upstream_suffix_and_merges(store, monkeypatch):
    """仪表盘模型分布必须按**客户端模型**聚合，不被 ``(上游)`` 后缀分裂。

    2026-08-17 线上回归：``request_logs.model_name`` 写入格式是
    ``juhe/mimo-v2.5-pro(vercel/mimo-v2.5-pro)``（客户端+上游拼接），
    原 ``get_usage_stats`` 直接用整串与 ``configured_names`` 比，导致
    全部行被过滤掉，仪表盘模型分布图渲染成空环（用户报）。

    修复：取 ``(`` 前的客户端名 → 按客户端聚合（跨上游合并 requests）→
    再做 configured 过滤。model_name 字段输出也去括号。
    """
    from zhongzhuan.store.models import create_model, Model

    now = Store.now()
    await create_model(store, Model(name="juhe/mimo", upstream_base="http://x", upstream_model="mimo"))
    await create_model(store, Model(name="juhe/ds", upstream_base="http://x", upstream_model="ds"))

    # 同一客户端模型跨 3 个上游 + 一个无上游直写 → 应合并为 1 条
    rows = [
        (now - 3600, "juhe/mimo(vercel/mimo)", "r1", 100, 50),
        (now - 3600, "juhe/mimo(zz-slo/mimo)", "r2", 80, 40),
        (now - 3600, "juhe/mimo(de5/mimo)", "r3", 60, 30),
        (now - 3600, "juhe/ds(amd/ds)", "r4", 200, 100),
        (now - 3600, "juhe/mimo", "r5", 40, 20),  # 无上游后缀的也归入
    ]
    for ts, model, rid, tin, tout in rows:
        await store.execute(
            "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id, "
            "tokens_in, tokens_out, cost) VALUES(?,?,?,?,?,?,?,?)",
            (ts, model, 200, 10, rid, tin, tout, 0.0),
        )

    result = await get_usage_stats(store, days=7)
    by_model = {m["model_name"]: m for m in result["by_model"]}

    # 跨上游合并：juhe/mimo 共 4 次请求，token 累加
    assert "juhe/mimo" in by_model
    mimo = by_model["juhe/mimo"]
    assert mimo["requests"] == 4
    assert mimo["tokens_in"] == 100 + 80 + 60 + 40
    assert mimo["tokens_out"] == 50 + 40 + 30 + 20
    # model_name 输出不带括号
    assert "(" not in mimo["model_name"]
    # juhe/ds 独立
    assert by_model["juhe/ds"]["requests"] == 1
    assert by_model["juhe/ds"]["tokens_in"] == 200
    # 未配置的探测数据全被剔（如 'x'）
    assert "x" not in by_model
    # totals 不受展示过滤影响
    assert result["totals"]["requests"] == 5


@pytest.mark.asyncio
async def test_token_create_stores_cipher_and_reveal_roundtrip(store):
    """新建令牌必须同时保存哈希与可解密密文，复制接口返回完整 Key。"""
    from zhongzhuan.store.access_tokens import create_token, reveal_token, list_tokens

    t = await create_token(store, label="copy-test")
    assert t.token.startswith("zz-")

    # 列表保持脱敏：token 字段是掩码，绝不出现完整 Key
    listed = await list_tokens(store)
    assert len(listed) == 1
    assert listed[0]["token"] == listed[0]["token_masked"]
    assert "***" in listed[0]["token"]
    assert t.token not in listed[0]["token"]
    assert all(t.token not in str(v) for v in listed[0].values())

    # 复制接口（reveal）能取回完整 Key
    plain = await reveal_token(store, t.id)
    assert plain == t.token


@pytest.mark.asyncio
async def test_legacy_token_without_cipher_reveals_none(store):
    """v010 之前创建的令牌无密文，reveal 返回 None（不可恢复），不换 Key 不废止。"""
    from zhongzhuan.store.access_tokens import create_token, reveal_token

    t = await create_token(store, label="legacy")
    # 模拟 v010 之前的令牌：清空密文列，只留哈希
    await store.execute("UPDATE access_tokens SET token_cipher=NULL WHERE id=?", (t.id,))
    assert await reveal_token(store, t.id) is None
    # 令牌仍然有效
    from zhongzhuan.store.access_tokens import get_token_by_value

    at = await get_token_by_value(store, t.token)
    assert at is not None and at.id == t.id


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
                assert 'id="refreshFallbackBtn"' in text
                assert 'btn.textContent = "刷新中..."' in text
                assert "btn.disabled = true" in text
                assert "btn.disabled = false" in text
                assert "刚刚同步" in text
                # 令牌复制：按钮按 id 调用 reveal 接口（不再把掩码当 Key 复制）
                assert 'onclick="copyToken(${t.id})"' in text
                assert 'api("/api/tokens/" + id + "/reveal"' in text
                assert 'document.execCommand("copy")' in text
                # 新建令牌弹窗：新布局包含完整 Key 一次性展示与复制按钮
                assert "创建访问令牌" in text
                assert 'id="newTokenVal"' in text
                assert "复制 Key" in text
                assert "onclick=\"copyText(document.getElementById('newTokenVal').textContent)\"" in text
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# 分组测试（2026-08-15 新增：POST /api/groups/{id}/test 测试分组内所有启用 Key）
# ---------------------------------------------------------------------------


async def _make_group_fixture(store):
    """建 2 个模型 + 1 个分组（含 2 个成员 + 各 1 个 key）。返回 (model1, model2, group)。"""
    from zhongzhuan.store.models import Model, create_model
    from zhongzhuan.store.keys import ApiKey, create_key
    from zhongzhuan.store.groups import GroupData, GroupMemberData, create_group, set_group_members

    m1 = await create_model(store, Model(name="am/t1", upstream_base="http://up1.example/v1",
                                         upstream_model="t1", protocol="openai"))
    m2 = await create_model(store, Model(name="bz/t2", upstream_base="http://up2.example/v1",
                                         upstream_model="t2", protocol="openai"))
    for m in (m1, m2):
        await create_key(store, ApiKey(id=None, model_id=m.id, label="k", key_value="sk-test123", enabled=1))
    g = await create_group(store, GroupData(name="grp-test", strategy="failover"))
    await set_group_members(store, g.id, [
        GroupMemberData(group_id=g.id, model_id=m1.id, weight=1, ord=0),
        GroupMemberData(group_id=g.id, model_id=m2.id, weight=1, ord=1),
    ])
    return m1, m2, g


@pytest.mark.asyncio
async def test_group_test_endpoint_runs_all_member_keys(store, monkeypatch):
    from zhongzhuan.admin import api_groups

    m1, m2, g = await _make_group_fixture(store)
    key_ids = [r[0] for r in await store.fetchall("SELECT id FROM api_keys ORDER BY id")]

    # 打桩：不发真实网络请求，直接返回可控结果
    async def fake_test(ctx, key_id, model):
        ok = key_id == key_ids[0]
        return {"key_id": key_id, "ok": ok, "status": 200 if ok else 503,
                "latency_ms": 5, "url": "http://stub", "model": model.name, "error": "" if ok else "boom"}

    monkeypatch.setattr(api_groups, "_test_group_key", fake_test)

    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(f"http://127.0.0.1:{port}/api/groups/{g.id}/test") as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body["ok"] is True
                assert body["group"] == "grp-test"
                assert len(body["models"]) == 2
                # 每模型 1 个 key，结果按 ord 排序
                assert body["models"][0]["name"] == "am/t1"
                assert body["models"][0]["keys"][0]["ok"] is True
                assert body["models"][1]["name"] == "bz/t2"
                assert body["models"][1]["keys"][0]["ok"] is False
                assert body["summary"] == {"total_keys": 2, "ok": 1, "fail": 1}
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_group_test_endpoint_unknown_group(store):
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post("http://127.0.0.1:{0}/api/groups/999999/test".format(port)) as resp:
                body = await resp.json()
                assert resp.status == 404
                assert body["ok"] is False
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_group_test_endpoint_empty_group(store):
    from zhongzhuan.store.groups import GroupData, create_group

    g = await create_group(store, GroupData(name="grp-empty", strategy="failover"))
    admin = AdminServer(store=store)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(f"http://127.0.0.1:{port}/api/groups/{g.id}/test") as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body["ok"] is True
                assert body["summary"] == {"total_keys": 0, "ok": 0, "fail": 0}
    finally:
        await runner.cleanup()
