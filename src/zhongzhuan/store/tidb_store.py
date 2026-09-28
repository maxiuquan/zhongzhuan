"""TiDB async store implementation using aiomysql.

连接生命周期（2026-09-28，TiDB RU 在线税事故）
=================================================

停机对照实验 + 官方文档实锤：

* TiDB Cloud Starter 集群在**约 5 分钟内没有活跃连接时会自动休眠**
  （scale-to-zero，官方 SQLAlchemy 接入文档原文），休眠期间不烧 RU；
* 只要有一条常驻连接在场（哪怕 QPS≈0.033），实测就有 ≈20 RU/s 的
  固定基线在烧 —— 16.8M/月 的账单里 ≈96% 是这笔「在线税」，且完全不
  出现在控制台 SQL Statements 里。

因此本实现的连接策略是「**按需连接，用完即放**」：

* 池**懒建**：首次真正执行 SQL 时才建池（含 migration），启动时 TiDB
  不可达不再杀死进程（降级模式，见 :meth:`ensure_ready`）；
* 池**空闲释放**：连续 ``idle_release_seconds`` 秒没有任何查询且没有
  事务/在途连接时，整池关闭 —— 让集群能进入官方休眠、在线税归零；
  下一次查询自动重建（TCP+TLS 握手 ≈ 百毫秒级，集群休眠唤醒秒级）；
* ``pool_recycle`` 采用官方推荐值 300s：公网链路的中间设备会在空闲
  几分钟后掐掉连接，复用被服务端关闭的连接会抛
  ``Lost connection to server during query``，先于服务端主动重建。
"""

from __future__ import annotations

import asyncio
import os
import time

import aiomysql

from .store import Store
from .migration_engine import MySQLMigrationExecutor, run_migrations_or_exit
from .migrations import MIGRATIONS

#: 官方 SQLAlchemy 接入文档推荐的连接回收周期（秒）：公网端点空闲连接会被
#: 中间设备掐断，300s 主动重建先于服务端掐线。旧值 1800 会让被掐的死连接
#: 在池里躺 25 分钟。
DEFAULT_POOL_RECYCLE_SECONDS: int = 300

#: 空闲释放默认窗口（秒）。取 120s：远小于官方「5 分钟无活跃连接即休眠」
#: 阈值，保证每次活动结束后连接尽快归零、集群尽快休眠。0 = 关闭释放
#: （退回常驻池行为，等价于改版前的 20 RU/s 在线税）。
DEFAULT_IDLE_RELEASE_SECONDS: int = int(os.getenv("ZHONGZHUAN_TIDB_IDLE_RELEASE_SECONDS", "120"))

#: 连接级（可安全重试一次）的 pymysql/MySQL 错误码：server gone away(2006)、
#: 查询中断线(2013)、broken pipe(2055)、连接被杀(1927)。命中即视为
#: 「借到的连接已死 / 池已失效」——语句未及送达或单语句未提交，重建池后
#: 重试一次是安全的（本项目语句均为点查/幂等 upsert）。
_RETRYABLE_ERRNOS = {2006, 2013, 2055, 1927}

#: 连接级错误的文本特征（覆盖 InterfaceError("Not connected")、TLS 握手
#: 失败、connect_timeout 的 TimeoutError 等不带 errno 的形态）。
_RETRYABLE_MARKERS = (
    "lost connection",
    "connection reset",
    "connection refused",
    "server has gone away",
    "not connected",
    "broken pipe",
    "timed out",
    "ssl",
)


