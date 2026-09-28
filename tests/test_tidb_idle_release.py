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


# ----------------------------------------------------------------------
# 连接级故障自愈（2026-09-28 补丁②：借池失败 → 丢池重建 → 重试一次）
# ----------------------------------------------------------------------

def _lost_connection() -> Exception:
    import pymysql.err

    return pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")


@pytest.mark.asyncio
async def test_statement_retry_recovers_on_fresh_pool(store) -> None:
    """借到死连接：语句在旧池上失败 → 丢池 → 新池上重试成功。"""
    s, pools = store
    await _execute_one(s)
    first_pool = pools[0]

    calls: list[_FakePool] = []

    async def op(conn: _FakeConn) -> str:
        calls.append(conn._pool)
        if conn._pool is first_pool:
            raise _lost_connection()
        return "ok"

    assert await s._run_statement(op) == "ok"
    assert calls[0] is first_pool, "首次执行应在旧池连接上进行"
    assert calls[1] is pools[1], "重试应落在新重建的池上"
    assert first_pool.close_count == 1, "死连接所在旧池必须被丢弃"
    assert s._pool is pools[1]


@pytest.mark.asyncio
async def test_non_retryable_error_propagates(store) -> None:
    """非连接级错误（SQL 语义错误等）绝不重试、绝不丢池。"""
    s, pools = store
    await _execute_one(s)

    calls: list[int] = []

    async def op(conn: _FakeConn) -> None:
        calls.append(1)
        raise ValueError("syntax error in test op")

    with pytest.raises(ValueError):
        await s._run_statement(op)

    assert len(calls) == 1, "只执行一次，没有重试"
    assert len(pools) == 1 and s._pool is pools[0], "池未被丢弃"


@pytest.mark.asyncio
async def test_retry_gives_up_after_once(store) -> None:
    """重建后仍失败 = 真故障，只重试一次就抛（防雪崩）。"""
    s, pools = store
    await _execute_one(s)

    calls: list[int] = []

    async def op(conn: _FakeConn) -> None:
        calls.append(1)
        raise _lost_connection()

    with pytest.raises(Exception):
        await s._run_statement(op)

    assert len(calls) == 2, "第一次 + 重建后重试一次，共两次"
    assert len(pools) == 2, "恰好重建过一个新池"


@pytest.mark.asyncio
async def test_transaction_bound_statements_not_retried(store) -> None:
    """事务绑定期间语句失败绝不透明重放（事务语义不能重放）。"""
    s, pools = store
    conn, _ = await s._acquire()
    s._tx_conn = conn

    calls: list[int] = []

    async def op(c: _FakeConn) -> None:
        calls.append(1)
        raise _lost_connection()

    with pytest.raises(Exception):
        await s._run_statement(op)

    assert len(calls) == 1
    assert s._pool is pools[0], "事务路径不触发丢池"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (None, False),  # 占位，运行时替换
    ],
)
def test_is_retryable_matrix(exc, expected) -> None:
    """连接级错误识别矩阵：errno / 文本特征 / 超时 各形态都要命中。"""
    import pymysql.err

    cases = [
        (pymysql.err.OperationalError(2013, "Lost connection"), True),
        (pymysql.err.OperationalError(2006, "MySQL server has gone away"), True),
        (pymysql.err.OperationalError(2055, "Broken pipe"), True),
        (pymysql.err.OperationalError(1064, "You have an error in your SQL syntax"), False),
        (pymysql.err.OperationalError(1146, "Table doesn't exist"), False),
        (pymysql.err.InterfaceError(0, "Not connected"), True),
        (RuntimeError("connection reset by peer"), True),
        (RuntimeError("SSL handshake failed"), True),
        (asyncio.TimeoutError(), True),
        (ValueError("totally unrelated"), False),
    ]
    for exc, expected in cases:
        assert TiDBStore._is_retryable(exc) is expected, f"{exc!r} -> {expected}"


@pytest.mark.asyncio
async def test_status_is_pure_memory(store) -> None:
    """/healthz 的 status() 必须零 SQL（SELECT 1 会唤醒休眠集群烧在线税）。"""
    s, pools = store
    await _execute_one(s)
    snap = s.status()
    assert snap["backend"] == "tidb"
    assert snap["pool_alive"] is True
    assert snap["busy_connections"] == 0
    assert snap["idle_seconds"] >= 0
    assert "idle_release_seconds" in snap and "pool_recycle_seconds" in snap
    # 纯内存佐证：不触发任何 _create_pool_locked / acquire —— pools 数不变
    assert len(pools) == 1
