"""Batch write queue for request / event logs (T20 / R-P1-64).

``BatchWriter`` buffers rows and flushes them in **one** multi-row ``INSERT``
per batch, so the number of commits is bounded by ``ceil(total / max_batch)``
(T20 criterion ③: commits <= batches).  It is backend-agnostic — the
multi-VALUES ``INSERT`` syntax is shared by SQLite and TiDB.

The writer is append-only: it never issues ``UPDATE`` / ``DELETE``.
"""
from __future__ import annotations

from typing import Any, Sequence

from .store import Store


class BatchWriter:
    """Buffers rows keyed by ``columns`` and flushes them as batched INSERTs."""

    def __init__(
        self,
        store: Store,
        *,
        table: str,
        columns: Sequence[str],
        max_batch: int = 500,
    ) -> None:
        self._store = store
        self._table = table
        self._columns = tuple(columns)
        self._max_batch = max(1, int(max_batch))
        self._buffer: list[tuple] = []
        self.flush_count = 0
        self.written = 0

    async def add(self, row: dict[str, Any]) -> None:
        """Queue one row (keyed by ``columns``); flush if the batch is full."""
        values = tuple(row.get(c) for c in self._columns)
        self._buffer.append(values)
        if len(self._buffer) >= self._max_batch:
            await self.flush()

    async def flush(self) -> int:
        """Flush the current buffer in a single INSERT. Returns rows written."""
        if not self._buffer:
            return 0
        rows = self._buffer
        self._buffer = []
        n = len(rows)
        placeholders = ", ".join(
            "(" + ",".join("?" for _ in self._columns) + ")" for _ in rows
        )
        cols = ", ".join(self._columns)
        sql = f"INSERT INTO {self._table} ({cols}) VALUES {placeholders}"
        params = tuple(v for row in rows for v in row)
        # Exactly one execute => exactly one commit (SQLite/TiDB commit per execute).
        await self._store.execute(sql, params)
        self.flush_count += 1
        self.written += n
        return n

    async def close(self) -> int:
        """Flush any remaining buffered rows (call on shutdown)."""
        return await self.flush()


__all__ = ["BatchWriter"]
