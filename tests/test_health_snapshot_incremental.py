"""key 健康快照增量写测试（2026-09-19，TiDB 配额事故修复）。

背景
----
``ProxyHandler._health_snapshot_loop`` 原先每 30 秒对 ``self._keys`` 做**无条件
全量** UPSERT：209 把 key × 2880 次/天 ≈ 60.2 万次写/天。TiDB Cloud Starter
免费档仅 50M RU/月，实测 2026-09-01 → 09-14 即耗尽（≈3.75M RU/天），集群被限制
访问、后台登录接口全 500。

修复后只写指纹（``status`` / ``cooldown_until`` / 限额 / 三个计数）发生变化的
key，另每 ``_HEALTH_FULL_SYNC_EVERY`` 个周期强制全量兜底一次。

覆盖判据
--------
* 首轮、路由池变更（reload / 增删 key）→ 全量写
* 稳定期 → **零写**（这是 RU 收益的来源）
* 单个 key 状态迁移 → 只写那一个
* 写失败**不推进**基准 → 下一轮重试（快照不得静默失鲜）
* 周期全量兜底确实按 ``_HEALTH_FULL_SYNC_EVERY`` 触发
"""

import asyncio
from collections import Counter

import pytest

from zhongzhuan.proxy import handler as handler_mod
from zhongzhuan.proxy.handler import ProxyHandler
from zhongzhuan.proxy.ratelimit import (
    STATE_HEALTHY,
    STATE_RATE_LIMITED,
    KeyHealth,
    SlidingWindow,
)
from zhongzhuan.store import key_health as key_health_mod


def _kh(key_id: int) -> KeyHealth:
    return KeyHealth(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        window=SlidingWindow(60, 1000),
        rpm_limit=1000,
    )


def _handler(keys: list[KeyHealth], *, store=None) -> ProxyHandler:
    return ProxyHandler(clients={}, keys=keys, store=store)


def _spy_saves(monkeypatch) -> list[int]:
    """记录每次 ``save_health`` 的 key_id，同时保留真实落库行为。

    handler 内部是 ``from ..store.key_health import save_health``（函数体内导入），
    每次都从模块取属性，因此 patch 模块属性即可生效。
    """
    calls: list[int] = []
    real = key_health_mod.save_health

    async def spy(s, row):
        calls.append(row.key_id)
        await real(s, row)

    monkeypatch.setattr(key_health_mod, "save_health", spy)
    return calls


async def _read_health(store) -> dict[int, tuple]:
    rows = await store.fetchall("SELECT key_id, status, cooldown_until FROM key_health")
    return {r[0]: (r[1], r[2]) for r in rows}


# --------------------------------------------------------------------------- #
# 首轮 / 稳定期：写放大的主体
# --------------------------------------------------------------------------- #


async def test_first_round_writes_every_key(store, monkeypatch):
    """首轮无基准 → 全量写，保证内存态一定落库。"""
    calls = _spy_saves(monkeypatch)
    h = _handler([_kh(1), _kh(2), _kh(3)], store=store)

    written = await h._health_snapshot_once()

    assert written == 3
    assert sorted(calls) == [1, 2, 3]


async def test_stable_pool_is_not_rewritten(store, monkeypatch):
    """状态没变 → 零写。原实现此处固定 3 次写。"""
    calls = _spy_saves(monkeypatch)
    h = _handler([_kh(1), _kh(2), _kh(3)], store=store)
    await h._health_snapshot_once()

    calls.clear()
    written = await h._health_snapshot_once()

    assert written == 0
    assert calls == []


async def test_dummy_key_is_skipped(store, monkeypatch):
    """key_id<=0（env/dummy key）不参与快照。"""
    calls = _spy_saves(monkeypatch)
    h = _handler([_kh(0), _kh(7)], store=store)

    written = await h._health_snapshot_once()

    assert written == 1
    assert calls == [7]


# --------------------------------------------------------------------------- #
# 单 key 迁移：只写变化的那个
# --------------------------------------------------------------------------- #


async def test_only_changed_key_is_rewritten(store, monkeypatch):
    """仅 key 2 的 success_count 变化 → 只写 key 2。"""
    calls = _spy_saves(monkeypatch)
    keys = [_kh(1), _kh(2), _kh(3)]
    h = _handler(keys, store=store)
    await h._health_snapshot_once()

    calls.clear()
    keys[1].success_count += 1
    written = await h._health_snapshot_once()

    assert written == 1
    assert calls == [2]


async def test_cooldown_transition_is_persisted(store, monkeypatch):
    """冷却迁移（status + cooldown_until）必须落库，且内容正确。"""
    _spy_saves(monkeypatch)
    keys = [_kh(1)]
    h = _handler(keys, store=store)
    await h._health_snapshot_once()

    keys[0].status = STATE_RATE_LIMITED
    keys[0].cooldown_until = 1_799_999_999.0
    keys[0].recent_429_count = 3
    written = await h._health_snapshot_once()

    assert written == 1
    assert await _read_health(store) == {1: (STATE_RATE_LIMITED, 1_799_999_999.0)}

    # 再跑一轮：已稳定，不再写。
    assert await h._health_snapshot_once() == 0


