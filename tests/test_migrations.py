"""Tests for the versioned migration engine (T03 / R-P0-04 / R-P0-05)."""
from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from zhongzhuan.store import migration_engine
from zhongzhuan.store.migration_engine import (
    MIGRATION_EXIT_CODE,
    SqliteMigrationExecutor,
    run_migrations_or_exit,
)
from zhongzhuan.store.migrations import MIGRATIONS


# pytest-asyncio (auto mode) creates and *closes* an event loop for its own
# async tests.  That would orphan ``asyncio.get_event_loop()`` from the
# migration tests' module-level loop mid-suite.  Keep a private loop that is
# independent of pytest-asyncio's, so the sync helpers below never depend on
# whatever loop the runner happens to have torn down.
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
    return str(tmp_path / "test.db")


def _executor(path: str):
    """Open a fresh aiosqlite connection wrapped in a SqliteMigrationExecutor."""
    db = _run(aiosqlite.connect(path))
    return db, SqliteMigrationExecutor(db)


def test_migrations_apply_in_order(tmp_db):
    """v001 then v003 both apply; schema_migrations has versions 1 and 3."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(db.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version"))
        names = _run(db.execute_fetchall("SELECT name FROM schema_migrations ORDER BY version"))
        assert [v for v, in rows] == [1, 3]
        assert [n for n, in names] == ["baseline", "token_hash"]
    finally:
        _run(db.close())


def test_migrations_idempotent(tmp_db):
    """A second run must not re-apply or error."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(db.execute_fetchall("SELECT version FROM schema_migrations"))
        assert len(rows) == 2
    finally:
        _run(db.close())


def test_v001_creates_tables(tmp_db):
    """Fresh DB gets all core tables."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ))
        names = {r[0] for r in rows}
        for t in ("models", "api_keys", "request_logs", "access_tokens", "admin_users",
                  "system_config", "key_health", "schema_migrations"):
            assert t in names, f"missing table {t}"
    finally:
        _run(db.close())


def test_migration_failure_exits(tmp_db):
    """A failing migration raises SystemExit (not silently swallowed)."""
    bad = migration_engine.Migration(
        version=999,
        name="broken",
        sqlite_sql=("THIS IS NOT VALID SQL",),
        sqlite_baseline_sql=("THIS IS NOT VALID SQL",),
    )
    db, ex = _executor(tmp_db)
    try:
        with pytest.raises(SystemExit) as ei:
            _run(run_migrations_or_exit(
                ex,
                list(MIGRATIONS) + [bad],
                sqlite_db_path=tmp_db,
            ))
        assert ei.value.code == MIGRATION_EXIT_CODE
    finally:
        _run(db.close())


def test_v003_hashes_legacy_tokens(tmp_db):
    """v003 hashes remaining plaintext tokens and clears the plaintext column."""
    # Seed a database with the baseline schema + a legacy plaintext token.
    db1, ex1 = _executor(tmp_db)
    _run(run_migrations_or_exit(ex1, [MIGRATIONS[0]], sqlite_db_path=tmp_db))
    _run(db1.execute("INSERT INTO access_tokens (token, label, enabled, created_at) VALUES (?, ?, 1, ?)",
                     ("zz-legacy-plaintext-token", "legacy", 1)))
    _run(db1.commit())
    _run(db1.close())

    # Now run the full registry (v003 runs, baseline mode because schema_migrations exists).
    db2, ex2 = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex2, MIGRATIONS, sqlite_db_path=tmp_db))
        async def _query():
            cur = await db2.execute(
                "SELECT token, token_prefix, token_hash FROM access_tokens WHERE label='legacy'"
            )
            return await cur.fetchone()
        row = _run(_query())
        assert row is not None
        token, prefix, digest = row
        assert token == ""  # plaintext cleared
        assert prefix == "zz-legac"  # first 8 chars (TOKEN_PREFIX_LEN)
        assert digest  # hashed
    finally:
        _run(db2.close())