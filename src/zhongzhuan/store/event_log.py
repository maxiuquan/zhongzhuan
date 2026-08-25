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
from weakref import WeakValueDictionary

from .store import Store


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if obj is not None else ""


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
        #: 每个 response_id 一把 seq 分配锁。用 WeakValueDictionary 而不是普通
        #: dict：锁对象只被「正拿着它排队/执行的协程帧」强引用，最后一个使用者
        #: 离开 ``async with`` 的瞬间条目自动消失 —— 长跑进程里 response_id 无限
        #: 增长也不会把这张表撑成内存泄漏（旧实现永不回收）。
        #:
        #: 为什么这样没有竞态：CPython 引用计数即时回收，条目消亡当且仅当所有
        #: 使用者的帧都已退出临界区。等待者 C 在拿到锁对象的**那一刻**起就持有
        #: 强引用，因此后来者 D 只会取到同一把锁，不可能出现「C 还在旧锁下排队、
        #: D 却拿到新锁并行分配 seq」的撕裂场景。（朴素方案的坑：持锁者释放后
        #: 检查 ``lock.locked()`` 再 ``del`` —— 释放会唤醒等待者但 ``locked()``
        #: 已变 False，会把还有等待者的锁从表里删掉，正是上面说的撕裂。）
        self._locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()
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
                    (response_id, seq, workspace_id, event_type, _dumps(data), int(time.time()), expires_at),
                )
                return seq
        await self._store.execute(
            "INSERT INTO response_events "
            "(response_id, seq, workspace_id, event_type, data, ts, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (response_id, seq, workspace_id, event_type, _dumps(data), int(time.time()), expires_at),
        )
        return seq

    async def read_events(
        self,
        response_id: str,
        *,
        after_seq: int = -1,
    ) -> list[dict[str, Any]]:
        """Return events for ``response_id`` ordered by ``seq`` after ``after_seq``."""
        rows = await self._store.fetchall(
            "SELECT seq, event_type, data FROM response_events WHERE response_id = ? AND seq > ? ORDER BY seq",
            (response_id, after_seq),
        )
        return [{"seq": r[0], "event_type": r[1], "data": _loads(r[2], {})} for r in rows]


__all__ = ["EventLog"]