async def test_failure_counters_change_is_persisted(store, monkeypatch):
    """失败计数（total_failures）变化触发写，且值正确。"""
    _spy_saves(monkeypatch)
    keys = [_kh(5)]
    h = _handler(keys, store=store)
    await h._health_snapshot_once()

    keys[0].total_failures += 2
    assert await h._health_snapshot_once() == 1

    rows = await store.fetchall("SELECT failure_count FROM key_health WHERE key_id=?", (5,))
    assert rows[0][0] == 2


# --------------------------------------------------------------------------- #
# 路由池变更：基准作废 → 全量重建
# --------------------------------------------------------------------------- #


async def test_reload_keys_triggers_full_rewrite(store, monkeypatch):
    """真实 reload_keys() 替换 self._keys 对象 → 指纹基准作废，全量重写。"""
    calls = _spy_saves(monkeypatch)
    keys = [_kh(1), _kh(2)]
    h = _handler(keys, store=store)
    h._load_keys_fn = lambda: _reload_copy(keys)
    await h._health_snapshot_once()

    calls.clear()
    await h.reload_keys()
    written = await h._health_snapshot_once()

    assert written == 2
    assert sorted(calls) == [1, 2]


async def _reload_copy(keys: list[KeyHealth]) -> list[KeyHealth]:
    """模拟 DB 重载：返回同 key_id 的新对象副本。"""
    return [_kh(k.key_id) for k in keys]


async def test_added_key_triggers_full_rewrite(store, monkeypatch):
    """池子新增 key → 数量变化使基准作废，全量重写。"""
    calls = _spy_saves(monkeypatch)
    keys = [_kh(1), _kh(2)]
    h = _handler(keys, store=store)
    await h._health_snapshot_once()

    calls.clear()
    keys.append(_kh(9))
    written = await h._health_snapshot_once()

    assert written == 3
    assert sorted(calls) == [1, 2, 9]


# --------------------------------------------------------------------------- #
# 失败语义：写失败不得推进基准
# --------------------------------------------------------------------------- #


async def test_failed_write_retries_next_round(store, monkeypatch):
    """写失败的 key 不记录基准 → 下一轮重试；恢复后补写，且不会永久重写。

    这是「增量写」最容易出的错：若在写失败时也推进指纹，该 key 的快照就会
    静默停在旧值上，直到下一次强制全量（默认 30 分钟）才自愈。
    """
    real = key_health_mod.save_health
    attempts: list[int] = []
    failing = {2}

    async def flaky(s, row):
        attempts.append(row.key_id)
        if row.key_id in failing:
            raise RuntimeError("db down")
        await real(s, row)

    monkeypatch.setattr(key_health_mod, "save_health", flaky)
    h = _handler([_kh(1), _kh(2)], store=store)

    # 第 1 轮：key 1 成功、key 2 失败 → 只有 1 个成功落库。
    assert await h._health_snapshot_once() == 1
    assert attempts == [1, 2]

    # 第 2 轮：key 1 已有基准被跳过；key 2 无基准 → 重试（此时 DB 已恢复）。
    attempts.clear()
    failing.clear()
    assert await h._health_snapshot_once() == 1
    assert attempts == [2]

    # 第 3 轮：两者都已有基准 → 零写。失败不会把池子拖进「永久全量重写」。
    attempts.clear()
    assert await h._health_snapshot_once() == 0
    assert attempts == []


async def test_missing_store_is_noop(monkeypatch):
    """无 store（无持久化的代理）→ 直接返回 0，不抛异常。"""
    calls = _spy_saves(monkeypatch)
    h = _handler([_kh(1)], store=None)

    assert await h._health_snapshot_once() == 0
    assert calls == []


# --------------------------------------------------------------------------- #
# 周期全量兜底
# --------------------------------------------------------------------------- #


async def test_loop_runs_periodic_full_sync(store, monkeypatch):
    """循环按 _HEALTH_FULL_SYNC_EVERY 周期全量兜底一次。

    周期 1 全量（基准为空）、周期 2 零写、周期 3 强制全量 → 每个 key 恰写 2 次。
    """
    monkeypatch.setattr(handler_mod, "_HEALTH_SNAPSHOT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(handler_mod, "_HEALTH_FULL_SYNC_EVERY", 3)
    calls = _spy_saves(monkeypatch)

    keys = [_kh(1), _kh(2)]
    h = _handler(keys, store=store)
    h._bg_running = True
    task = asyncio.create_task(h._health_snapshot_loop())
    try:
        for _ in range(400):
            await asyncio.sleep(0.005)
            if len(calls) >= 4:  # 2 个 key × 2 轮全量
                break
    finally:
        h._bg_running = False
        await asyncio.wait_for(task, timeout=2)

    counts = Counter(calls)
    assert counts[1] == 2, f"key 1 期望「首轮 + 兜底轮」两次，实际 {counts[1]}"
    assert counts[2] == 2


async def test_loop_exits_when_bg_running_cleared(store, monkeypatch):
    """_bg_running=False → 循环正常退出（stop_background_tasks 依赖此语义）。"""
    monkeypatch.setattr(handler_mod, "_HEALTH_SNAPSHOT_INTERVAL_SECONDS", 0.01)
    _spy_saves(monkeypatch)
    h = _handler([_kh(1)], store=store)

    h._bg_running = True
    task = asyncio.create_task(h._health_snapshot_loop())
    await asyncio.sleep(0.03)
    h._bg_running = False
    await asyncio.wait_for(task, timeout=2)  # 不超时即退出

    assert task.done()
