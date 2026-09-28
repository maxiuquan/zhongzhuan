"""Abstract Store base class + factory."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod


class Store(ABC):
    """Cross-platform async storage interface."""

    #: Dialect for SQL that differs between backends (``sqlite`` vs ``mysql``).
    dialect: str = "sqlite"

    def upsert_into(self, table: str) -> str:
        """Return a full ``<UPSERT> INTO <table>`` statement prefix.

        SQLite uses ``INSERT OR REPLACE INTO``; MySQL / TiDB use ``REPLACE INTO``.
        Both replace the conflicting row (same upsert semantics).
        """
        return f"INSERT OR REPLACE INTO {table}" if self.dialect == "sqlite" else f"REPLACE INTO {table}"

    def status(self) -> dict:
        """In-memory store health snapshot for /healthz.

        **必须零 SQL**：任何真实查询都会把已休眠的 TiDB 集群唤醒（在线税
        回归），健康检查轮询绝不允许碰库。具体后端覆盖本方法补充池状态。
        """
        return {"backend": self.dialect}

    @abstractmethod
    async def execute(self, sql: str, params: tuple | None = None) -> int:
        """Execute a write statement. Returns lastrowid."""
        ...

    async def execute_rowcount(self, sql: str, params: tuple | None = None) -> int:
        """Execute a write statement and return the **affected row count**.

        :meth:`execute` returns ``cursor.lastrowid``（自增主键），**不是**影响
        行数 —— 把它当 rowcount 用会把「删了 3 行」误判成「删了 47 行」。
        需要「这条写语句到底动了几行」的调用方（DELETE 的存在性判断、CAS 式
        UPDATE 是否抢到、retention 计数）必须走本方法。

        具体后端用 ``cursor.rowcount`` 覆盖本方法；这里提供的默认实现只是为
        了不破坏轻量测试替身（它们直接继承 ``Store`` 且只实现了三个抽象方法，
        其 ``execute`` 恰好已返回受影响行数）而做的尽力委托，负值一律钳到 0。
        生产后端不得依赖该默认路径。
        """
        affected = await self.execute(sql, params)
        return affected if affected > 0 else 0

    @abstractmethod
    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None: ...

    @abstractmethod
    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]: ...

    @abstractmethod
    async def close(self) -> None: ...

    def transaction(self):  # noqa: D401
        """Return an async context manager batching multiple statements in one commit.

        Default implementation is a no-op that does not batch (each ``execute``
        commits on its own).  Backends that support explicit transactions
        (SQLite, TiDB) override this to span a single commit across the block.
        """
        return _NoopTransaction()

    @staticmethod
    def now() -> int:
        return int(time.time())


class _NoopTransaction:
    """Async context manager that does not batch (no-op default)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def create_store(config) -> Store:
    """Factory: create TiDBStore or SqliteStore based on config/env.

    TiDB 路径带**启动降级**（2026-09-28）：启动时连不上不再抛异常杀死进程
    （旧不变量「库连不上时绝对不要重启」的根源），而是返回一个**懒建池**的
    store —— 后台查询会在下次执行时按需重连。代价是降级期间首批 DB 读取
    会失败，调用方（``__main__.run_foreground``）对启动期读取各自做了
    try/except，进程会带着空 key 池 + 响亮的 warning 起来。
    """
    from loguru import logger

    tidb_host = os.getenv("ZHONGZHUAN_TIDB_HOST", "")

    if config.storage.backend == "tidb" or tidb_host:
        from .tidb_store import TiDBStore

        idle_release = os.getenv("ZHONGZHUAN_TIDB_IDLE_RELEASE_SECONDS")
        store = TiDBStore(
            host=tidb_host or os.getenv("ZHONGZHUAN_TIDB_HOST", ""),
            port=int(os.getenv("ZHONGZHUAN_TIDB_PORT", "4000")),
            user=os.getenv("ZHONGZHUAN_TIDB_USER", ""),
            password=os.getenv("ZHONGZHUAN_TIDB_PASSWORD", ""),
            database=os.getenv("ZHONGZHUAN_TIDB_DATABASE", "zhongzhuan"),
            ssl=os.getenv("ZHONGZHUAN_TIDB_SSL", "true") == "true",
            pool_size=int(os.getenv("ZHONGZHUAN_TIDB_POOL_SIZE", "20")),
            idle_release_seconds=int(idle_release) if idle_release not in (None, "") else None,
        )
        try:
            await store.ensure_ready()
            logger.info(
                "使用 TiDB Cloud 存储 "
                f"(pool_size={store._pool_size}, idle_release={store._idle_release_seconds:g}s)"
            )
        except Exception as exc:  # noqa: BLE001 - 启动降级：不杀进程，按需重连
            logger.warning(
                f"TiDB 不可达，进入降级模式（{type(exc).__name__}: {exc}）；"
                "进程继续启动，DB 查询将在下次执行时按需重连"
            )
        return store

    from .sqlite_store import SqliteStore

    logger.info("使用 SQLite 本地存储")
    return await SqliteStore.create(config.storage.sqlite_db_path)
