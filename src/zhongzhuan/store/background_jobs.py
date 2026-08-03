"""Background job lifecycle store (T24 / R-P1-34, R-P1-35, R-P1-37).

``BackgroundJobStore`` owns the whole ``background_jobs`` state machine:

    queued -> in_progress -> {completed | failed | incomplete | cancelled | expired}

and the three mechanisms that make a ``background=true`` response survive a
process restart:

* **lease** -- :meth:`claim_job` is an atomic compare-and-swap that hands one
  job to exactly one worker and stamps ``lease_until`` into the future, so a
  second worker polling the same table sees nothing to take;
* **heartbeat** -- :meth:`renew_lease` pushes ``lease_until`` forward while the
  worker is alive.  A ``kill -9`` stops the heartbeat, the lease expires, and
  the job becomes claimable again *by construction* (no crash detector, no
  timer, no reaper daemon);
* **bounded recovery** -- ``attempt`` counts how many times the job has been
  handed out.  Once it reaches :data:`MAX_RECOVERY_ATTEMPTS` the job is no
  longer claimable and the next poll marks it ``failed`` (R-P1-37: recover
  **exactly once**, then stop).  An unbounded retry on a job that crashes the
  worker every time is a crash loop, not resilience.

Why the CAS is written read-then-guarded-write
----------------------------------------------
:meth:`Store.execute` returns ``lastrowid``, not ``rowcount`` -- there is no
portable affected-row count across the SQLite and TiDB backends.  The claim is
therefore: read the candidate under a per-store :class:`asyncio.Lock`, then
issue an ``UPDATE`` whose ``WHERE`` still re-states the full precondition
(``attempt`` + ``lease_until``).  The lock makes the decision exact for the
in-process worker pool; the SQL guard keeps a *second process* from stealing a
lease it did not win.

DEVIATION (T24 spec): ``renew_lease`` also accepts a job whose ``lease_until``
is ``0`` (never leased).  A strict "only renew what you already hold" guard
would reject the very first lease of a freshly queued job, which is the
behaviour ``ResponseStore.lease_task`` has shipped with since T16.  An
*expired* lease (``0 < lease_until < now``) is still refused, so the anti-steal
intent of the rule is preserved.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from weakref import WeakKeyDictionary

from .store import Store

#: How many times a job may be handed to a worker in total.  ``attempt`` is
#: incremented on every claim, so ``2`` means "the original run plus exactly
#: one recovery" (R-P1-37).
MAX_RECOVERY_ATTEMPTS: int = 2

#: Terminal states of the background state machine (§4.2.4).
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "incomplete", "cancelled", "expired"}
)

#: States a job can be claimed / renewed from.
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "in_progress")

#: Column order of ``background_jobs`` (v004 DDL) -- kept in sync manually
#: because neither backend exposes a portable ``PRAGMA table_info``.
JOB_COLUMNS: tuple[str, ...] = (
    "task_id", "response_id", "workspace_id", "status", "created_at",
    "updated_at", "lease_until", "cancel_requested", "max_wall_seconds",
    "max_tool_rounds", "attempt", "expires_at",
)

#: One claim lock per underlying :class:`Store`.  Two ``BackgroundJobStore``
#: instances over the same connection (two workers in one process) must share
#: it, otherwise the CAS is only as atomic as the backend's isolation level.
_CLAIM_LOCKS: "WeakKeyDictionary[Store, asyncio.Lock]" = WeakKeyDictionary()


def _claim_lock(store: Store) -> asyncio.Lock:
    lock = _CLAIM_LOCKS.get(store)
    if lock is None:
        lock = asyncio.Lock()
        _CLAIM_LOCKS[store] = lock
    return lock


class BackgroundJobStore:
    """Data access for the ``background_jobs`` table (lease / cancel / recovery)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _now(now: int | None = None) -> int:
        """Current epoch seconds, or the injected ``now`` (deterministic tests)."""
        return int(time.time()) if now is None else int(now)

    def _lock(self) -> asyncio.Lock:
        return _claim_lock(self._store)

    # -- create --------------------------------------------------------------

    async def create_job(
        self,
        *,
        task_id: str,
        response_id: str,
        workspace_id: str = "",
        max_wall_seconds: int = 3600,
        max_tool_rounds: int = 32,
        expires_at: int = 0,
        now: int | None = None,
    ) -> None:
        """Insert one ``queued`` job with ``attempt = 0``."""
        ts = self._now(now)
        await self._store.execute(
            "INSERT INTO background_jobs "
            "(task_id, response_id, workspace_id, status, created_at, updated_at, "
            " lease_until, cancel_requested, max_wall_seconds, max_tool_rounds, "
            " attempt, expires_at) "
            "VALUES (?, ?, ?, 'queued', ?, ?, 0, 0, ?, ?, 0, ?)",
            (task_id, response_id, workspace_id, ts, ts,
             int(max_wall_seconds), int(max_tool_rounds), int(expires_at)),
        )

    # -- claim / lease -------------------------------------------------------

    async def peek_claimable(self, *, now: int | None = None) -> str | None:
        """The id :meth:`claim_job` would take next, without taking it."""
        ts = self._now(now)
        row = await self._store.fetchone(
            "SELECT task_id FROM background_jobs "
            "WHERE status IN ('queued', 'in_progress') AND lease_until < ? "
            "AND (expires_at = 0 OR expires_at > ?) AND attempt < ? "
            "ORDER BY created_at, task_id LIMIT 1",
            (ts, ts, MAX_RECOVERY_ATTEMPTS),
        )
        return str(row[0]) if row is not None else None

    async def claim_job(
        self,
        lease_seconds: int = 300,
        *,
        now: int | None = None,
        task_id: str | None = None,
    ) -> str | None:
        """Atomically take ownership of the oldest claimable job.

        A job is claimable when it is still active, its lease has expired, its
        TTL has not, and it has recovery attempts left.  On success the lease
        is pushed to ``now + lease_seconds``, the status becomes
        ``in_progress`` and ``attempt`` is incremented.

        ``task_id`` narrows the candidate set to one job -- a worker that was
        handed a specific job must not accidentally claim a different one.

        Returns the claimed ``task_id``, or ``None``.  When nothing is
        claimable *because* a job has burned all of its attempts, that job is
        marked ``failed`` first (R-P1-37 -- the second crash is terminal).
        """
        ts = self._now(now)
        async with self._lock():
            sql = (
                "SELECT task_id, attempt FROM background_jobs "
                "WHERE status IN ('queued', 'in_progress') AND lease_until < ? "
                "AND (expires_at = 0 OR expires_at > ?) AND attempt < ?"
            )
            params: tuple[Any, ...] = (ts, ts, MAX_RECOVERY_ATTEMPTS)
            if task_id is not None:
                sql += " AND task_id = ?"
                params += (task_id,)
            row = await self._store.fetchone(
                sql + " ORDER BY created_at, task_id LIMIT 1", params,
            )
            if row is None:
                await self._reap_exhausted(ts, task_id=task_id)
                return None
            claimed_id, attempt = str(row[0]), int(row[1])
            await self._store.execute(
                "UPDATE background_jobs SET status = 'in_progress', "
                "lease_until = ?, attempt = ?, updated_at = ? "
                "WHERE task_id = ? AND attempt = ? AND lease_until < ?",
                (ts + int(lease_seconds), attempt + 1, ts, claimed_id, attempt, ts),
            )
            return claimed_id

    async def _reap_exhausted(self, now: int, *, task_id: str | None = None) -> None:
        """Mark every attempt-exhausted, lease-expired job ``failed``.

        Runs on the *miss* path of :meth:`claim_job` so the worker loop doubles
        as the reaper -- no separate daemon to deploy, monitor or forget.
        """
        sql = (
            "SELECT task_id FROM background_jobs "
            "WHERE status IN ('queued', 'in_progress') AND lease_until < ? "
            "AND attempt >= ?"
        )
        params: tuple[Any, ...] = (now, MAX_RECOVERY_ATTEMPTS)
        if task_id is not None:
            sql += " AND task_id = ?"
            params += (task_id,)
        rows = await self._store.fetchall(sql, params)
        for row in rows:
            await self.mark_failed(str(row[0]), reason="recovery_exhausted", now=now)

    async def expire_stale(self, *, now: int | None = None) -> list[str]:
        """Mark every past-TTL active job ``expired``; return their ids.

        ``expires_at`` is the job's absolute deadline: a queued job nobody ever
        picked up must not be resurrected days later, because its client is
        long gone (§4.2.4).
        """
        ts = self._now(now)
        rows = await self._store.fetchall(
            "SELECT task_id FROM background_jobs "
            "WHERE status IN ('queued', 'in_progress') "
            "AND expires_at > 0 AND expires_at <= ?",
            (ts,),
        )
        expired = [str(r[0]) for r in rows]
        for task_id in expired:
            await self.mark_terminal(task_id, "expired", now=ts)
        return expired

    async def renew_lease(
        self, task_id: str, lease_seconds: int = 300, *, now: int | None = None,
    ) -> bool:
        """Heartbeat: push ``lease_until`` forward while the job is still ours.

        Returns ``False`` once the job is terminal or its lease already
        expired (someone else may have claimed it) -- the worker's heartbeat
        loop uses that as its stop signal.  See the module docstring for the
        ``lease_until == 0`` deviation.
        """
        ts = self._now(now)
        async with self._lock():
            row = await self._store.fetchone(
                "SELECT status, lease_until FROM background_jobs WHERE task_id = ?",
                (task_id,),
            )
            if row is None:
                return False
            status, lease_until = str(row[0]), int(row[1])
            if status not in ACTIVE_STATUSES:
                return False
            if lease_until != 0 and lease_until < ts:
                return False
            await self._store.execute(
                "UPDATE background_jobs SET lease_until = ?, updated_at = ? "
                "WHERE task_id = ? AND status IN ('queued', 'in_progress') "
                "AND lease_until = ?",
                (ts + int(lease_seconds), ts, task_id, lease_until),
            )
            return True

    # -- cancel --------------------------------------------------------------

    async def request_cancel(self, task_id: str, *, now: int | None = None) -> None:
        """Raise the cooperative cancel flag (the worker polls it per round)."""
        await self._store.execute(
            "UPDATE background_jobs SET cancel_requested = 1, updated_at = ? "
            "WHERE task_id = ?",
            (self._now(now), task_id),
        )

    async def is_cancel_requested(self, task_id: str) -> bool:
        """Whether a cancel has been requested for ``task_id``."""
        row = await self._store.fetchone(
            "SELECT cancel_requested FROM background_jobs WHERE task_id = ?",
            (task_id,),
        )
        return bool(row[0]) if row is not None else False

    # -- terminal ------------------------------------------------------------

    async def mark_terminal(
        self, task_id: str, status: str, *, now: int | None = None,
    ) -> None:
        """Set the job's status (used for both terminal and interim moves)."""
        await self._store.execute(
            "UPDATE background_jobs SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, self._now(now), task_id),
        )

    async def mark_failed(
        self,
        task_id: str,
        *,
        reason: str = "recovery_exhausted",
        now: int | None = None,
    ) -> None:
        """Fail the job and record *why* on the associated ``responses`` row.

        The v004 ``background_jobs`` DDL has no ``terminal_reason`` column and
        T24 must not change it, so the reason is written to the response the
        job belongs to -- which is where every reader (retrieve endpoint,
        catch-up, audit) looks for it anyway.
        """
        ts = self._now(now)
        await self.mark_terminal(task_id, "failed", now=ts)
        row = await self._store.fetchone(
            "SELECT response_id FROM background_jobs WHERE task_id = ?", (task_id,),
        )
        response_id = str(row[0]) if row is not None and row[0] else ""
        if not response_id:
            return
        await self._store.execute(
            "UPDATE responses SET status = 'failed', terminal_reason = ?, "
            "updated_at = ?, completed_at = ? WHERE response_id = ?",
            (reason, ts, ts, response_id),
        )

    # -- read ----------------------------------------------------------------

    async def get_job(
        self, task_id: str, *, workspace_id: str = "",
    ) -> dict[str, Any] | None:
        """Return the whole row as a dict, scoped to ``workspace_id``."""
        row = await self._store.fetchone(
            "SELECT * FROM background_jobs WHERE task_id = ? AND workspace_id = ?",
            (task_id, workspace_id),
        )
        if row is None:
            return None
        return dict(zip(JOB_COLUMNS, row))

    async def get_job_any_tenant(self, task_id: str) -> dict[str, Any] | None:
        """Tenant-agnostic read for the worker itself (it owns every tenant)."""
        row = await self._store.fetchone(
            "SELECT * FROM background_jobs WHERE task_id = ?", (task_id,),
        )
        if row is None:
            return None
        return dict(zip(JOB_COLUMNS, row))


__all__ = [
    "MAX_RECOVERY_ATTEMPTS",
    "TERMINAL_STATUSES",
    "TERMINAL_STATUSES",
    "ACTIVE_STATUSES",
    "JOB_COLUMNS",
    "BackgroundJobStore",
]
