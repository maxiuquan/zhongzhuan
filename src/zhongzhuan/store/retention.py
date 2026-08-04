"""Full-table retention: per-table TTL cleanup + disk watermark (T34 / R-P2-11/18).

T06 established the **baseline**: ``request_logs`` TTL cleanup plus a periodic
scheduler.  T34 (this module) delivers the full per-table retention matrix the
T06 header promised, plus the disk-watermark early-reclaim path of R-P2-18.

TTL matrix (defaults from architecture doc §5.9 / PRD §4-Q3)
-----------------------------------------------------------
======================  =======================  ================================
table                   cleanup predicate        TTL
======================  =======================  ================================
``responses``           age by ``created_at``    ``responses_days`` = 30d
``response_input_items``  cascade via parent       follows ``responses`` (30d)
``response_output_items`` cascade via parent       follows ``responses`` (30d)
``response_state_chain``  cascade via parent       follows ``responses`` (30d)
``response_events``     age by ``ts``            ``events_days`` = 7d
``background_jobs``     terminal + age/deadline  ``background_days`` = 30d
``tool_executions``     age by ``created_at``    ``tool_audit_days`` = 90d
``idempotency_records`` ``expires_at`` only      set by caller (24h default)
``request_logs``        age by ``ts``            ``request_logs_days`` = 14d
======================  =======================  ================================

Two cleanup mechanisms are used, per table:

* **age-based** -- ``<timecol> < now - TTL`` (plain ``?`` comparison, portable
  across SQLite and TiDB -- no date functions, matching the T06 constraint);
* **expires_at-based** -- ``expires_at > 0 AND expires_at <= now``.  ``0``
  means "never expires" and is the documented contract of
  :class:`~zhongzhuan.store.background_jobs.BackgroundJobStore` and
  :class:`~zhongzhuan.store.idempotency.IdempotencyStore`, so rows carrying it
  are **never** purged by this path.

Special cases honoured here:

* ``background_jobs`` -- only **terminal** rows (completed/failed/cancelled/
  expired) are ever swept, so an active job whose ``expires_at = 0`` (no
  deadline, "never auto-expire") can never be killed mid-flight by the
  retention scheduler.  Terminal rows are swept by their explicit deadline
  *or* after ``background_days``.
* ``idempotency_records`` -- ``expires_at`` is the *only* TTL key: deleting a
  row whose ``expires_at = 0`` would break the runtime idempotency guarantee
  ("the key blocks forever").
* cascade tables (``response_input_items`` / ``response_output_items`` /
  ``response_state_chain``) have no time column of their own, so they are
  cleaned as orphans of purged ``responses`` rows.

Disk watermark (R-P2-18)
------------------------
``early_reclaim_if_needed`` measures the SQLite file (main + WAL + SHM).  When
usage exceeds the soft limit (default 8 GB, §5.9) it runs full retention with
every TTL scaled by ``soft_limit / actual_size`` -- i.e. it reclaims *earlier*
than the normal TTLs -- and emits a ``logger.warning`` alert.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from pathlib import Path
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
#: windows short on large tables).
DEFAULT_BATCH_SIZE: int = 5000

#: Default SQLite soft limit (GB) from §5.9; exceeding it triggers early
#: reclamation + a disk-watermark alert.
DEFAULT_SOFT_LIMIT_GB: float = 8.0


@dataclass(frozen=True)
class RetentionLimits:
    """Per-table TTL matrix (architecture doc §5.9 / PRD §4-Q3 defaults)."""

    responses_days: int = 30
    events_days: int = 7
    background_days: int = 30
    tool_audit_days: int = 90
    idempotency_hours: int = 24
    request_logs_days: int = 14


DEFAULT_RETENTION_LIMITS: RetentionLimits = RetentionLimits()

#: Terminal statuses of ``background_jobs``; only these may be swept by the
#: retention scheduler (active jobs are handled by ``expire_stale`` instead).
_TERMINAL_BG_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled", "expired")

_DAY: int = 86400


# --------------------------------------------------------------------------- #
# Generic bounded cross-backend delete
# --------------------------------------------------------------------------- #


async def _delete_batched(
    store: Store,
    table: str,
    id_cols: tuple[str, ...],
    where: str,
    params: tuple,
    *,
    batch_size: int,
) -> int:
    """Bounded DELETE for *table* matching *where* on both backends.

    ``DELETE ... LIMIT`` is rejected by SQLite and ``DELETE ... RETURNING`` by
    TiDB, so the batch is selected by primary key in a tabled-out subquery
    first (same trick the T06 baseline used for ``request_logs``).  Loops until
    fewer than ``batch_size`` rows remain.
    """
    deleted = 0
    while True:
        count_row = await store.fetchone(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
        remaining = count_row[0] if count_row else 0
        if remaining <= 0:
            break
        take = min(batch_size, remaining)
        if len(id_cols) == 1:
            col = id_cols[0]
            sql = (
                f"DELETE FROM {table} WHERE {col} IN ("
                f"SELECT {col} FROM (SELECT {col} FROM {table} WHERE {where} LIMIT ?) AS batch"
                f")"
            )
        else:
            cols = ", ".join(id_cols)
            sql = (
                f"DELETE FROM {table} WHERE ({cols}) IN ("
                f"SELECT {cols} FROM (SELECT {cols} FROM {table} WHERE {where} LIMIT ?) AS batch"
                f")"
            )
        await store.execute(sql, params + (take,))
        deleted += take
        if take < batch_size:
            break
    return deleted


def _ttl_seconds(days: int) -> int:
    """Guard: ``days <= 0`` disables the age-based cleanup for a table."""
    return days * _DAY if days > 0 else 0


# --------------------------------------------------------------------------- #
# Request-log baseline (T06, kept for backwards compatibility)
# --------------------------------------------------------------------------- #


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
    cutoff = Store.now() - _ttl_seconds(retention_days)
    return await _delete_batched(
        store,
        "request_logs",
        ("id",),
        "ts < ?",
        (cutoff,),
        batch_size=batch_size,
    )


# --------------------------------------------------------------------------- #
# Full-table retention pass (T34 / R-P2-11)
# --------------------------------------------------------------------------- #


async def run_full_retention(
    store: Store,
    limits: RetentionLimits = DEFAULT_RETENTION_LIMITS,
    *,
    now: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """Run the full per-table retention pass.

    Args:
        store: Store.
        limits: TTL matrix (defaults to §5.9).
        now: Reference timestamp for tests (defaults to ``Store.now()``).
        batch_size: Bounded rows per DELETE pass.

    Returns:
        ``{table: rows_deleted}`` for every table in the matrix.
    """
    ts = now if now is not None else Store.now()
    deleted: dict[str, int] = {}

    # responses: explicit expires_at deadline OR the age-based 30d TTL.
    resp_secs = _ttl_seconds(limits.responses_days)
    if resp_secs:
        deleted["responses"] = await _delete_batched(
            store,
            "responses",
            ("response_id",),
            "(expires_at > 0 AND expires_at <= ?) OR created_at < ?",
            (ts, ts - resp_secs),
            batch_size=batch_size,
        )
    else:
        deleted["responses"] = 0

    # cascade children of purged/absent responses (no time column of their own).
    orphan = "response_id NOT IN (SELECT response_id FROM responses)"
    for table, cols in (
        ("response_input_items", ("response_id", "seq")),
        ("response_output_items", ("response_id", "output_index")),
        ("response_state_chain", ("response_id",)),
    ):
        deleted[table] = await _delete_batched(
            store,
            table,
            cols,
            orphan,
            (),
            batch_size=batch_size,
        )

    # response_events: 7d from the event timestamp.
    events_secs = _ttl_seconds(limits.events_days)
    if events_secs:
        deleted["response_events"] = await _delete_batched(
            store,
            "response_events",
            ("response_id", "seq"),
            "ts < ?",
            (ts - events_secs,),
            batch_size=batch_size,
        )
    else:
        deleted["response_events"] = 0

    # tool_executions: 90d audit retention.
    tool_secs = _ttl_seconds(limits.tool_audit_days)
    if tool_secs:
        deleted["tool_executions"] = await _delete_batched(
            store,
            "tool_executions",
            ("execution_id",),
            "created_at < ?",
            (ts - tool_secs,),
            batch_size=batch_size,
        )
    else:
        deleted["tool_executions"] = 0

    # background_jobs: only terminal rows; explicit deadline OR 30d age.
    # Active jobs (queued/in_progress) are NEVER swept -- this preserves the
    # "expires_at = 0 => no TTL, never auto-expired" contract of the
    # BackgroundJobStore while still bounding the terminal audit trail.
    bg_secs = _ttl_seconds(limits.background_days)
    if bg_secs:
        terminal = ", ".join(f"'{s}'" for s in _TERMINAL_BG_STATUSES)
        deleted["background_jobs"] = await _delete_batched(
            store,
            "background_jobs",
            ("task_id",),
            f"status IN ({terminal}) AND ((expires_at > 0 AND expires_at <= ?) OR created_at < ?)",
            (ts, ts - bg_secs),
            batch_size=batch_size,
        )
    else:
        deleted["background_jobs"] = 0

    # idempotency_records: expires_at is the ONLY TTL (expires_at = 0 means
    # "never expires" and the runtime idempotency view depends on that).
    deleted["idempotency_records"] = await _delete_batched(
        store,
        "idempotency_records",
        ("workspace_id", "idempotency_key"),
        "expires_at > 0 AND expires_at <= ?",
        (ts,),
        batch_size=batch_size,
    )

    # request_logs: legacy ts-based TTL (T06).
    log_secs = _ttl_seconds(limits.request_logs_days)
    if log_secs:
        deleted["request_logs"] = await _delete_batched(
            store,
            "request_logs",
            ("id",),
            "ts < ?",
            (ts - log_secs,),
            batch_size=batch_size,
        )
    else:
        deleted["request_logs"] = 0

    return deleted


# --------------------------------------------------------------------------- #
# Disk watermark: early reclamation + alert (R-P2-18 / §4-Q3)
# --------------------------------------------------------------------------- #


def db_file_size(db_path: str | Path) -> int:
    """Total bytes of the SQLite database file (main + WAL + SHM)."""
    path = Path(db_path)
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            pass
    return total


async def check_disk_watermark(
    db_path: str | Path,
    *,
    soft_limit_gb: float | None = None,
    soft_limit_bytes: int | None = None,
) -> float:
    """Return the DB size / soft-limit ratio (``1.0`` = at the soft limit).

    Emits a ``logger.warning`` alert when the limit is exceeded (R-P2-18).
    Returns ``0.0`` when no limit is configured.
    """
    if soft_limit_bytes is None:
        if soft_limit_gb is None:
            return 0.0
        soft_limit_bytes = int(soft_limit_gb * 1024**3)
    if soft_limit_bytes <= 0:
        return 0.0
    size = db_file_size(db_path)
    ratio = size / soft_limit_bytes
    if ratio > 1.0:
        logger.warning(
            f"disk watermark exceeded: DB {db_path} uses {size} bytes "
            f"({(ratio - 1.0) * 100:.0f}% over the {soft_limit_bytes}-byte soft "
            f"limit) -- scheduling early reclamation"
        )
    return ratio


def scale_limits(limits: RetentionLimits, factor: float) -> RetentionLimits:
    """Scale every TTL by *factor* (``factor < 1`` => earlier reclamation)."""
    return RetentionLimits(
        responses_days=max(1, int(limits.responses_days * factor)),
        events_days=max(1, int(limits.events_days * factor)),
        background_days=max(1, int(limits.background_days * factor)),
        tool_audit_days=max(1, int(limits.tool_audit_days * factor)),
        idempotency_hours=max(1, int(limits.idempotency_hours * factor)),
        request_logs_days=max(1, int(limits.request_logs_days * factor)),
    )


async def early_reclaim_if_needed(
    store: Store,
    db_path: str | Path,
    limits: RetentionLimits = DEFAULT_RETENTION_LIMITS,
    *,
    soft_limit_gb: float | None = None,
    soft_limit_bytes: int | None = None,
    now: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, int]:
    """When DB disk usage exceeds the soft limit, reclaim *early*.

    "Early" means every TTL is scaled by ``soft_limit / actual_size`` so the
    cleanup fires *before* the normal TTLs would have expired the rows.  An
    alert (``logger.warning``) is emitted by :func:`check_disk_watermark` and
    here for the reclamation action.

    Returns:
        Per-table deleted counts; an empty dict when usage is under the limit.
    """
    ratio = await check_disk_watermark(db_path, soft_limit_gb=soft_limit_gb, soft_limit_bytes=soft_limit_bytes)
    if ratio <= 1.0:
        return {}
    factor = 1.0 / ratio
    scaled = scale_limits(limits, factor)
    logger.warning(
        f"early reclamation triggered: disk at {ratio:.2f}x the soft limit; "
        f"running retention with TTLs scaled by {factor:.3f}"
    )
    return await run_full_retention(store, scaled, now=now, batch_size=batch_size)


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #


@dataclass
class RetentionScheduler:
    """Runs the full retention pass on a fixed cadence as a background task.

    Backwards compatible with the T06 constructor (``store``,
    ``retention_days``, ``interval_seconds``, ``batch_size``, ``on_pass``).
    New T34 knobs are optional: ``limits`` (full TTL matrix) and ``db_path`` /
    ``soft_limit_gb`` (disk-watermark early reclamation).

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
    #: Full TTL matrix (defaults to §5.9; ``request_logs_days`` follows
    #: ``retention_days`` unless an explicit ``limits`` overrides it).
    limits: RetentionLimits | None = None
    #: SQLite DB path to monitor for the disk-watermark early reclamation.
    db_path: str | None = None
    #: Soft disk limit in GB (default 8 GB, §5.9).
    soft_limit_gb: float = DEFAULT_SOFT_LIMIT_GB

    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    async def start(self) -> None:
        """Start the periodic cleanup task (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="retention-cleanup")
        logger.info(
            f"retention scheduler started: request_logs TTL={self.retention_days}d interval={self.interval_seconds}s"
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

    def _effective_limits(self) -> RetentionLimits:
        limits = self.limits if self.limits is not None else DEFAULT_RETENTION_LIMITS
        if limits.request_logs_days == self.retention_days:
            return limits
        return RetentionLimits(**{**asdict(limits), "request_logs_days": self.retention_days})

    async def _run(self) -> None:
        # Run once immediately, then every ``interval_seconds``.
        while not self._stop_event.is_set():
            try:
                deleted = await self._one_pass()
                if deleted:
                    logger.info(f"retention cleanup deleted {deleted} row(s)")
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
        limits = self._effective_limits()
        report = await run_full_retention(
            self.store,
            limits,
            batch_size=self.batch_size,
        )
        if self.db_path:
            extra = await early_reclaim_if_needed(
                self.store,
                self.db_path,
                limits,
                soft_limit_gb=self.soft_limit_gb,
                batch_size=self.batch_size,
            )
            for table, count in extra.items():
                report[table] = report.get(table, 0) + count
        return sum(report.values())


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


__all__ = [
    "DEFAULT_REQUEST_LOG_DAYS",
    "DEFAULT_SCHEDULE_SECONDS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_SOFT_LIMIT_GB",
    "RetentionLimits",
    "DEFAULT_RETENTION_LIMITS",
    "cleanup_old_logs",
    "run_full_retention",
    "db_file_size",
    "check_disk_watermark",
    "scale_limits",
    "early_reclaim_if_needed",
    "RetentionScheduler",
    "run_cleanup_once",
]
