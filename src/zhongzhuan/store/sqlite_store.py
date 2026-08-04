"""SQLite async store implementation."""

from __future__ import annotations

import aiosqlite
from pathlib import Path

from .store import Store
from .migration_engine import SqliteMigrationExecutor, run_migrations_or_exit
from .migrations import MIGRATIONS


class SqliteStore(Store):
    """Async SQLite store using aiosqlite."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    @classmethod
    async def create(cls, db_path: str) -> SqliteStore:
        db_path = Path(db_path)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        db = await aiosqlite.connect(str(db_path))
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("PRAGMA foreign_keys=ON")

        # Versioned migrations (R-P0-04 / R-P0-05). A failure refuses to start.
        await run_migrations_or_exit(
            SqliteMigrationExecutor(db),
            MIGRATIONS,
            sqlite_db_path=db_path,
        )

        return cls(db)

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        cursor = await self._db.execute(sql, params or ())
        await self._db.commit()
        return cursor.lastrowid or 0

    async def fetchone(self, sql: str, params: tuple | None = None) -> tuple | None:
        cursor = await self._db.execute(sql, params or ())
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple | None = None) -> list[tuple]:
        cursor = await self._db.execute(sql, params or ())
        return await cursor.fetchall()

    async def close(self) -> None:
        await self._db.close()

    def transaction(self):
        """Batch multiple statements into a single commit (R-P1-50)."""
        return _SqliteTransaction(self._db)


class _SqliteTransaction:
    """Async context manager that batches writes into one commit.

    ``execute`` calls inside the block append to the same sqlite transaction
    and are committed exactly once on a clean exit.  Any exception rolls back
    the whole block.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._active = False

    async def __aenter__(self):
        if getattr(self._db, "in_transaction", False):
            await self._db.commit()
        await self._db.execute("BEGIN")
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if not self._active:
            return False
        self._active = False
        if exc_type is None:
            await self._db.commit()
        else:
            await self._db.rollback()
        return False
