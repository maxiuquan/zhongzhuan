"""「Key 健康」模块测试（2026-10-02）。

覆盖三件事：

* v018 持久化：``failure_class`` / ``last_failure_at`` 随快照落库、读回对账
  两侧指纹（9 字段）一致，稳定期零写
* ``/api/keys/health-report`` 聚合端点：非 healthy 过滤 + 渠道/模型 join +
  原因中文映射 + 汇总
* proxy 不可达时的优雅降级（ok=false，不白屏）
"""

import socket

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.admin import api_keys as api_keys_mod
from zhongzhuan.proxy import handler as handler_mod
from zhongzhuan.proxy.ratelimit import STATE_ERROR
from zhongzhuan.proxy.handler import _health_fingerprint
from zhongzhuan.store import key_health as kh_mod
from zhongzhuan.store.key_health import KeyHealthRow, load_all_health, row_to_fingerprint, save_health

from tests.test_health_snapshot_incremental import _handler, _kh


# --------------------------------------------------------------------------- #
# v018：失败原因持久化 + 对账指纹对称
# --------------------------------------------------------------------------- #


async def test_failure_reason_persisted_and_reconciled(store, monkeypatch):
    """失败原因随快照落库，读回指纹与内存指纹 ==，稳定期零写。"""
    calls = []
    real = kh_mod.save_health

    async def spy(s, row):
        calls.append(row.key_id)
        await real(s, row)

    monkeypatch.setattr(kh_mod, "save_health", spy)

    k = _kh(11)
    k.status = STATE_ERROR
    k.failure_class = "transient"
    k.last_failure_at = 1727800000.0
    k.total_failures = 3
    h = _handler([k], store=store)

    # 首轮（对账读回，空库 → 全部落库）
    written = await h._health_snapshot_once()
    assert written == 1

    # DB 里的行带原因字段
    rows = await load_all_health(store)
    assert rows[11].failure_class == "transient"
    assert rows[11].last_failure_at == 1727800000.0

    # 读回指纹与内存指纹对称（9 字段）——对账的基础
    assert row_to_fingerprint(rows[11]) == _health_fingerprint(k)

    # 稳定期：对账发现一致 → 零写
    calls.clear()
    assert await h._health_snapshot_once() == 0
    assert calls == []


async def test_load_all_health_carries_failure_reason(store):
    """v018：启动恢复（load_all_health → KeyHealth）消费的字段来源可用。"""
    await save_health(
        store,
        KeyHealthRow(
            key_id=42,
            status="invalid",
            cooldown_until=0.0,
            rpm_limit=0,
            tpm_limit=0,
            success_count=0,
            failure_count=2,
            recent_429_count=0,
            failure_class="permanent",
            last_failure_at=1727800123.0,
        ),
    )
    rows = await load_all_health(store)
    assert rows[42].status == "invalid"
    assert rows[42].failure_class == "permanent"
    assert rows[42].last_failure_at == 1727800123.0


# --------------------------------------------------------------------------- #
# /api/keys/health-report 聚合端点
# --------------------------------------------------------------------------- #


def _make_app(store):
    """绕过登录中间件，直接挂 api_keys.register_routes（ctx 仅需 store）。"""
    from types import SimpleNamespace

    app = web.Application()
    api_keys_mod.register_routes(app, SimpleNamespace(store=store))
    return app


def _fake_health_payload():
    return [
        {"key_id": 300158, "status": "invalid", "failure_class": "permanent",
         "cooldown_remaining": 0.0, "backoff_level": 0, "last_failure_at": 1727800000.0,
         "consecutive_failures": 0, "total_failures": 1},
        {"key_id": 300166, "status": "error", "failure_class": "transient",
         "cooldown_remaining": 12.4, "backoff_level": 1, "last_failure_at": 1727800050.0,
         "consecutive_failures": 1, "total_failures": 2},
        {"key_id": 7, "status": "healthy", "failure_class": "",
         "cooldown_remaining": 0.0, "backoff_level": 0, "last_failure_at": 0.0,
         "consecutive_failures": 0, "total_failures": 0},
    ]


async def _serve(app):
    """起一个临时站点，返回 (base_url, runner)。"""
    runner = web.AppRunner(app)
    await runner.setup()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return f"http://127.0.0.1:{port}", runner


@pytest.mark.asyncio
async def test_health_report_endpoint(store, monkeypatch):
    async def fake_health():
        return _fake_health_payload()

    monkeypatch.setattr(api_keys_mod, "fetch_proxy_key_health", fake_health)
    await store.execute(
        "INSERT INTO models(id, name, upstream_base, upstream_model, enabled, created_at, updated_at)"
        " VALUES(501, 'glm', 'https://api.hcnsec.cn/v1', 'glm-5.2', 1, 0, 0)"
    )
    await store.execute(
        "INSERT INTO api_keys(id, model_id, label, key_cipher, enabled, priority, created_at)"
        " VALUES(300158, 501, 'hcn-key', X'00', 1, 0, 0)"
    )

    base, runner = await _serve(_make_app(store))
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/api/keys/health-report") as resp:
                assert resp.status == 200
                body = await resp.json()
    finally:
        await runner.cleanup()

    assert body["ok"] is True
    assert body["summary"]["total"] == 3
    assert body["summary"]["failed"] == 2
    assert body["summary"]["by_status"] == {"error": 1, "invalid": 1}

    # healthy 的 key 7 不出现；invalid 排在 error 前面
    ids = [it["key_id"] for it in body["items"]]
    assert ids == [300158, 300166]
    first = body["items"][0]
    assert first["channel"] == "api.hcnsec.cn"
    assert first["model"] == "glm-5.2"
    assert first["label"] == "hcn-key"
    assert first["reason"] == "凭据失效（401/403）"
    assert first["last_failure_at"] == 1727800000.0
    # 300166 不在 DB 里（join miss）→ 兜底显示
    second = body["items"][1]
    assert second["channel"] == "(未知)"
    assert second["model"] == "(未知模型)"
    assert second["reason"] == "上游错误（5xx/超时）"


@pytest.mark.asyncio
async def test_health_report_degrades_when_proxy_unreachable(store, monkeypatch):
    async def boom():
        raise ConnectionError("proxy down")

    monkeypatch.setattr(api_keys_mod, "fetch_proxy_key_health", boom)

    base, runner = await _serve(_make_app(store))
    try:
        async with ClientSession() as sess:
            async with sess.get(f"{base}/api/keys/health-report") as resp:
                assert resp.status == 200
                body = await resp.json()
    finally:
        await runner.cleanup()

    assert body["ok"] is False
    assert "不可达" in body["error"]
    assert body["items"] == []