class TiDBStore(Store):
    """Async TiDB store using aiomysql connection pool (lazy + idle-released)."""

    dialect = "mysql"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        ssl: bool = True,
        pool_size: int = 20,
        idle_release_seconds: int | None = None,
        pool_recycle_seconds: int = DEFAULT_POOL_RECYCLE_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database
        self._ssl = ssl
        self._pool_size = max(1, int(pool_size))
        self._idle_release_seconds = (
            DEFAULT_IDLE_RELEASE_SECONDS if idle_release_seconds is None else float(idle_release_seconds)
        )
        self._pool_recycle_seconds = int(pool_recycle_seconds)

        self._pool: aiomysql.Pool | None = None
        #: 池的创建/替换/close 决策互斥。查询本身不持此锁（池 acquire 不在锁内）。
        self._pool_lock = asyncio.Lock()
        #: 当前借出（含正在 acquire 中）的连接数。reaper 只在 _busy == 0 时释放。
        self._busy = 0
        #: migration 是否已执行过（只需一次；懒重连不再触发 migration engine）。
        self._migrated = False
        self._closed = False
        #: monotonic 时间戳：最近一次真实 SQL 活动时间。
        self._last_used = time.monotonic()
        self._reaper_task: asyncio.Task | None = None

        # 事务接线：事务打开期间绑定的专用连接（见 _TiDBTransaction）。
        # 非空时 execute / fetchone / fetchall 全部路由到这条连接，
        # 否则语句会从池里另取连接、落在事务外（TiDB 池是 autocommit）。
        self._tx_conn: aiomysql.Connection | None = None
        # 同一 store 实例上串行化事务段：池里每条连接是独立会话，两个并发
        # 事务无法共享同一个 _tx_conn 绑定。当前调用方（分组成员重写、定价
        # upsert、token 轮换）都是短小 CRUD 块，串行化的代价可忽略。
        self._tx_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        ssl: bool = True,
        pool_size: int = 20,
        idle_release_seconds: int | None = None,
        pool_recycle_seconds: int = DEFAULT_POOL_RECYCLE_SECONDS,
    ) -> TiDBStore:
        """构建并**立即建池 + 跑 migration**（连接失败抛异常，供调用方 skip/降级判断）。"""
        store = cls(
            host=host,
            port=port,
            user=user,
            password=password,
            database=database,
            ssl=ssl,
            pool_size=pool_size,
            idle_release_seconds=idle_release_seconds,
            pool_recycle_seconds=pool_recycle_seconds,
        )
        await store.ensure_ready()
        return store

    async def ensure_ready(self) -> None:
        """确保池存在（懒建 + 首建时跑 migration）。连接失败抛原始异常。"""
        async with self._pool_lock:
            if self._pool is not None:
                return
            self._pool = await self._create_pool_locked()

    async def _create_pool_locked(self) -> aiomysql.Pool:
        """真正创建 aiomysql 池（调用方必须已持 _pool_lock）。"""
        ssl_ctx = None
        if self._ssl:
            import ssl as _ssl

            ssl_ctx = _ssl.create_default_context()

        # minsize=1：不再预建 N 条常驻连接（旧实现 minsize=maxsize=pool_size，
        # 启动即拉满 —— 正是「有连接在场就烧 20 RU/s」的直接来源之一）。
        pool = await aiomysql.create_pool(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            db=self._database,
            autocommit=True,
            minsize=1,
            maxsize=self._pool_size,
            connect_timeout=10,
            ssl=ssl_ctx,
            charset="utf8mb4",
            # 官方推荐值（300s）：公网链路空闲连接会被中间设备掐断，
            # 主动重建先于服务端掐线，避免 "Lost connection during query"。
            pool_recycle=self._pool_recycle_seconds,
        )

        # Versioned migrations (R-P0-04 / R-P0-05)。只在**一生一次**的首建时跑：
        # 懒重连不再触发 migration engine（run_migrations_or_exit 在 schema
        # 损坏时会 sys.exit，绝不能在半路请求里被触发）。
        if not self._migrated:
            async with pool.acquire() as conn:
                await run_migrations_or_exit(MySQLMigrationExecutor(conn), MIGRATIONS)
            self._migrated = True

        self._start_reaper()
        return pool

    def _start_reaper(self) -> None:
        """启动空闲释放任务（幂等；仅 idle_release_seconds > 0 时）。"""
        if self._reaper_task is not None or self._closed:
            return
        if self._idle_release_seconds <= 0:
            return
        self._reaper_task = asyncio.create_task(self._idle_reaper(), name="tidb-idle-reaper")

    async def _idle_reaper(self) -> None:
        """空闲 ``idle_release_seconds`` 后整池关闭，让集群能进入官方休眠。

        释放条件（全部满足才动手）：无在途连接（``_busy == 0``）、无事务绑定
        （``_tx_conn is None``）、距最近一次 SQL 活动已超过释放窗口。释放是
        「整池 close」而不是逐条断开：下次查询经 :meth:`_ensure_pool` 重建。
        """
        try:
            while not self._closed:
                # 粒度 = min(释放窗口, 15s)，下限 0.5s：窗口大时不必频繁醒来，
                # 窗口小（单测）时也能及时检查。
                await asyncio.sleep(max(0.5, min(self._idle_release_seconds, 15.0)))
                if self._closed or self._idle_release_seconds <= 0:
                    continue
                idle_for = time.monotonic() - self._last_used
                if idle_for < self._idle_release_seconds:
                    continue
                async with self._pool_lock:
                    pool = self._pool
                    if pool is None or self._busy > 0 or self._tx_conn is not None:
                        continue  # 有活动 / 在途连接 / 事务，本轮不释放
                    # 先摘引用再关池：此后所有 _ensure_pool 走新建路径，
                    # 不会出现「把旧池连接还进新池」的错位。
                    self._pool = None
                try:
                    pool.close()
                    await asyncio.wait_for(pool.wait_closed(), timeout=5.0)
                    from loguru import logger

                    logger.info(
                        f"TiDB pool released after {idle_for:.0f}s idle "
                        "(cluster free to hibernate; next query rebuilds the pool)"
                    )
                except Exception as exc:  # noqa: BLE001 - 释放失败只记录，不熔断
                    from loguru import logger

                    logger.warning(f"TiDB idle pool close failed: {exc}")
        except asyncio.CancelledError:
            return

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        async def _op(conn):
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return cur.lastrowid or 0

        return await self._run_statement(_op)

    async def execute_rowcount(self, sql: str, params: tuple | None = None) -> int:
        """同 :meth:`execute`，但返回受影响行数（``cursor.rowcount``）。

        aiomysql 对未命中任何行的 UPDATE/DELETE 返回 0；负值统一钳到 0，
        调用方只做 ``> 0`` 判断。
        """

        async def _op(conn):
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                affected = int(cur.rowcount or 0)
                return affected if affected > 0 else 0

        return await self._run_statement(_op)

    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        async def _op(conn):
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return await cur.fetchone()

        return await self._run_statement(_op)

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        async def _op(conn):
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return await cur.fetchall()

        return await self._run_statement(_op)

    # ------------------------------------------------------------------
    # 连接级故障的自愈：借到死连接 → 丢池重建 → 重试一次
    # ------------------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """是否为「连接死了/池失效」级别的错误（只对这些重试）。"""
        try:
            import pymysql.err

            if isinstance(exc, pymysql.err.OperationalError):
                code = exc.args[0] if exc.args else None
                if code in _RETRYABLE_ERRNOS:
                    return True
        except ImportError:  # pragma: no cover - aiomysql 必带 pymysql
            pass
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return True
        text = str(exc).lower()
        return any(marker in text for marker in _RETRYABLE_MARKERS)

    async def _run_statement(self, op):
        """执行单语句，对连接级错误自愈：丢弃当前池、重建、重试一次。

        重试的安全前提：失败要么发生在语句发出前（借到被服务端/中间设备
        掐断的死连接——公网链路 340s idle timeout 的常态），要么语句在
        断线中未及提交（autocommit 单语句）。**只重试一次**：第二次失败
        说明是真故障（网络/服务不可用），原样抛给调用方，防雪崩。
        事务期间（``_tx_conn`` 绑定）不走此路径——事务语义不能透明重放。
        """
        try:
            return await self._run_statement_once(op)
        except Exception as exc:  # noqa: BLE001 - 判定后决定是否重试
            if self._tx_conn is not None or not self._is_retryable(exc):
                raise
            from loguru import logger

            logger.warning(
                f"TiDB connection-level error ({type(exc).__name__}: {exc}); "
                "discarding pool and retrying statement once"
            )
            await self._discard_pool()
            return await self._run_statement_once(op)

    async def _run_statement_once(self, op):
        conn, pool = await self._acquire()
        try:
            return await op(conn)
        finally:
            self._release(conn, pool)

    async def _discard_pool(self) -> None:
        """连接级故障后丢弃当前池（下条语句经 ensure/懒建路径重建）。

        旧池 ``close()`` 是标记式的：在途连接归还时才真正断开，不会打断
        并发请求；摘引用在锁内完成，与 reaper 互斥。
        """
        async with self._pool_lock:
            pool = self._pool
            self._pool = None
        if pool is None:
            return
        pool.close()

        async def _reap() -> None:
            try:
                await asyncio.wait_for(pool.wait_closed(), timeout=5.0)
            except Exception:  # noqa: BLE001 - 后台清理，失败不熔断
                pass

        asyncio.create_task(_reap())

    def status(self) -> dict:
        """/healthz 用的**纯内存**状态快照：不发 SQL（SELECT 1 会唤醒休眠
        集群、烧在线税，绝不能放健康检查轮询里）。"""
        idle_for = max(0.0, time.monotonic() - self._last_used)
        return {
            "backend": "tidb",
            "pool_alive": self._pool is not None,
            "busy_connections": self._busy,
            "idle_seconds": round(idle_for, 1),
            "idle_release_seconds": self._idle_release_seconds,
            "pool_recycle_seconds": self._pool_recycle_seconds,
            "migrated": self._migrated,
        }

    async def _acquire(self) -> tuple[aiomysql.Connection, aiomysql.Pool | None]:
        """取一条连接：事务进行中复用事务连接，否则从（必要时新建的）池里取。

        返回 ``(conn, pool)``；``pool is None`` 表示这条连接属于正在打开的
        :class:`_TiDBTransaction`，调用方**不得**归还（由事务统一归还）。
        非 None 的 ``pool`` 必须与归还时使用的是同一个池 —— 懒重连会换池。
        """
        if self._tx_conn is not None:
            return self._tx_conn, None
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await self._create_pool_locked()
            pool = self._pool
        # 同步递增（与锁释放之间无 await）：reaper 在锁内检查 _busy，
        # 因此「拿到 pool 引用」与「_busy += 1」之间没有可被 reaper 插入的窗口。
        self._busy += 1
        try:
            conn = await pool.acquire()
        except BaseException:
            self._busy -= 1
            raise
        self._last_used = time.monotonic()
        return conn, pool

    def _release(self, conn: aiomysql.Connection, pool: aiomysql.Pool | None) -> None:
        """归还连接。``pool is None`` = 事务连接，事务退出时统一归还。"""
        if pool is None:
            return
        self._busy -= 1
        self._last_used = time.monotonic()
        try:
            pool.release(conn)
        except Exception:  # noqa: BLE001 - 归还失败不影响调用方
            pass

    async def close(self) -> None:
        self._closed = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reaper_task = None
        async with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
            await pool.wait_closed()

    def transaction(self):
        """Batch multiple statements into a single commit (R-P1-50)."""
        return _TiDBTransaction(self)


