"""v003 -- hash access tokens (T04).

Turns the legacy plaintext ``access_tokens.token`` column into the hashed
schema used by ``store/access_tokens.py``:

    token_prefix  -- first 8 chars, the lookup index + display suffix
    token_hash    -- HMAC-SHA256 hex digest, compared in constant time
    rotation_of / last_used_at / created_by / revoked_at / revoked_by -- audit

Data migration: existing plaintext tokens are hashed into the new columns and
the plaintext column is then cleared.  The HMAC key is resolved through the
same code path as normal token creation (``resolve_hmac_key``), so new and
legacy tokens share one key space.

The migration uses a ``hook`` because the hash step cannot be expressed as
static SQL.  ``run_hook_on_baseline=True`` because existing databases are the
*most* likely to hold legacy plaintext rows.

Why the table is rebuilt (SQLite)
---------------------------------
``create_token`` no longer writes the plaintext ``token`` column, so that
column must accept ``NULL`` and must **not** be ``UNIQUE`` (SQLite would reject
a second empty string).  The v001 baseline declares ``token TEXT NOT NULL
UNIQUE``.  SQLite cannot drop a column constraint in place, so the table is
rebuilt with the *final* schema (all seven new columns plus ``token`` nullable
and un-UNIQUE'd).  MySQL / TiDB can drop the constraint in place with
``MODIFY COLUMN``; its ``UNIQUE`` index tolerates multiple ``NULL`` values, so
the index is left in place.
"""

from __future__ import annotations

from ..migration_engine import Migration, MigrationExecutor

#: SQLite rebuild of ``access_tokens``.  SQLite cannot drop NOT NULL / UNIQUE
#: constraints in place, so the table is recreated with the final schema:
#: all seven new columns, and ``token`` nullable without a UNIQUE index.
#: The old table (and its autoindex on ``token``) is dropped by the rename.
SQLITE_REBUILD: tuple[str, ...] = (
    """CREATE TABLE access_tokens_rebuilt (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT,
    token_prefix    TEXT NOT NULL DEFAULT '',
    token_hash      TEXT NOT NULL DEFAULT '',
    label           TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    quota_tokens    INTEGER NOT NULL DEFAULT -1,
    used_tokens     INTEGER NOT NULL DEFAULT 0,
    model_whitelist TEXT NOT NULL DEFAULT '',
    expires_at      INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    rotation_of     INTEGER NOT NULL DEFAULT 0,
    last_used_at    INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL DEFAULT '',
    revoked_at      INTEGER NOT NULL DEFAULT 0,
    revoked_by      TEXT NOT NULL DEFAULT ''
)""",
    # Only the pre-v003 columns are copied; every new column takes its
    # DEFAULT ('' / 0).  The hook hashes the plaintext ``token`` afterwards.
    """INSERT INTO access_tokens_rebuilt (
        id, token, label, enabled, quota_tokens, used_tokens,
        model_whitelist, expires_at, created_at
    )
    SELECT
        id, token, label, enabled, quota_tokens, used_tokens,
        model_whitelist, expires_at, created_at
    FROM access_tokens""",
    "DROP TABLE access_tokens",
    "ALTER TABLE access_tokens_rebuilt RENAME TO access_tokens",
)

#: MySQL / TiDB: add the new columns, then open the ``token`` column so the
#: hashed code path can write it as NULL.  ``token`` is UNIQUE in the baseline;
#: MySQL tolerates multiple NULLs in a UNIQUE index, so the index is retained.
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE access_tokens ADD COLUMN token_prefix VARCHAR(16) NOT NULL DEFAULT ''",
    "ALTER TABLE access_tokens ADD COLUMN token_hash VARCHAR(128) NOT NULL DEFAULT ''",
    "ALTER TABLE access_tokens ADD COLUMN rotation_of INT NOT NULL DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN last_used_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN created_by VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE access_tokens ADD COLUMN revoked_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN revoked_by VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE access_tokens MODIFY COLUMN token VARCHAR(128) NULL",
)


async def _hash_legacy_tokens(ex: MigrationExecutor) -> None:
    """Hash remaining plaintext tokens and clear the plaintext column.

    Idempotent: rows whose ``token_hash`` is already set are skipped, and a
    second run finds nothing left to do.
    """
    # Resolve the HMAC key the same way access_tokens.py does.
    from ..access_tokens import resolve_hmac_key, token_prefix_of, hash_token

    # Access-token helpers (resolve_hmac_key / _read_config_key / _write_config_key)
    # accept any object exposing async ``fetchone(sql, params)`` and
    # ``execute(sql, params)`` -- the migration executor qualifies.
    key = await resolve_hmac_key(ex)

    rows = await ex.fetchall(
        "SELECT id, token FROM access_tokens "
        "WHERE token IS NOT NULL AND token != '' AND token_hash = ''"
    )
    for row_id, plaintext in rows:
        if not plaintext:
            continue
        prefix = token_prefix_of(plaintext)
        digest = hash_token(plaintext, key) if key else ""
        await ex.execute(
            "UPDATE access_tokens SET token_prefix=?, token_hash=?, token='' "
            "WHERE id=?",
            (prefix, digest, row_id),
        )
    # Final safety: clear any remaining plaintext so it never survives on disk.
    await ex.execute(
        "UPDATE access_tokens SET token='' "
        "WHERE token IS NOT NULL AND token != '' AND token_hash != ''"
    )


MIGRATION = Migration(
    version=3,
    name="token_hash",
    sqlite_sql=SQLITE_REBUILD,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_REBUILD,
    mysql_baseline_sql=MYSQL_ALTERS,
    hook=_hash_legacy_tokens,
    run_hook_on_baseline=True,
    baseline_probe="access_tokens",
)