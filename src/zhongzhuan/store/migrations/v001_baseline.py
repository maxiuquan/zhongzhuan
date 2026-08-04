"""v001 -- baseline schema (T03).

Consolidates the ten legacy tables plus the fifteen ad-hoc ``ALTER TABLE``
statements that used to live in ``store/schema.py`` (`:6-22` / `:24-40`) into
one versioned migration.

Legacy database compatibility
-----------------------------
A database that already contains ``models`` but has no ``schema_migrations``
table predates the migration engine.  For those the runner switches to
*baseline mode*: the ``CREATE TABLE`` DDL is **not** replayed, only the
provably idempotent repair statements (``ADD COLUMN`` / ``CREATE INDEX``) run,
and the version is recorded with ``status='baselined'``.

Why the repair statements still run in baseline mode: the pre-engine code
executed all fifteen ALTERs on *every* boot.  A legacy database that was
created by an older build may therefore be missing columns.  Recording v001 as
applied without those repairs would turn a silently-working install into a
``no such column`` crash.  ``ADD COLUMN`` is covered by the duplicate-column
whitelist, so replaying it costs nothing on an already-complete schema.

MySQL / TiDB note
-----------------
``CREATE INDEX IF NOT EXISTS`` is **not** valid MySQL/TiDB syntax (this was the
latent startup bug at ``schema.py:269``).  The MySQL variant therefore uses a
plain ``CREATE INDEX`` and relies on the engine tolerating errno 1061
(``ER_DUP_KEYNAME``).
"""

from __future__ import annotations

from ..migration_engine import Migration