class _TiDBTransaction:
    """Async context manager that batches writes into one commit.

    TiDB's pool is autocommit; we open an explicit transaction and commit it
    once on clean exit.  Any exception rolls back the whole block.

    连接绑定（R 修复）：事务期间把专用连接挂在 ``store._tx_conn`` 上，
    ``execute`` / ``fetchone`` / ``fetchall`` 经由 ``_acquire()`` 全部路由到
    这条连接。否则块内语句会从池里另取连接执行 —— 落在事务外、autocommit
    立即提交，回滚时什么都收不回来，「事务」名存实亡。

    懒池适配：事务连接走 :meth:`TiDBStore._acquire` 同一条路（保证 reaper
    能看到在途事务），并持有**本事务自己的池引用** —— 释放时还回借出时的
    那个池，而不是 store 当前的池（空闲释放可能在事务中途换池）。
    """

    def __init__(self, store: TiDBStore) -> None:
        self._store = store
        self._pool: aiomysql.Pool | None = None
        self._conn: aiomysql.Connection | None = None
        self._cur: aiomysql.cursor.SSCursor | None = None
        self._locked = False

    async def _open_tx_conn(self) -> None:
        """借连接并 BEGIN；连接级失败（死连接/池失效）丢池重建后重试一次。"""
        for attempt in (1, 2):
            try:
                self._conn, self._pool = await self._store._acquire()
                self._cur = await self._conn.cursor()
                await self._conn.begin()
                return
            except Exception as exc:  # noqa: BLE001 - 判定后决定是否重试
                # 本轮借出的连接/游标先清理干净，避免泄漏
                if self._cur is not None:
                    try:
                        await self._cur.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._cur = None
                if self._conn is not None:
                    self._store._release(self._conn, self._pool)
                    self._conn = None
                    self._pool = None
                if attempt == 2 or not self._store._is_retryable(exc):
                    raise
                from loguru import logger

                logger.warning(
                    f"TiDB transaction begin hit connection-level error "
                    f"({type(exc).__name__}: {exc}); discarding pool and retrying once"
                )
                await self._store._discard_pool()

    async def __aenter__(self):
        # 先拿事务锁再取连接：保证同一 store 上的并发事务段串行进入。
        await self._store._tx_lock.acquire()
        self._locked = True
        try:
            await self._open_tx_conn()
            self._store._tx_conn = self._conn
        except BaseException:
            self._store._tx_conn = None
            if self._cur is not None:
                await self._cur.close()
                self._cur = None
            if self._conn is not None:
                self._store._release(self._conn, self._pool)
                self._conn = None
                self._pool = None
            self._release_lock()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self._conn is not None:
                try:
                    if exc_type is None:
                        await self._conn.commit()
                    else:
                        await self._conn.rollback()
                finally:
                    self._store._tx_conn = None
                    if self._cur is not None:
                        await self._cur.close()
                        self._cur = None
                    self._store._release(self._conn, self._pool)
                    self._conn = None
                    self._pool = None
        finally:
            self._release_lock()
        return False

    def _release_lock(self) -> None:
        if self._locked:
            self._locked = False
            self._store._tx_lock.release()
