"""TiDB async store implementation using aiomysql."""
from __future__ import annotations

import aiomysql

from .store import Store
from .schema import MYSQL_SCHEMA


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
        pool_size: int = 5,
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
            minsize=2,
            maxsize=pool_size,
            connect_timeout=10,
            ssl=ssl_ctx,
            charset="utf8mb4",
        )

        # Run schema
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in MYSQL_SCHEMA.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        await cur.execute(stmt)

        return cls(pool)

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params or ())
                return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params or ())
                return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params or ())
                return await cur.fetchall()

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()