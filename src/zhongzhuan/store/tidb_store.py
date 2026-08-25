"""TiDB async store implementation using aiomysql."""

from __future__ import annotations

import asyncio

import aiomysql

from .store import Store
from .migration_engine import MySQLMigrationExecutor, run_migrations_or_exit
from .migrations import MIGRATIONS


class TiDBStore(Store):
    """Async TiDB store using aiomysql connection pool."""

    dialect = "mysql"

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool
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
    ) -> TiDBStore:
        ssl_ctx = None
        if ssl:
            import ssl as _ssl

            ssl_ctx = _ssl.create_default_context()

        pool = await aiomysql.create_pool(
            host=host,
            port=port,
            user=user,
            password=password,
            db=database,
            autocommit=True,
            minsize=pool_size,
            maxsize=pool_size,
            connect_timeout=10,
            ssl=ssl_ctx,
            charset="utf8mb4",
            # TiDB Cloud / MySQL 服务端默认 wait_timeout 会在空闲一段时间后
            # 掐掉连接；池里复用一条已被服务端关闭的连接会直接抛
            # "Lost connection to server during query"。pool_recycle 让
            # aiomysql 在连接存活超过 30 分钟后主动重建，先于服务端掐线。
            pool_recycle=1800,
        )

        # Versioned migrations (R-P0-04 / R-P0-05). A failure refuses to start.
        async with pool.acquire() as conn:
            await run_migrations_or_exit(MySQLMigrationExecutor(conn), MIGRATIONS)

        return cls(pool)

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        conn, owned = await self._acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return cur.lastrowid or 0
        finally:
            if owned:
                self._pool.release(conn)

    async def execute_rowcount(self, sql: str, params: tuple | None = None) -> int:
        """同 :meth:`execute`，但返回受影响行数（``cursor.rowcount``）。

        aiomysql 对未命中任何行的 UPDATE/DELETE 返回 0；负值统一钳到 0，
        调用方只做 ``> 0`` 判断。
        """
        conn, owned = await self._acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                affected = int(cur.rowcount or 0)
                return affected if affected > 0 else 0
        finally:
            if owned:
                self._pool.release(conn)

    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        conn, owned = await self._acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return await cur.fetchone()
        finally:
            if owned:
                self._pool.release(conn)

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        conn, owned = await self._acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return await cur.fetchall()
        finally:
            if owned:
                self._pool.release(conn)

    async def _acquire(self) -> tuple[aiomysql.Connection, bool]:
        """取一条连接：事务进行中复用事务连接，否则从池里取一条新的。

        返回 ``(conn, owned)``；``owned=False`` 表示这条连接属于正在打开的
        :class:`_TiDBTransaction`，调用方**不得**归还池（由事务统一归还）。
        """
        if self._tx_conn is not None:
            return self._tx_conn, False
        return await self._pool.acquire(), True

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

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
    """

    def __init__(self, store: TiDBStore) -> None:
        self._store = store
        self._conn: aiomysql.Connection | None = None
        self._cur: aiomysql.cursor.SSCursor | None = None
        self._locked = False

    async def __aenter__(self):
        # 先拿事务锁再取连接：保证同一 store 上的并发事务段串行进入。
        await self._store._tx_lock.acquire()
        self._locked = True
        try:
            self._conn = await self._store._pool.acquire()
            self._cur = await self._conn.cursor()
            await self._conn.begin()
            self._store._tx_conn = self._conn
        except BaseException:
            self._store._tx_conn = None
            if self._cur is not None:
                await self._cur.close()
                self._cur = None
            if self._conn is not None:
                self._store._pool.release(self._conn)
                self._conn = None
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
                    self._store._pool.release(self._conn)
                    self._conn = None
        finally:
            self._release_lock()
        return False

    def _release_lock(self) -> None:
        if self._locked:
            self._locked = False
            self._store._tx_lock.release()
