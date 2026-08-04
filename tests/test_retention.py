"""T34 acceptance — full-table retention + disk watermark (R-P2-11, R-P2-18).

Criterion mapping
-----------------
* ⑤ each table is cleaned by **its own TTL** (architecture doc §5.9 / PRD §4-Q3
  defaults): one test per table plus the scheduler driving the full pass;
* ⑥ disk usage over the soft limit triggers **early reclamation** (TTLs scaled
  down) **and a logger alert**;
* regression guard: the T06 ``request_logs`` baseline keeps working, and the
  ``background_jobs`` "``expires_at = 0`` never auto-expires" contract (only
  terminal rows are ever swept) is preserved.

The background-jobs semantics deserve an explicit note: ``expire_stale`` owns
*active* jobs, so the retention scheduler only ever sweeps **terminal**
statuses (completed / failed / cancelled / expired).  A queued/in_progress job
-- even one with ``expires_at = 0`` -- can never be killed mid-flight here.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from zhongzhuan.store.retention import (
    DEFAULT_RETENTION_LIMITS,
    RetentionLimits,
    RetentionScheduler,
    check_disk_watermark,
    early_reclaim_if_needed,
    run_full_retention,
    scale_limits,
)
from zhongzhuan.store.store import Store

_DAY = 86400


async def _add_response(store, rid: str, *, created_at: int, expires_at: int = 0, status: str = "completed"):
    await store.execute(
        "INSERT INTO responses(response_id, workspace_id, status, model, created_at, updated_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (rid, "ws", status, "gpt-4o", created_at, created_at, expires_at),
    )


async def _add_input_item(store, rid: str, seq: int):
    await store.execute(
        "INSERT INTO response_input_items(response_id, seq, workspace_id, item_type, role) VALUES(?,?,?,?,?)",
        (rid, seq, "ws", "message", "user"),
    )


async def _add_output_item(store, rid: str, out_idx: int):
    await store.execute(
        "INSERT INTO response_output_items(response_id, seq, output_index, workspace_id, item_type, role) "
        "VALUES(?,?,?,?,?,?)",
        (rid, 0, out_idx, "ws", "message", "assistant"),
    )


async def _add_state_chain(store, rid: str):
    await store.execute(
        "INSERT INTO response_state_chain(response_id, workspace_id, previous_response_id, depth) VALUES(?,?,?,?)",
        (rid, "ws", "", 0),
    )


async def _add_event(store, rid: str, seq: int, *, ts: int):
    await store.execute(
        "INSERT INTO response_events(response_id, seq, workspace_id, event_type, data, ts) VALUES(?,?,?,?,?,?)",
        (rid, seq, "ws", "response.created", "{}", ts),
    )


async def _add_bg_job(store, task_id: str, *, status: str, created_at: int, expires_at: int = 0):
    await store.execute(
        "INSERT INTO background_jobs(task_id, response_id, workspace_id, status, created_at, updated_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (task_id, "resp", "ws", status, created_at, created_at, expires_at),
    )


async def _add_tool_exec(store, exec_id: str, *, created_at: int):
    await store.execute(
        "INSERT INTO tool_executions(execution_id, response_id, workspace_id, call_id, tool_name,"
        " idempotency_key, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (exec_id, "resp", "ws", "call1", "get_weather", "key1", "completed", created_at, created_at),
    )


async def _add_idem(store, key: str, *, created_at: int, expires_at: int):
    await store.execute(
        "INSERT INTO idempotency_records(workspace_id, idempotency_key, request_digest, response_id,"
        " status_code, state, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("ws", key, "digest", "resp", 200, "done", created_at, expires_at),
    )


async def _add_log(store, rid: str, ts: int):
    await store.execute(
        "INSERT INTO request_logs(ts, model_name, status, latency_ms, request_id) VALUES(?,?,?,?,?)",
        (ts, "gpt-4o", 200, 10, rid),
    )


async def _count(store, table: str, where: str = "1=1", params: tuple = ()) -> int:
    row = await store.fetchone(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
    return row[0] if row else 0


# --------------------------------------------------------------------------- #
# ⑤ per-table TTL cleanup
# --------------------------------------------------------------------------- #


async def test_responses_cleaned_by_30d_ttl_and_expires_at(store):
    now = Store.now()
    await _add_response(store, "r_old", created_at=now - 31 * _DAY)  # age-based: deleted
    await _add_response(store, "r_fresh", created_at=now - 5 * _DAY)  # kept
    await _add_response(store, "r_deadline", created_at=now - _DAY, expires_at=now - 100)  # deadline: deleted

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["responses"] == 2
    assert await _count(store, "responses") == 1


async def test_input_output_items_and_state_chain_cascade(store):
    now = Store.now()
    # Orphans (parent never exists / already purged) are swept...
    await _add_input_item(store, "ghost", 0)
    await _add_output_item(store, "ghost", 0)
    await _add_state_chain(store, "ghost")
    # ...while children of a live parent survive.
    await _add_response(store, "alive", created_at=now)
    await _add_input_item(store, "alive", 0)
    await _add_output_item(store, "alive", 0)
    await _add_state_chain(store, "alive")

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["response_input_items"] == 1
    assert report["response_output_items"] == 1
    assert report["response_state_chain"] == 1
    assert await _count(store, "response_input_items") == 1
    assert await _count(store, "response_output_items") == 1
    assert await _count(store, "response_state_chain") == 1


async def test_response_events_cleaned_by_7d_ttl(store):
    now = Store.now()
    await _add_event(store, "r1", 0, ts=now - 8 * _DAY)  # deleted
    await _add_event(store, "r2", 0, ts=now - 3 * _DAY)  # kept

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["response_events"] == 1
    assert await _count(store, "response_events") == 1


async def test_tool_executions_cleaned_by_90d_ttl(store):
    now = Store.now()
    await _add_tool_exec(store, "exec_old", created_at=now - 91 * _DAY)  # deleted
    await _add_tool_exec(store, "exec_recent", created_at=now - 10 * _DAY)  # kept

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["tool_executions"] == 1
    assert await _count(store, "tool_executions") == 1


async def test_background_jobs_terminal_only_never_sweeps_active(store):
    now = Store.now()
    await _add_bg_job(store, "bg_old_terminal", status="completed", created_at=now - 31 * _DAY)  # deleted
    await _add_bg_job(
        store, "bg_active", status="queued", created_at=now - 31 * _DAY
    )  # KEPT (expires_at=0, never swept)
    await _add_bg_job(store, "bg_recent", status="completed", created_at=now - 5 * _DAY)  # kept
    await _add_bg_job(store, "bg_deadline", status="completed", created_at=now - _DAY, expires_at=now - 10)  # deleted

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["background_jobs"] == 2
    assert await _count(store, "background_jobs", "task_id='bg_active'") == 1
    assert await _count(store, "background_jobs", "task_id='bg_recent'") == 1


async def test_idempotency_records_expires_at_is_the_only_ttl(store):
    now = Store.now()
    await _add_idem(store, "k_expired", created_at=now - _DAY, expires_at=now - 100)  # deleted
    await _add_idem(store, "k_never", created_at=now - 5 * _DAY, expires_at=0)  # KEPT (永不过期)
    await _add_idem(store, "k_future", created_at=now - _DAY, expires_at=now + 3600)  # kept

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["idempotency_records"] == 1
    assert await _count(store, "idempotency_records") == 2


async def test_request_logs_cleaned_by_14d_ttl(store):
    now = Store.now()
    await _add_log(store, "req_old", ts=now - 15 * _DAY)  # deleted
    await _add_log(store, "req_new", ts=now - 3 * _DAY)  # kept

    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS, now=now)

    assert report["request_logs"] == 1
    assert await _count(store, "request_logs") == 1


async def test_full_retention_report_covers_all_tables(store):
    report = await run_full_retention(store, DEFAULT_RETENTION_LIMITS)
    expected = {
        "responses",
        "response_input_items",
        "response_output_items",
        "response_state_chain",
        "response_events",
        "tool_executions",
        "background_jobs",
        "idempotency_records",
        "request_logs",
    }
    assert set(report) == expected
    assert all(isinstance(v, int) and v >= 0 for v in report.values())


async def test_scheduler_runs_full_retention(store):
    now = Store.now()
    await _add_response(store, "r_old", created_at=now - 31 * _DAY)
    await _add_event(store, "r1", 0, ts=now - 8 * _DAY)
    await _add_tool_exec(store, "exec_old", created_at=now - 91 * _DAY)
    await _add_log(store, "req_old", ts=now - 15 * _DAY)

    scheduler = RetentionScheduler(store, retention_days=14, interval_seconds=3600)
    await scheduler.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await scheduler.stop()

    assert await _count(store, "responses") == 0
    assert await _count(store, "response_events") == 0
    assert await _count(store, "tool_executions") == 0
    assert await _count(store, "request_logs") == 0


# --------------------------------------------------------------------------- #
# ⑥ disk watermark: early reclamation + alert (R-P2-18)
# --------------------------------------------------------------------------- #


def _capture_loguru():
    """Attach a WARNING-level loguru sink; returns (records, sink_id)."""
    records: list[str] = []
    sink_id = logger.add(records.append, level="WARNING")
    return records, sink_id


async def test_disk_watermark_triggers_early_reclaim_and_alert(store, tmp_path):
    now = Store.now()
    # Within the normal 30d TTL, so a normal pass keeps them both...
    await _add_response(store, "r_20d", created_at=now - 20 * _DAY)
    await _add_response(store, "r_10d", created_at=now - 10 * _DAY)

    fake_db = tmp_path / "fake.db"
    fake_db.write_bytes(b"x" * 200)  # 2x the 100-byte soft limit

    records, sink_id = _capture_loguru()
    try:
        report = await early_reclaim_if_needed(
            store,
            fake_db,
            DEFAULT_RETENTION_LIMITS,
            soft_limit_bytes=100,
            now=now,
        )
    finally:
        logger.remove(sink_id)

    # ratio 200/100 = 2.0 -> TTLs scaled by 0.5 -> responses TTL = 15d, so the
    # 20-day-old row is reclaimed *early* while the 10-day-old one survives.
    assert report["responses"] == 1
    assert await _count(store, "responses", "response_id='r_10d'") == 1
    assert any("watermark" in message or "early" in message for message in records)


async def test_disk_watermark_under_limit_is_noop(store, tmp_path):
    now = Store.now()
    await _add_response(store, "r_20d", created_at=now - 20 * _DAY)  # would only be early-reclaimed

    fake_db = tmp_path / "fake.db"
    fake_db.write_bytes(b"x" * 50)  # under a 100-byte limit

    records, sink_id = _capture_loguru()
    try:
        report = await early_reclaim_if_needed(
            store,
            fake_db,
            DEFAULT_RETENTION_LIMITS,
            soft_limit_bytes=100,
            now=now,
        )
    finally:
        logger.remove(sink_id)

    assert report == {}
    assert await _count(store, "responses") == 1
    assert not records  # no alert while under the soft limit


def test_scale_limits_shortens_ttls():
    scaled = scale_limits(DEFAULT_RETENTION_LIMITS, 0.5)
    assert scaled.responses_days == 15
    assert scaled.events_days == 3
    assert scaled.background_days == 15
    assert scaled.tool_audit_days == 45
    assert scaled.idempotency_hours == 12
    assert scaled.request_logs_days == 7
    # a factor below 1 must never zero out a TTL (that would disable cleanup)
    tiny = scale_limits(DEFAULT_RETENTION_LIMITS, 0.01)
    assert tiny.responses_days >= 1


async def test_check_disk_watermark_reports_ratio(store, tmp_path):
    fake_db = tmp_path / "fake.db"
    fake_db.write_bytes(b"x" * 300)
    records, sink_id = _capture_loguru()
    try:
        ratio = await check_disk_watermark(fake_db, soft_limit_bytes=100)
    finally:
        logger.remove(sink_id)
    assert ratio == pytest.approx(3.0)
    assert records  # alert emitted


# --------------------------------------------------------------------------- #
# regression: T06 request_logs baseline + custom limits honour
# --------------------------------------------------------------------------- #


async def test_custom_limits_override_matrix(store):
    now = Store.now()
    await _add_log(store, "req_10d", ts=now - 10 * _DAY)
    await _add_log(store, "req_20d", ts=now - 20 * _DAY)

    limits = RetentionLimits(request_logs_days=7)  # everything else stays default
    report = await run_full_retention(store, limits, now=now)

    assert report["request_logs"] == 2  # both older than 7d
    assert await _count(store, "request_logs") == 0


# --------------------------------------------------------------------------- #
# regression: TiDB derived-table alias (MySQL 1248)
# --------------------------------------------------------------------------- #


def test_delete_batched_derived_table_has_alias():
    """TiDB requires every derived table to have its own alias (ER 1248).

    ``_delete_batched`` builds ``DELETE ... WHERE id IN (SELECT id FROM
    (SELECT ... LIMIT ?))``.  MySQL / TiDB reject an unaliased derived table;
    SQLite tolerates it, so this only failed on the production TiDB backend.
    """
    import inspect

    from zhongzhuan.store import retention

    src = inspect.getsource(retention._delete_batched)
    # Both the single-column and composite-key branches must alias the subquery.
    assert "LIMIT ?) AS batch" in src
    assert "LIMIT ?)" not in src.replace("LIMIT ?) AS batch", "")
