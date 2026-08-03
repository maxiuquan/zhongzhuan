"""Tests for IdempotencyStore (T26 / R-P1-47).

判据映射见各测试 docstring：缺失 / 命中 / in_flight / done / conflict / TTL 过期 /
租户隔离 / 空 key。
"""
from __future__ import annotations

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.store.idempotency import (
    DEFAULT_TTL_SECONDS,
    STATE_CONFLICT,
    STATE_DONE,
    STATE_IN_FLIGHT,
    IdempotencyStore,
)
from zhongzhuan.store.store import create_store


async def _make_store(tmp_path):
    cfg = default_config()
    # create_store 工厂读的是 storage.sqlite_db_path（不是 db_path）。
    cfg.storage.backend = "sqlite"
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.tidb = None
    return await create_store(cfg)


@pytest.mark.asyncio
async def test_seen_miss_for_fresh_key(tmp_path):
    """判据（缺失）：全新幂等键 seen() 为 False。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        assert await store.seen("k1", workspace_id="ws") is False
        assert await store.lookup("k1", workspace_id="ws") is None
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_mark_executed_blocks_and_records_response(tmp_path):
    """判据（done 阻断）：mark_executed 后 seen() 为 True，且 lookup 回放 response_id。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        await store.mark_executed("k2", workspace_id="ws", response_id="resp_2")
        assert await store.seen("k2", workspace_id="ws") is True
        rec = await store.lookup("k2", workspace_id="ws")
        assert rec is not None
        assert rec["state"] == STATE_DONE
        assert rec["response_id"] == "resp_2"
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_reserve_blocks_and_duplicate_returns_false(tmp_path):
    """判据（in_flight 阻断）：reserve 占位后 seen() 为 True；再次 reserve 返回 False。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        assert await store.reserve("k3", workspace_id="ws") is True
        assert await store.seen("k3", workspace_id="ws") is True
        # 第二次占位应被拒（键已被占）。
        assert await store.reserve("k3", workspace_id="ws") is False
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_in_flight_then_done_still_blocks(tmp_path):
    """判据（全程阻断）：in_flight -> done 两种状态都阻止二次执行。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        await store.reserve("k4", workspace_id="ws")
        assert await store.seen("k4", workspace_id="ws") is True
        # 执行完毕，翻成 done。
        await store.mark_executed("k4", workspace_id="ws", response_id="resp_4")
        rec = await store.lookup("k4", workspace_id="ws")
        assert rec["state"] == STATE_DONE
        # done 依然阻断。
        assert await store.seen("k4", workspace_id="ws") is True
        assert await store.reserve("k4", workspace_id="ws") is False
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_conflict_state_does_not_block(tmp_path):
    """判据（conflict 非阻断）：同键异体冲突不应被静默当作已执行。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        # conflict 由 T27 消费；这里直接落一条以验证 seen() 语义。
        await s.execute(
            "INSERT OR REPLACE INTO idempotency_records "
            "(workspace_id, idempotency_key, request_digest, response_id, "
            " status_code, state, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("ws", "k5", "digest-b", "resp_other", 0, STATE_CONFLICT, 1000, 0),
        )
        rec = await store.lookup("k5", workspace_id="ws")
        assert rec is not None
        assert rec["state"] == STATE_CONFLICT
        # conflict 不在 BLOCKING_STATES，因此 seen() 为 False（不会误挡新请求）。
        assert await store.seen("k5", workspace_id="ws") is False
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_ttl_expiry_unblocks_after_window(tmp_path):
    """判据（TTL 过期）：过期后 seen()/lookup 视为未占用，可被新请求覆盖。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        base = 1_000_000
        # 窗口 10s。
        await store.mark_executed(
            "k6", workspace_id="ws", response_id="resp_6",
            ttl_seconds=10, now=base,
        )
        assert await store.seen("k6", workspace_id="ws", now=base) is True
        # 跨越过期点。
        assert await store.lookup("k6", workspace_id="ws", now=base + 11) is None
        assert await store.seen("k6", workspace_id="ws", now=base + 11) is False
        # 新请求可再次占位（不被永久挡死）。
        assert await store.reserve("k6", workspace_id="ws", now=base + 11) is True
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_default_ttl_is_positive_window(tmp_path):
    """判据（TTL 默认窗口）：默认 24h，运算结果在窗口内。"""
    assert DEFAULT_TTL_SECONDS == 86400
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        base = 2_000_000
        await store.mark_executed("k7", workspace_id="ws", now=base)
        rec = await store.lookup("k7", workspace_id="ws", now=base)
        assert rec["expires_at"] == base + DEFAULT_TTL_SECONDS
        assert await store.seen("k7", workspace_id="ws", now=base + 100) is True
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_tenant_isolation(tmp_path):
    """判据（租户隔离）：A 租户的键不能挡住 B 租户的同名键。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        await store.reserve("shared", workspace_id="A")
        assert await store.seen("shared", workspace_id="A") is True
        # 不同 workspace 视为不同键。
        assert await store.seen("shared", workspace_id="B") is False
        assert await store.reserve("shared", workspace_id="B") is True
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_empty_key_is_never_blocking(tmp_path):
    """判据（空 key）：没带幂等键的请求本就没有幂等承诺，绝不互相阻断。"""
    s = await _make_store(tmp_path)
    try:
        store = IdempotencyStore(s)
        assert await store.seen("") is False
        assert await store.lookup("") is None
        # reserve 空 key 返回 True（放行，而非当成已占用）。
        assert await store.reserve("") is True
    finally:
        await s.close()
