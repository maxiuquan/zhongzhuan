"""Abstract Store base class + factory."""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod


class Store(ABC):
    """Cross-platform async storage interface."""

    @abstractmethod
    async def execute(self, sql: str, params: tuple | None = None) -> int:
        """Execute a write statement. Returns lastrowid."""
        ...

    @abstractmethod
    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        ...

    @abstractmethod
    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    @staticmethod
    def now() -> int:
        return int(time.time())


async def create_store(config) -> Store:
    """Factory: create TiDBStore or SqliteStore based on config/env."""
    from loguru import logger

    tidb_host = os.getenv("ZHONGZHUAN_TIDB_HOST", "")

    if config.storage.backend == "tidb" or tidb_host:
        from .tidb_store import TiDBStore
        store = await TiDBStore.create(
            host=tidb_host or os.getenv("ZHONGZHUAN_TIDB_HOST", ""),
            port=int(os.getenv("ZHONGZHUAN_TIDB_PORT", "4000")),
            user=os.getenv("ZHONGZHUAN_TIDB_USER", ""),
            password=os.getenv("ZHONGZHUAN_TIDB_PASSWORD", ""),
            database=os.getenv("ZHONGZHUAN_TIDB_DATABASE", "zhongzhuan"),
            ssl=os.getenv("ZHONGZHUAN_TIDB_SSL", "true") == "true",
            pool_size=int(os.getenv("ZHONGZHUAN_TIDB_POOL_SIZE", "5")),
        )
        logger.info("使用 TiDB Cloud 存储")
        return store

    from .sqlite_store import SqliteStore
    logger.info("使用 SQLite 本地存储")
    return await SqliteStore.create(config.storage.sqlite_db_path)