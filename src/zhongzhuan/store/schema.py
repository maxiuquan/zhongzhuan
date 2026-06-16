"""SQLite and MySQL/TiDB schema strings."""

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    upstream_base TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    rpm_limit     INTEGER NOT NULL DEFAULT 0,
    tpm_limit     INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    weight        INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    strategy        TEXT NOT NULL CHECK(strategy IN ('round_robin','weighted','failover')),
    fallback_enabled INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_models (
    group_id  INTEGER NOT NULL REFERENCES model_groups(id) ON DELETE CASCADE,
    model_id  INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    weight    INTEGER NOT NULL DEFAULT 1,
    ord       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, model_id)
);

CREATE TABLE IF NOT EXISTS request_logs (
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
    request_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS access_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model_name, ts);
"""

MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id            INT PRIMARY KEY AUTO_INCREMENT,
    name          VARCHAR(128) NOT NULL UNIQUE,
    upstream_base VARCHAR(512) NOT NULL,
    upstream_model VARCHAR(128) NOT NULL,
    rpm_limit     INT NOT NULL DEFAULT 0,
    tpm_limit     INT NOT NULL DEFAULT 0,
    enabled       TINYINT NOT NULL DEFAULT 1,
    weight        INT NOT NULL DEFAULT 1,
    created_at    BIGINT NOT NULL,
    updated_at    BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_keys (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    model_id    INT NOT NULL,
    label       VARCHAR(128) NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,
    enabled     TINYINT NOT NULL DEFAULT 1,
    priority    INT NOT NULL DEFAULT 0,
    created_at  BIGINT NOT NULL,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS model_groups (
    id              INT PRIMARY KEY AUTO_INCREMENT,
    name            VARCHAR(128) NOT NULL UNIQUE,
    strategy        VARCHAR(32) NOT NULL,
    fallback_enabled TINYINT NOT NULL DEFAULT 1,
    created_at      BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS group_models (
    group_id  INT NOT NULL,
    model_id  INT NOT NULL,
    weight    INT NOT NULL DEFAULT 1,
    ord       INT NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, model_id),
    FOREIGN KEY (group_id) REFERENCES model_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS request_logs (
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
    request_id  VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS system_config (
    `key`   VARCHAR(64) PRIMARY KEY,
    `value` TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS admin_users (
    id            INT PRIMARY KEY AUTO_INCREMENT,
    username      VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    created_at    BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS access_tokens (
    id         INT PRIMARY KEY AUTO_INCREMENT,
    token      VARCHAR(128) NOT NULL UNIQUE,
    label      VARCHAR(64) NOT NULL DEFAULT '',
    enabled    TINYINT NOT NULL DEFAULT 1,
    created_at BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model_name, ts);
"""