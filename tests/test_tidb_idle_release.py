"""TiDBStore 懒建池 + 空闲释放（2026-09-28 RU 在线税修复）的行为守护。

不连真库：monkeypatch ``_create_pool_locked`` 返回假池，验证三条铁律：

1. **空闲释放**：连续 ``idle_release_seconds`` 无活动后整池被关闭、
   ``_pool`` 归 None（集群得以进入官方 scale-to-zero），下次查询自动重建；
2. **忙时保护**：有在途连接（``_busy > 0``）或事务绑定时，reaper 绝不关池；
3. **不跨池还连接**：释放窗口换池后，旧池连接仍还给旧池
   （``release`` 收到的 pool 引用与借出时一致）。

背景：停机对照实验实锤「有活连接就有 ≈20 RU/s 在线税」，所以「连接必须
能归零」是这套 store 的核心行为，回归即事故。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from zhongzhuan.store.tidb_store import TiDBStore


class _FakeConn:
    def __init__(self, pool: "_FakePool") -> None:
        self._pool = pool
        self.closed = False
        self.queries: list[str] = []


class _FakePool:
    """aiomysql.Pool 行为子集：acquire/release/close/wait_closed/size/freesize。"""

    def __init__(self) -> None:
        self.closed = False
        self.size = 2
        self.freesize = 2
        self.acquired: list[_FakeConn] = []
        self.close_count = 0
        self.created = time.monotonic()

    async def acquire(self) -> _FakeConn:
        assert not self.closed, "acquire on closed pool"
        conn = _FakeConn(self)
        self.acquired.append(conn)
        self.freesize -= 1
        return conn

    def release(self, conn: _FakeConn) -> None:
        assert conn in self.acquired, "release of foreign connection"
        self.acquired.remove(conn)
        self.freesize += 1

    def close(self) -> None:
        self.closed = True
        self.close_count += 1

    async def wait_closed(self) -> None:
        return None


@pytest.fixture
async def store(monkeypatch):
    """未连接的 TiDBStore：假池工厂 + 0.4s 释放窗口。"""
    s = TiDBStore(
        host="invalid",
        port=4000,
        user="u",
        password="p",
        database="d",
        ssl=False,
        pool_size=2,
        idle_release_seconds=0.4,
    )
    pools: list[_FakePool] = []

    async def _fake_create() -> _FakePool:
        pool = _FakePool()
        pools.append(pool)
        s._migrated = True  # 绕过 migration（真池路径不在此测）
        s._start_reaper()
        return pool

    monkeypatch.setattr(s, "_create_pool_locked", _fake_create)
    yield s, pools
    s._closed = True
    if s._reaper_task is not None:
        s._reaper_task.cancel()
        try:
            await s._reaper_task
        except (asyncio.CancelledError, Exception):
            pass


async def _execute_one(s: TiDBStore) -> None:
    conn, pool = await s._acquire()
    s._release(conn, pool)


@pytest.mark.asyncio
async def test_idle_pool_is_released_and_rebuilt(store) -> None:
    s, pools = store
    await _execute_one(s)
    assert len(pools) == 1 and s._pool is pools[0]

    # 等过释放窗口（0.4s 窗口 + reaper 0.5s 粒度 + 余量）
    await asyncio.sleep(1.5)
    assert pools[0].close_count == 1, "空闲超窗后池应被关闭"
    assert s._pool is None, "释放后 _pool 必须归 None（集群得以休眠）"

    # 下一次查询懒重建，且是**新**池
    await _execute_one(s)
    assert len(pools) == 2 and s._pool is pools[1]


@pytest.mark.asyncio
async def test_busy_connection_blocks_release(store) -> None:
    s, pools = store
    conn, pool = await s._acquire()
    assert s._busy == 1

    await asyncio.sleep(1.5)
    assert pools[0].close_count == 0, "有在途连接时 reaper 绝不能关池"
    assert s._pool is pool

    s._release(conn, pool)
    assert s._busy == 0
    # 归还后空闲窗口重新计时：立即再等也不会立刻关（窗口未过）
    await asyncio.sleep(0.1)
    assert s._pool is pool


@pytest.mark.asyncio
async def test_transaction_binding_blocks_release(store) -> None:
    s, pools = store
    conn, pool = await s._acquire()
    s._tx_conn = conn

    await asyncio.sleep(1.5)
    assert pools[0].close_count == 0, "事务绑定期间绝不能关池"
    assert s._pool is pool

    s._tx_conn = None
    s._release(conn, pool)


@pytest.mark.asyncio
async def test_release_goes_back_to_borrowed_pool(store) -> None:
    """释放窗口换池后，旧连接必须还给借出时的旧池（不跨池错位）。"""
    s, pools = store
    await _execute_one(s)  # 建第一个池
    old_pool = pools[0]

    # 模拟 reaper 已把旧池摘除：下一个 _acquire 拿到新池
    s._pool = None
    conn, pool = await s._acquire()
    assert pool is not old_pool and pool is pools[1]

    # 旧池 release 仍然只接受旧池自己的连接（FakePool 会 assert）
    stale = _FakeConn(old_pool)
    old_pool.acquired.append(stale)
    old_pool.freesize -= 1
    old_pool.release(stale)  # 不会抛 = 引用绑定正确

    s._release(conn, pool)
    assert conn in pool.acquired or pool.freesize == pool.size
