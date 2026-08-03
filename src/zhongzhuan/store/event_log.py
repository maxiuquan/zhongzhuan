"""Append-only event log for the v3 Responses resource (T20 / R-P0-11 / R-P1-14).

``EventLog`` is the **single write path** for the ``response_events`` table.  It
only ever issues ``INSERT`` (append) and ``SELECT`` (read) statements — there is
no ``UPDATE`` / ``DELETE`` path here.  Retention purging lives in
``retention.py``; the CI lint rule forbids ``UPDATE response_events`` /
``DELETE FROM response_events`` inside this file (the ``purge_expired`` exception
belongs to ``retention.py``).

Design rules
------------
* **Sequence numbers**: when the caller does not supply ``seq``, ``EventLog``
  allocates the next one atomically per ``response_id`` under a per-response
  lock, so concurrent appends stay strictly monotonic with **no gaps and no
  duplicates** (T20 criterion ①).  In production the canonical ``seq`` comes
  from ``ResponsesEventEmitter`` (supplied explicitly, starting at 0); the
  allocator here is the convenience/fallback path and starts at 1 to stay
  compatible with the committed ``ResponseStore`` behaviour.
* **Reasoning never persisted**: the caller is responsible for passing already
  redacted payloads (``item_registry``).  This module stores exactly what it is
  given and never injects reasoning text.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping

from .store import Store

_JSON = dict(ensure_ascii=False, separators=(",", ":"))


def _dumps(obj: Any) -> str:
    return json.dumps(obj, **_JSON) if obj is not None else ""


def _loads(text: str, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


class EventLog:
    """Append-only persistence layer over the ``response_events`` table."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, response_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(response_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[response_id] = lock
            return lock

    async def append_event(
        self,
        *,
        response_id: str,
        event_type: str,
        data: Mapping[str, Any],
        workspace_id: str = "",
        seq: int | None = None,
        expires_at: int = 0,
    ) -> int:
        """Append one event; return the ``seq`` it was written under.

        If ``seq`` is ``None`` it is allocated as ``MAX(seq)+1`` per
        ``response_id`` inside a per-response lock (no gaps / dups under
        concurrency).  Supplying ``seq`` (e.g. from ``ResponsesEventEmitter``)
        writes it verbatim and bypasses allocation.
        """
        if seq is None:
            lock = await self._lock_for(response_id)
            async with lock:
                row = await self._store.fetchone(
                    "SELECT COALESCE(MAX(seq), 0) FROM response_events WHERE response_id = ?",
                    (response_id,),
                )
                seq = (row[0] if row else 0) + 1
                await self._store.execute(
                    "INSERT INTO response_events "
                    "(response_id, seq, workspace_id, event_type, data, ts, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (response_id, seq, workspace_id, event_type, _dumps(data),
                     int(time.time()), expires_at),
                )
                return seq
        await self._store.execute(
            "INSERT INTO response_events "
            "(response_id, seq, workspace_id, event_type, data, ts, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (response_id, seq, workspace_id, event_type, _dumps(data),
             int(time.time()), expires_at),
        )
        return seq

    async def read_events(
        self, response_id: str, *, after_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Return events for ``response_id`` ordered by ``seq`` after ``after_seq``."""
        rows = await self._store.fetchall(
            "SELECT seq, event_type, data FROM response_events "
            "WHERE response_id = ? AND seq > ? ORDER BY seq",
            (response_id, after_seq),
        )
        return [
            {"seq": r[0], "event_type": r[1], "data": _loads(r[2], {})}
            for r in rows
        ]


__all__ = ["EventLog"]
