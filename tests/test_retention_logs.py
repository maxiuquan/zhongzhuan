"""Retention baseline tests (T06 / R-P0-03 / R-P1-71).

Covers:
* ``cleanup_old_logs`` on SQLite (real store) and a TiDB-backed stub
  (same SQL contract, no live TiDB required).
* Both backends: insert 100 rows spanning 30 days, run ``cleanup(14)`` and
  assert the surviving row count.
* The periodic scheduler actually deletes expired rows and stops cleanly.
"""

from __future__ import annotations

import asyncio

import pytest

from zhongzhuan.store import retention
from zhongzhuan.store.logs import cleanup_old_logs
from zhongzhuan.store.retention import RetentionScheduler
from zhongzhuan.store.sqlite_store import SqliteStore
from zhongzhuan.store.store import Store

_DAY = 86400


_PRIVATE_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _PRIVATE_LOOP
    if _PRIVATE_LOOP is None or _PRIVATE_LOOP.is_closed():
        _PRIVATE_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_PRIVATE_LOOP)
    return _PRIVATE_LOOP


def _run(coro):
    return _loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# SQLite backend (real store)
# --------------------------------------------------------------------------- #
@pytest.fixture
def sqlite_store(tmp_path):
    path = str(tmp_path / "retention.db")
    s = _run(SqliteStore.create(path))
    yield s
    _run(s.close())


def _seed_logs(store: Store, days_span: int = 30, count: int = 100) -> None:
    """Insert *count* rows with timestamps spread across *days_span* days.

    Half the rows are older than the 14-day cutoff, half are newer.
    """
    now = Store.now()
    for i in range(count):
        # i in [0, count): offset 0..days_span days into the past.
        offset = int((i / count) * days_span * _DAY)
        ts = now - offset
        _run(
            store.execute(
                "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id) VALUES(?,?,?,?,?)",
                (ts, "gpt-4o", 200, 10, f"req-{i}"),
            )
        )


def _count(store: Store) -> int:
    row = _run(store.fetchone("SELECT COUNT(*) FROM request_logs"))
    return row[0] if row else 0


def test_sqlite_cleanup_removes_old_rows(sqlite_store):
    _seed_logs(sqlite_store, days_span=30, count=100)
    assert _count(sqlite_store) == 100

    deleted = _run(cleanup_old_logs(sqlite_store, retention_days=14))
    remaining = _count(sqlite_store)

    # 30-day span, 14-day cutoff.  Rows with offset < 14 days are kept
    # (i < 14/30*100 = 46.67 => i=0..46, 47 rows); the rest are deleted (53).
    assert deleted == 53
    assert remaining == 47


def test_sqlite_cleanup_keeps_recent_rows(sqlite_store):
    now = Store.now()
    _run(
        sqlite_store.execute(
            "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id) VALUES(?,?,?,?,?)",
            (now - 1 * _DAY, "gpt-4o", 200, 10, "req-recent"),
        )
    )
    _run(
        sqlite_store.execute(
            "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id) VALUES(?,?,?,?,?)",
            (now - 20 * _DAY, "gpt-4o", 200, 10, "req-old"),
        )
    )
    deleted = _run(cleanup_old_logs(sqlite_store, retention_days=14))
    assert deleted == 1
    assert _count(sqlite_store) == 1


def test_sqlite_cleanup_zero_days_noop(sqlite_store):
    _seed_logs(sqlite_store, days_span=30, count=10)
    deleted = _run(cleanup_old_logs(sqlite_store, retention_days=0))
    assert deleted == 0
    assert _count(sqlite_store) == 10


def test_sqlite_batch_size_bounds_single_pass(sqlite_store):
    _seed_logs(sqlite_store, days_span=30, count=100)
    deleted = _run(
        retention.cleanup_old_logs(
            sqlite_store,
            retention_days=14,
            batch_size=5,
        )
    )
    # 53 rows are over the cutoff; batch_size only bounds each pass, but the
    # loop keeps going until fewer than batch_size remain.
    assert deleted == 53
    assert _count(sqlite_store) == 47


# --------------------------------------------------------------------------- #
# TiDB backend (stub with the same SQL interface)
# --------------------------------------------------------------------------- #
class _FakeTiDBStore(Store):
    """Minimal in-memory Store exposing the same SQL contract as the real one.

    Exercises the exact SQL the retention code issues against TiDB without
    requiring a live TiDB instance.
    """

    def __init__(self) -> None:
        self._rows: list[tuple] = []
        self._next_id = 1

    async def execute(self, sql, params=None):
        params = params or ()
        if sql.startswith("INSERT INTO request_logs"):
            self._rows.append((self._next_id, *params))
            self._next_id += 1
            return self._next_id - 1
        if sql.startswith("DELETE FROM request_logs"):
            cutoff = params[0]
            limit = params[1] if len(params) > 1 else None
            kept = []
            removed = 0
            for r in self._rows:
                if r[1] < cutoff and (limit is None or removed < limit):
                    removed += 1
                else:
                    kept.append(r)
            self._rows = kept
            return removed
        return 0

    async def fetchone(self, sql, params=None):
        if sql.startswith("SELECT COUNT(*) FROM request_logs"):
            cutoff = params[0] if params else None
            if cutoff is None:
                return (len(self._rows),)
            return (sum(1 for r in self._rows if r[1] < cutoff),)
        return None

    async def fetchall(self, sql, params=None):
        return list(self._rows)

    async def close(self):
        pass


def test_tidb_cleanup_removes_old_rows():
    store = _FakeTiDBStore()
    now = Store.now()
    for i in range(100):
        offset = int((i / 100) * 30 * _DAY)
        _run(
            store.execute(
                "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id) VALUES(?,?,?,?,?)",
                (now - offset, "gpt-4o", 200, 10, f"req-{i}"),
            )
        )
    assert len(store._rows) == 100

    deleted = _run(cleanup_old_logs(store, retention_days=14))
    assert deleted == 53
    assert len(store._rows) == 47


# --------------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------------- #
def test_scheduler_cleans_expired_logs(sqlite_store):
    _seed_logs(sqlite_store, days_span=30, count=100)
    assert _count(sqlite_store) == 100

    scheduler = RetentionScheduler(
        sqlite_store,
        retention_days=14,
        interval_seconds=0.05,
    )
    _run(scheduler.start())
    try:
        # Give the immediate first pass time to run.
        _run(asyncio.sleep(0.2))
    finally:
        _run(scheduler.stop())

    remaining = _count(sqlite_store)
    assert remaining == 47


def test_scheduler_runs_once_immediately(sqlite_store):
    _seed_logs(sqlite_store, days_span=30, count=20)
    # A very large interval proves the first pass fires immediately, not after
    # the first sleep.
    scheduler = RetentionScheduler(
        sqlite_store,
        retention_days=14,
        interval_seconds=3600,
    )
    _run(scheduler.start())
    try:
        _run(asyncio.sleep(0.1))
    finally:
        _run(scheduler.stop())
    assert _count(sqlite_store) == 10


def test_scheduler_stop_is_idempotent(sqlite_store):
    scheduler = RetentionScheduler(sqlite_store, retention_days=14)
    _run(scheduler.start())
    _run(scheduler.stop())
    _run(scheduler.stop())  # second stop must not raise
    assert scheduler._task is None
