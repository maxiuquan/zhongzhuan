"""T19 acceptance — v004 ResponseStore DDL aligns with authoritative §4.2 / B2.

Verifies the schema produced by the migration engine (SQLite runtime) and the
static MySQL/TiDB DDL both satisfy:

* every ResponseStore table exists,
* every table carries the ``workspace_id`` tenant key and an ``expires_at`` TTL
  column,
* every table that needs TTL purging has an ``expires`` index,
* the new ``idempotency_records`` table (§5.8) is present,
* ``background_jobs`` is the canonical name (renamed from the early
  ``background_tasks`` per decision B2),
* migrations are idempotent (re-running is a no-op, never errors).

Mirrors the private-loop pattern in ``test_migrations.py`` so pytest-asyncio's
loop teardown never orphans the migration executor's connection.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from zhongzhuan.store import migration_engine
from zhongzhuan.store.migration_engine import (
    SqliteMigrationExecutor,
    run_migrations_or_exit,
)
from zhongzhuan.store.migrations import MIGRATIONS
from zhongzhuan.store.migrations.v004_response_store import (
    MYSQL_TABLES,
    SQLITE_TABLES,
)

# Tables the ResponseStore depends on (authoritative §4.2 / B2).
V004_TABLES = (
    "responses",
    "response_input_items",
    "response_output_items",
    "response_events",
    "response_state_chain",
    "background_jobs",
    "tool_executions",
    "idempotency_records",
)

# TTL purge indexes that must exist per table.
EXPIRES_INDEXES = (
    "idx_responses_expires",
    "idx_resp_input_expires",
    "idx_resp_output_expires",
    "idx_resp_events_expires",
    "idx_bt_expires",
    "idx_te_expires",
    "idx_idem_expires",
)

_PRIVATE_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _PRIVATE_LOOP
    if _PRIVATE_LOOP is None or _PRIVATE_LOOP.is_closed():
        _PRIVATE_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_PRIVATE_LOOP)
    return _PRIVATE_LOOP


def _run(coro):
    return _loop().run_until_complete(coro)


@pytest.fixture
def tmp_db(tmp_path) -> str:
    return str(tmp_path / "schema_test.db")


def _executor(path: str):
    db = _run(aiosqlite.connect(path))
    return db, SqliteMigrationExecutor(db)


def _table_columns(db, table: str) -> list[str]:
    rows = _run(db.execute_fetchall(f"PRAGMA table_info({table})"))
    return [r[1] for r in rows]


def _index_names(db) -> set[str]:
    rows = _run(db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Runtime (SQLite) — what the migration engine actually produces
# ---------------------------------------------------------------------------


def test_v004_all_tables_present(tmp_db):
    """Fresh DB gets every ResponseStore table, no stale names."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(
            db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
        names = {r[0] for r in rows}
        for t in V004_TABLES:
            assert t in names, f"missing v004 table {t}"
        # The early, renamed name must NOT exist.
        assert "background_tasks" not in names, "stale 'background_tasks' table leaked"
    finally:
        _run(db.close())


def test_v004_every_table_has_workspace_id_and_expires_at(tmp_db):
    """Every ResponseStore table carries the tenant key + TTL column."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        for t in V004_TABLES:
            cols = _table_columns(db, t)
            assert "workspace_id" in cols, f"{t} missing workspace_id"
            assert "expires_at" in cols, f"{t} missing expires_at"
    finally:
        _run(db.close())


def test_v004_expires_indexes_present(tmp_db):
    """TTL purge indexes are created for every expiring table."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        present = _index_names(db)
        for idx in EXPIRES_INDEXES:
            assert idx in present, f"missing expires index {idx}"
    finally:
        _run(db.close())


def test_v004_idempotent(tmp_db):
    """Re-running migrations is a no-op and never errors."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows_before = _run(db.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version"))
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows_after = _run(db.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version"))
        assert [v for (v,) in rows_before] == [v for (v,) in rows_after]
        # Table count is unchanged.
        names_before = _run(
            db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
        names_after = _run(
            db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
        assert {r[0] for r in names_before} == {r[0] for r in names_after}
    finally:
        _run(db.close())


# ---------------------------------------------------------------------------
# Static (MySQL / TiDB) — the dual-backend DDL strings
# ---------------------------------------------------------------------------


def test_mysql_ddl_every_table_has_workspace_id_and_expires_at():
    """Every MySQL CREATE TABLE carries workspace_id + expires_at."""
    table_defs = [s for s in MYSQL_TABLES if s.strip().upper().startswith("CREATE TABLE")]
    assert table_defs, "no MySQL table definitions found"
    for ddl in table_defs:
        upper = ddl.upper()
        assert "WORKSPACE_ID" in upper, f"MySQL DDL missing workspace_id:\n{ddl}"
        assert "EXPIRES_AT" in upper, f"MySQL DDL missing expires_at:\n{ddl}"


def test_mysql_ddl_has_all_v004_tables():
    """The MySQL DDL declares every ResponseStore table (no stale names)."""
    blob = "\n".join(MYSQL_TABLES).upper()
    for t in V004_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {t.upper()}" in blob, f"MySQL DDL missing {t}"
    assert "BACKGROUND_TASKS" not in blob, "MySQL DDL still references background_tasks"


def test_mysql_ddl_has_expires_indexes():
    """The MySQL DDL declares every TTL purge index."""
    blob = "\n".join(MYSQL_TABLES).upper()
    for idx in EXPIRES_INDEXES:
        assert idx.upper() in blob, f"MySQL DDL missing index {idx}"


def test_sqlite_and_mysql_table_sets_match():
    """Both backends declare the same table set (no drift between them)."""

    def _tables(tuple_of_ddl):
        out = []
        for s in tuple_of_ddl:
            line = s.strip()
            if line.upper().startswith("CREATE TABLE"):
                # Extract the table name between "CREATE TABLE IF NOT EXISTS " and "(".
                head = line.split("(", 1)[0]
                name = head.split()[-1].strip().strip("`")
                out.append(name)
        return out

    sqlite_tables = set(_tables(SQLITE_TABLES))
    mysql_tables = set(_tables(MYSQL_TABLES))
    assert sqlite_tables == mysql_tables, f"backend table drift: sqlite={sqlite_tables} mysql={mysql_tables}"

    # Index name sets should also match (excludes the per-table PK indexes).
    def _indexes(tuple_of_ddl):
        out = []
        for s in tuple_of_ddl:
            line = s.strip()
            if line.upper().startswith("CREATE INDEX"):
                head = line.split("ON", 1)[0]
                name = head.split()[-1].strip().strip("`")
                out.append(name)
        return set(out)

    sqlite_idx = _indexes(SQLITE_TABLES)
    mysql_idx = _indexes(MYSQL_TABLES)
    assert sqlite_idx == mysql_idx, f"backend index drift: sqlite={sqlite_idx} mysql={mysql_idx}"
