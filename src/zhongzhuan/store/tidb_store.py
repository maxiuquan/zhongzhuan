"""TiDB async store implementation using aiomysql."""

from __future__ import annotations

import aiomysql

from .store import Store
from .migration_engine import MySQLMigrationExecutor, run_migrations_or_exit
from .migrations import MIGRATIONS


class TiDBStore(Store):
    """Async TiDB store using aiomysql connection pool."""

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool

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
        )

        # Versioned migrations (R-P0-04 / R-P0-05). A failure refuses to start.
        async with pool.acquire() as conn:
            await run_migrations_or_exit(MySQLMigrationExecutor(conn), MIGRATIONS)

        return cls(pool)

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql.replace("?", "%s"), params or ())
                return await cur.fetchall()

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

    def transaction(self):
        """Batch multiple statements into a single commit (R-P1-50)."""
        return _TiDBTransaction(self._pool)


class _TiDBTransaction:
    """Async context manager that batches writes into one commit.

    TiDB's pool is autocommit; we open an explicit transaction and commit it
    once on clean exit.  Any exception rolls back the whole block.
    """

    def __init__(self, pool: aiomysql.Pool) -> None:
        self._pool = pool
        self._active = False

    async def __aenter__(self):
        self._conn = await self._pool.acquire()
        self._cur = await self._conn.cursor()
        await self._conn.begin()
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self._active:
            return False
        self._active = False
        try:
            if exc_type is None:
                await self._conn.commit()
            else:
                await self._conn.rollback()
        finally:
            await self._cur.close()
            self._pool.release(self._conn)
        return False
