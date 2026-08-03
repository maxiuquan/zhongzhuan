"""Request-log retention: cleanup + periodic scheduler (T06 / R-P0-03 / R-P1-71).

The full per-table retention matrix (``responses``, ``response_events``,
``background_jobs``, ``tool_executions``, ``idempotency_records``, ...) is
delivered later by T34.  This module establishes the **baseline**: the
``request_logs`` TTL cleanup plus a scheduler that runs it on a cadence, so
long-running instances never let the request log grow without bound.

The base cleanup is expressed in plain SQL so it works on both backends
(SQLite and TiDB) -- no date functions, just a ``ts < cutoff`` comparison.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

from .store import Store

#: Default TTL for ``request_logs`` (days).  Mirrors the architecture default
#: ``responses_bridge.retention.request_logs_days = 14``.
DEFAULT_REQUEST_LOG_DAYS: int = 14

#: Default scheduling interval in seconds (3 h).  The idle cost is negligible
#: and the cleanup only ever hits rows older than the TTL, so a coarse cadence
#: is fine.
DEFAULT_SCHEDULE_SECONDS: int = 3 * 3600

#: Default row-count cap for one cleanup pass (bounded DELETE keeps lock
#: windows short on large logs).
DEFAULT_BATCH_SIZE: int = 5000


async def cleanup_old_logs(
    store: Store,
    retention_days: int = DEFAULT_REQUEST_LOG_DAYS,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """Delete ``request_logs`` rows older than *retention_days*.

    Args:
        store: Store.
        retention_days: Keep rows newer than this many days. ``<= 0`` disables
            deletion (returns 0).
        batch_size: Maximum rows deleted per pass.

    Returns:
        Number of rows deleted.
    """
    if retention_days <= 0:
        return 0
    cutoff = Store.now() - retention_days * 86400
    # Cross-backend (SQLite + TiDB) bounded DELETE.  ``DELETE ... LIMIT`` and
    # ``DELETE ... RETURNING`` are not portable (SQLite rejects LIMIT, TiDB
    # rejects RETURNING), so the batch is selected by id in a subquery first.
    deleted = 0
    while True:
        count_row = await store.fetchone(
            "SELECT COUNT(*) FROM request_logs WHERE ts < ?", (cutoff,)
        )
        remaining = count_row[0] if count_row else 0
        if remaining <= 0:
            break
        take = min(batch_size, remaining)
        # Self-referencing UPDATE/DELETE needs the inner SELECT tabled out.
        await store.execute(
            "DELETE FROM request_logs WHERE id IN ("
            "SELECT id FROM (SELECT id FROM request_logs WHERE ts < ? LIMIT ?)"
            ")",
            (cutoff, take),
        )
        deleted += take
        if take < batch_size:
            break
    return deleted


@dataclass
class RetentionScheduler:
    """Runs :func:`cleanup_old_logs` on a fixed cadence as a background task.

    Example:
        .. code-block:: python

            scheduler = RetentionScheduler(
                store, retention_days=cfg_resp.retention.request_logs_days,
            )
            await scheduler.start()
            ...
            await scheduler.stop()
    """

    store: Store
    retention_days: int = DEFAULT_REQUEST_LOG_DAYS
    interval_seconds: float = DEFAULT_SCHEDULE_SECONDS
    batch_size: int = DEFAULT_BATCH_SIZE
    #: Optional hook called after each pass with the number of rows deleted.
    on_pass: Callable[[int], Awaitable[None]] | None = None

    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    async def start(self) -> None:
        """Start the periodic cleanup task (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="retention-cleanup")
        logger.info(
            f"retention scheduler started: request_logs TTL={self.retention_days}d "
            f"interval={self.interval_seconds}s"
        )

    async def stop(self) -> None:
        """Signal the scheduler to stop and await its exit."""
        if self._task is None or self._task.done():
            self._task = None
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        # Run once immediately, then every ``interval_seconds``.
        while not self._stop_event.is_set():
            try:
                deleted = await self._one_pass()
                if deleted:
                    logger.info(f"retention cleanup deleted {deleted} request_logs row(s)")
                if self.on_pass is not None:
                    await self.on_pass(deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("retention cleanup pass failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _one_pass(self) -> int:
        return await cleanup_old_logs(
            self.store,
            self.retention_days,
            batch_size=self.batch_size,
        )


# --------------------------------------------------------------------------- #
# Backwards-compatible convenience: keep the old ``logs.cleanup_old_logs`` name
# working by re-exporting the same implementation.  ``store/logs.py`` may import
# this rather than maintaining a duplicate body.
# --------------------------------------------------------------------------- #
async def run_cleanup_once(store: Store, retention_days: int = DEFAULT_REQUEST_LOG_DAYS) -> int:
    """One-shot cleanup -- convenient for tests and CLI invocations."""
    return await cleanup_old_logs(store, retention_days)


def _noop(_: int) -> Awaitable[None]:
    """Placeholder used to satisfy type checkers; never called directly."""
    return asyncio.sleep(0)