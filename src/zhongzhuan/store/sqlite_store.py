"""SQLite async store implementation."""
from __future__ import annotations

import aiosqlite
from pathlib import Path

from .store import Store
from .schema import SQLITE_SCHEMA


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
        await db.executescript(SQLITE_SCHEMA)
        await db.commit()
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