# --------------------------------------------------------------------------- #
# SQLite
# --------------------------------------------------------------------------- #
SQLITE_TABLES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    upstream_base TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    rpm_limit     INTEGER NOT NULL DEFAULT 0,
    tpm_limit     INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    weight        INTEGER NOT NULL DEFAULT 1,
    protocol      TEXT NOT NULL DEFAULT 'openai',
    anthropic_version TEXT NOT NULL DEFAULT '2023-06-01',
    max_tokens_default INTEGER NOT NULL DEFAULT 4096,
    upstream_path_override TEXT NOT NULL DEFAULT '',
    is_fallback   INTEGER NOT NULL DEFAULT 0,
    aliases       TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS model_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    strategy        TEXT NOT NULL CHECK(strategy IN ('round_robin','weighted','failover')),
    fallback_enabled INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS group_models (
    group_id  INTEGER NOT NULL REFERENCES model_groups(id) ON DELETE CASCADE,
    model_id  INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    weight    INTEGER NOT NULL DEFAULT 1,
    ord       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, model_id)
)""",
    """CREATE TABLE IF NOT EXISTS request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    client_ip   TEXT,
    model_name  TEXT NOT NULL,
    resolved_model_id INTEGER,
    key_id      INTEGER,
    status      INTEGER NOT NULL,
    latency_ms  INTEGER NOT NULL,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    error       TEXT DEFAULT '',
    request_id  TEXT NOT NULL,
    inbound_protocol TEXT DEFAULT '',
    outbound_protocol TEXT DEFAULT '',
    translated  INTEGER DEFAULT 0,
    token_id    INTEGER DEFAULT 0,
    cost        REAL DEFAULT 0
)""",
    """CREATE TABLE IF NOT EXISTS system_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS access_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL UNIQUE,
    label           TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    quota_tokens    INTEGER NOT NULL DEFAULT -1,
    used_tokens     INTEGER NOT NULL DEFAULT 0,
    model_whitelist TEXT NOT NULL DEFAULT '',
    expires_at      INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS model_pricing (
    model_name          TEXT PRIMARY KEY,
    input_price_per_1k  REAL NOT NULL DEFAULT 0,
    output_price_per_1k REAL NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'CNY',
    updated_at          INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS key_health (
    key_id          INTEGER PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'healthy',
    cooldown_until  REAL NOT NULL DEFAULT 0,
    rpm_limit       INTEGER NOT NULL DEFAULT 0,
    tpm_limit       INTEGER NOT NULL DEFAULT 0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    recent_429_count INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL
)""",
)

SQLITE_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts)",
    "CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model_name, ts)",
)

#: The fifteen legacy ``ALTER TABLE`` statements (former ``schema.py:6-22``).
#: Every one of them is an ``ADD COLUMN`` and therefore covered by the
#: duplicate-column whitelist of the migration engine.
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN protocol TEXT NOT NULL DEFAULT 'openai'",
    "ALTER TABLE models ADD COLUMN anthropic_version TEXT NOT NULL DEFAULT '2023-06-01'",
    "ALTER TABLE models ADD COLUMN max_tokens_default INTEGER NOT NULL DEFAULT 4096",
    "ALTER TABLE models ADD COLUMN upstream_path_override TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN is_fallback INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE models ADD COLUMN aliases TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE request_logs ADD COLUMN inbound_protocol TEXT DEFAULT ''",
    "ALTER TABLE request_logs ADD COLUMN outbound_protocol TEXT DEFAULT ''",
    "ALTER TABLE request_logs ADD COLUMN translated INTEGER DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN quota_tokens BIGINT NOT NULL DEFAULT -1",
    "ALTER TABLE access_tokens ADD COLUMN used_tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN model_whitelist TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE access_tokens ADD COLUMN expires_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE request_logs ADD COLUMN token_id INTEGER DEFAULT 0",
    "ALTER TABLE request_logs ADD COLUMN cost REAL DEFAULT 0",
)

# --------------------------------------------------------------------------- #
# MySQL / TiDB
# --------------------------------------------------------------------------- #
MYSQL_TABLES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS models (
    id            INT PRIMARY KEY AUTO_INCREMENT,
    name          VARCHAR(128) NOT NULL UNIQUE,
    upstream_base VARCHAR(512) NOT NULL,
    upstream_model VARCHAR(128) NOT NULL,
    rpm_limit     INT NOT NULL DEFAULT 0,
    tpm_limit     INT NOT NULL DEFAULT 0,
    enabled       TINYINT NOT NULL DEFAULT 1,
    weight        INT NOT NULL DEFAULT 1,
    protocol      VARCHAR(16) NOT NULL DEFAULT 'openai',
    anthropic_version VARCHAR(32) NOT NULL DEFAULT '2023-06-01',
    max_tokens_default INT NOT NULL DEFAULT 4096,
    upstream_path_override VARCHAR(512) NOT NULL DEFAULT '',
    is_fallback   TINYINT NOT NULL DEFAULT 0,
    aliases       VARCHAR(512) NOT NULL DEFAULT '',
    created_at    BIGINT NOT NULL,
    updated_at    BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS api_keys (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    model_id    INT NOT NULL,
    label       VARCHAR(128) NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,
    enabled     TINYINT NOT NULL DEFAULT 1,
    priority    INT NOT NULL DEFAULT 0,
    created_at  BIGINT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_groups (
    id              INT PRIMARY KEY AUTO_INCREMENT,
    name            VARCHAR(128) NOT NULL UNIQUE,
    strategy        VARCHAR(32) NOT NULL,
    fallback_enabled TINYINT NOT NULL DEFAULT 1,
    created_at      BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS group_models (
    group_id  INT NOT NULL,
    model_id  INT NOT NULL,
    weight    INT NOT NULL DEFAULT 1,
    ord       INT NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, model_id),
    FOREIGN KEY (group_id) REFERENCES model_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS request_logs (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    ts          BIGINT NOT NULL,
    client_ip   VARCHAR(64),
    model_name  VARCHAR(128) NOT NULL,
    resolved_model_id INT,
    key_id      INT,
    status      INT NOT NULL,
    latency_ms  INT NOT NULL,
    tokens_in   INT DEFAULT 0,
    tokens_out  INT DEFAULT 0,
    error       TEXT,
    request_id  VARCHAR(64) NOT NULL,
    inbound_protocol VARCHAR(16) DEFAULT '',
    outbound_protocol VARCHAR(16) DEFAULT '',
    translated  TINYINT DEFAULT 0,
    token_id    INT DEFAULT 0,
    cost        DOUBLE DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS system_config (
    `key`   VARCHAR(64) PRIMARY KEY,
    `value` TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS admin_users (
    id            INT PRIMARY KEY AUTO_INCREMENT,
    username      VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    created_at    BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS access_tokens (
    id              INT PRIMARY KEY AUTO_INCREMENT,
    token           VARCHAR(128) NOT NULL UNIQUE,
    label           VARCHAR(64) NOT NULL DEFAULT '',
    enabled         TINYINT NOT NULL DEFAULT 1,
    quota_tokens    BIGINT NOT NULL DEFAULT -1,
    used_tokens     BIGINT NOT NULL DEFAULT 0,
    model_whitelist VARCHAR(512) NOT NULL DEFAULT '',
    expires_at      BIGINT NOT NULL DEFAULT 0,
    created_at      BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS model_pricing (
    model_name          VARCHAR(128) PRIMARY KEY,
    input_price_per_1k  DOUBLE NOT NULL DEFAULT 0,
    output_price_per_1k DOUBLE NOT NULL DEFAULT 0,
    currency            VARCHAR(8) NOT NULL DEFAULT 'CNY',
    updated_at          BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS key_health (
    key_id          INT PRIMARY KEY,
    status          VARCHAR(16) NOT NULL DEFAULT 'healthy',
    cooldown_until  DOUBLE NOT NULL DEFAULT 0,
    rpm_limit       INT NOT NULL DEFAULT 0,
    tpm_limit       INT NOT NULL DEFAULT 0,
    success_count   INT NOT NULL DEFAULT 0,
    failure_count   INT NOT NULL DEFAULT 0,
    recent_429_count INT NOT NULL DEFAULT 0,
    updated_at      BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)

#: Plain ``CREATE INDEX`` -- MySQL/TiDB reject ``IF NOT EXISTS`` here.
#: Re-running is tolerated through errno 1061 (ER_DUP_KEYNAME).
MYSQL_INDEXES: tuple[str, ...] = (
    "CREATE INDEX idx_logs_ts ON request_logs(ts)",
    "CREATE INDEX idx_logs_model ON request_logs(model_name, ts)",
)

#: Former ``schema.py:24-40``.  ``IF NOT EXISTS`` on ``ADD COLUMN`` is a MariaDB
#: extension that TiDB does not implement, so it is dropped here and the engine
#: tolerates errno 1060 (ER_DUP_FIELDNAME) instead.
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN protocol VARCHAR(16) NOT NULL DEFAULT 'openai'",
    "ALTER TABLE models ADD COLUMN anthropic_version VARCHAR(32) NOT NULL DEFAULT '2023-06-01'",
    "ALTER TABLE models ADD COLUMN max_tokens_default INT NOT NULL DEFAULT 4096",
    "ALTER TABLE models ADD COLUMN upstream_path_override VARCHAR(512) NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN is_fallback TINYINT NOT NULL DEFAULT 0",
    "ALTER TABLE models ADD COLUMN aliases VARCHAR(512) NOT NULL DEFAULT ''",
    "ALTER TABLE request_logs ADD COLUMN inbound_protocol VARCHAR(16) DEFAULT ''",
    "ALTER TABLE request_logs ADD COLUMN outbound_protocol VARCHAR(16) DEFAULT ''",
    "ALTER TABLE request_logs ADD COLUMN translated TINYINT DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN quota_tokens BIGINT NOT NULL DEFAULT -1",
    "ALTER TABLE access_tokens ADD COLUMN used_tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE access_tokens ADD COLUMN model_whitelist VARCHAR(512) NOT NULL DEFAULT ''",
    "ALTER TABLE access_tokens ADD COLUMN expires_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE request_logs ADD COLUMN token_id INT DEFAULT 0",
    "ALTER TABLE request_logs ADD COLUMN cost DOUBLE DEFAULT 0",
)

# --------------------------------------------------------------------------- #
# Backwards compatible schema strings (kept for ``store/schema.py``)
# --------------------------------------------------------------------------- #
SQLITE_SCHEMA: str = "\n".join(f"{s};" for s in SQLITE_TABLES + SQLITE_INDEXES)
MYSQL_SCHEMA: str = "\n".join(f"{s};" for s in MYSQL_TABLES + MYSQL_INDEXES)

MIGRATION = Migration(
    version=1,
    name="baseline",
    sqlite_sql=SQLITE_TABLES + SQLITE_INDEXES + SQLITE_ALTERS,
    mysql_sql=MYSQL_TABLES + MYSQL_INDEXES + MYSQL_ALTERS,
    # Baseline mode: never replay CREATE TABLE, only idempotent repairs.
    sqlite_baseline_sql=SQLITE_ALTERS + SQLITE_INDEXES,
    mysql_baseline_sql=MYSQL_ALTERS + MYSQL_INDEXES,
    baseline_probe="models",
)
