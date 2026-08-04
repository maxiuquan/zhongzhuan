"""v004 -- Responses persistable store tables (T16, §4.2.2 / §4.2.3).

Adds the persistence layer required by the Responses resource endpoints and
the state chain / background machinery:

``responses``
    The response object: id, status, lifecycle timestamps, model, the
    sanitized request, usage, error / incomplete details, terminal reason,
    previous-response parent id, background flag, and tenant/workspace key.

``response_input_items``
    The normalized input items for a response (id, seq, type, role, payload).

``response_output_items``
    The output items for a response (id, seq, output_index, type, role, payload).

``response_events``
    Append-only Responses SSE event log with a monotonic ``seq`` (for
    catch-up streams and debug replay, §4.2.8 / §11.3).

``response_state_chain``
    previous_response_id parent links for state-chain loop detection (§9.4).

``background_jobs``
    Background task state machine (queued -> in_progress -> terminal) with
    lease/heartbeat, cancel flag and budget counters (§4.2.4).

``tool_executions``
    Recorded tool execution state: idempotency key, status, approval, result
    digest, cost/max-tries budget (§4.2.6 / §9.4).

``idempotency_records``
    Client-side idempotency keys: request digest, bound response id, state
    (in_flight/done/conflict) with TTL (§9.4 / §5.8).

Design rules (T19 alignment with authoritative DDL §4.2 / B2 decision):
* **``workspace_id``** is the tenant key on every table (renamed from the
  early ``tenant_id`` per decision B2 -- single range scan isolates tenants).
* Every table carries an **``expires_at``** TTL column and an ``expires`` index
  so retention can purge without per-table logic.
* ``seq`` on ``response_events`` is append-only and monotonic per response.
* Reasoning raw text is **never** persisted: the store columns only reference
  the response id, and the item payload redaction is enforced by the
  item_registry before insertion (R-P0-14 / R-P1-29 / R-P1-40).
* ``payload`` is stored as JSON text (SQLite-compatible) so both the SQLite
  and TiDB backends can share one schema.
"""

from __future__ import annotations

from ..migration_engine import Migration

# ---------------------------------------------------------------------------
# SQLite DDL
# ---------------------------------------------------------------------------

SQLITE_TABLES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS responses (
        response_id      TEXT PRIMARY KEY,
        workspace_id     TEXT NOT NULL DEFAULT '',
        status           TEXT NOT NULL DEFAULT 'queued',
        model            TEXT NOT NULL DEFAULT '',
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL,
        completed_at     INTEGER NOT NULL DEFAULT 0,
        previous_response_id TEXT NOT NULL DEFAULT '',
        background       INTEGER NOT NULL DEFAULT 0,
        request          TEXT NOT NULL DEFAULT '{}',
        output           TEXT NOT NULL DEFAULT '[]',
        `usage`          TEXT NOT NULL DEFAULT '{}',
        error            TEXT NOT NULL DEFAULT '',
        incomplete_details TEXT NOT NULL DEFAULT '{}',
        terminal_reason  TEXT NOT NULL DEFAULT '',
        cancelled        INTEGER NOT NULL DEFAULT 0,
        expires_at       INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS response_input_items (
        response_id   TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        workspace_id  TEXT NOT NULL DEFAULT '',
        item_type     TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL DEFAULT '',
        payload       TEXT NOT NULL DEFAULT '{}',
        expires_at    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS response_output_items (
        response_id   TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        output_index  INTEGER NOT NULL DEFAULT 0,
        workspace_id  TEXT NOT NULL DEFAULT '',
        item_type     TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL DEFAULT '',
        payload       TEXT NOT NULL DEFAULT '{}',
        expires_at    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, output_index)
    )""",
    """CREATE TABLE IF NOT EXISTS response_events (
        response_id   TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        workspace_id  TEXT NOT NULL DEFAULT '',
        event_type    TEXT NOT NULL DEFAULT '',
        data          TEXT NOT NULL DEFAULT '{}',
        ts            INTEGER NOT NULL DEFAULT 0,
        expires_at    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS response_state_chain (
        response_id   TEXT PRIMARY KEY,
        workspace_id  TEXT NOT NULL DEFAULT '',
        previous_response_id TEXT NOT NULL DEFAULT '',
        depth         INTEGER NOT NULL DEFAULT 0,
        expires_at    INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS background_jobs (
        task_id       TEXT PRIMARY KEY,
        response_id   TEXT NOT NULL DEFAULT '',
        workspace_id  TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'queued',
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL,
        lease_until   INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        max_wall_seconds INTEGER NOT NULL DEFAULT 900,
        max_tool_rounds INTEGER NOT NULL DEFAULT 32,
        attempt        INTEGER NOT NULL DEFAULT 0,
        expires_at    INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS tool_executions (
        execution_id  TEXT PRIMARY KEY,
        response_id   TEXT NOT NULL DEFAULT '',
        workspace_id  TEXT NOT NULL DEFAULT '',
        call_id       TEXT NOT NULL DEFAULT '',
        tool_name     TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending',
        approval      TEXT NOT NULL DEFAULT '',
        result_digest TEXT NOT NULL DEFAULT '',
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL,
        expires_at    INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS idempotency_records (
        workspace_id    TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL,
        request_digest  TEXT NOT NULL DEFAULT '',
        response_id     TEXT NOT NULL DEFAULT '',
        status_code     INTEGER NOT NULL DEFAULT 0,
        state           TEXT NOT NULL DEFAULT 'in_flight',
        created_at      INTEGER NOT NULL DEFAULT 0,
        expires_at      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace_id, idempotency_key)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_responses_ws ON responses(workspace_id, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_responses_prev ON responses(previous_response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_responses_expires ON responses(expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_input_ws ON response_input_items(response_id, workspace_id)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_input_expires ON response_input_items(expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_output_ws ON response_output_items(response_id, workspace_id)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_output_expires ON response_output_items(expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_events_ws ON response_events(response_id, workspace_id)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_events_expires ON response_events(expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_state_chain_ws ON response_state_chain(workspace_id, previous_response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_bt_ws ON background_jobs(workspace_id, status)""",
    """CREATE INDEX IF NOT EXISTS idx_bt_expires ON background_jobs(expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_te_ws ON tool_executions(workspace_id, response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_te_expires ON tool_executions(expires_at)""",
    """CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_records(expires_at)""",
)

# ---------------------------------------------------------------------------
# MySQL / TiDB DDL (parameter types; TEXT is fine for both)
# ---------------------------------------------------------------------------

MYSQL_TABLES: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS responses (
        response_id      VARCHAR(128) PRIMARY KEY,
        workspace_id     VARCHAR(64) NOT NULL DEFAULT '',
        status           VARCHAR(32) NOT NULL DEFAULT 'queued',
        model            VARCHAR(128) NOT NULL DEFAULT '',
        created_at       BIGINT NOT NULL,
        updated_at       BIGINT NOT NULL,
        completed_at     BIGINT NOT NULL DEFAULT 0,
        previous_response_id VARCHAR(128) NOT NULL DEFAULT '',
        background       TINYINT NOT NULL DEFAULT 0,
        request          TEXT NOT NULL,
        output           TEXT NOT NULL,
        `usage`          TEXT NOT NULL,
        error            TEXT NOT NULL DEFAULT '',
        incomplete_details TEXT NOT NULL DEFAULT '',
        terminal_reason  VARCHAR(64) NOT NULL DEFAULT '',
        cancelled        TINYINT NOT NULL DEFAULT 0,
        expires_at       BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS response_input_items (
        response_id   VARCHAR(128) NOT NULL,
        seq           BIGINT NOT NULL,
        workspace_id  VARCHAR(64) NOT NULL DEFAULT '',
        item_type     VARCHAR(64) NOT NULL DEFAULT '',
        role          VARCHAR(32) NOT NULL DEFAULT '',
        payload       TEXT NOT NULL,
        expires_at    BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS response_output_items (
        response_id   VARCHAR(128) NOT NULL,
        seq           BIGINT NOT NULL,
        output_index  BIGINT NOT NULL DEFAULT 0,
        workspace_id  VARCHAR(64) NOT NULL DEFAULT '',
        item_type     VARCHAR(64) NOT NULL DEFAULT '',
        role          VARCHAR(32) NOT NULL DEFAULT '',
        payload       TEXT NOT NULL,
        expires_at    BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, output_index)
    )""",
    """CREATE TABLE IF NOT EXISTS response_events (
        response_id   VARCHAR(128) NOT NULL,
        seq           BIGINT NOT NULL,
        workspace_id  VARCHAR(64) NOT NULL DEFAULT '',
        event_type    VARCHAR(128) NOT NULL DEFAULT '',
        data          TEXT NOT NULL,
        ts            BIGINT NOT NULL DEFAULT 0,
        expires_at    BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS response_state_chain (
        response_id   VARCHAR(128) PRIMARY KEY,
        workspace_id  VARCHAR(64) NOT NULL DEFAULT '',
        previous_response_id VARCHAR(128) NOT NULL DEFAULT '',
        depth         BIGINT NOT NULL DEFAULT 0,
        expires_at    BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS background_jobs (
        task_id       VARCHAR(128) PRIMARY KEY,
        response_id   VARCHAR(128) NOT NULL DEFAULT '',
        workspace_id  VARCHAR(64) NOT NULL DEFAULT '',
        status        VARCHAR(32) NOT NULL DEFAULT 'queued',
        created_at    BIGINT NOT NULL,
        updated_at    BIGINT NOT NULL,
        lease_until   BIGINT NOT NULL DEFAULT 0,
        cancel_requested TINYINT NOT NULL DEFAULT 0,
        max_wall_seconds BIGINT NOT NULL DEFAULT 900,
        max_tool_rounds BIGINT NOT NULL DEFAULT 32,
        attempt       BIGINT NOT NULL DEFAULT 0,
        expires_at    BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS tool_executions (
        execution_id  VARCHAR(128) PRIMARY KEY,
        response_id   VARCHAR(128) NOT NULL DEFAULT '',
        workspace_id  VARCHAR(64) NOT NULL DEFAULT '',
        call_id       VARCHAR(128) NOT NULL DEFAULT '',
        tool_name     VARCHAR(128) NOT NULL DEFAULT '',
        idempotency_key VARCHAR(128) NOT NULL DEFAULT '',
        status        VARCHAR(32) NOT NULL DEFAULT 'pending',
        approval      VARCHAR(32) NOT NULL DEFAULT '',
        result_digest VARCHAR(128) NOT NULL DEFAULT '',
        created_at    BIGINT NOT NULL,
        updated_at    BIGINT NOT NULL,
        expires_at    BIGINT NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS idempotency_records (
        workspace_id    VARCHAR(64) NOT NULL DEFAULT '',
        idempotency_key VARCHAR(255) NOT NULL,
        request_digest  VARCHAR(128) NOT NULL DEFAULT '',
        response_id     VARCHAR(128) NOT NULL DEFAULT '',
        status_code     INT NOT NULL DEFAULT 0,
        state           VARCHAR(32) NOT NULL DEFAULT 'in_flight',
        created_at      BIGINT NOT NULL DEFAULT 0,
        expires_at      BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace_id, idempotency_key)
    )""",
    """CREATE INDEX idx_responses_ws ON responses(workspace_id, created_at)""",
    """CREATE INDEX idx_responses_prev ON responses(previous_response_id)""",
    """CREATE INDEX idx_responses_expires ON responses(expires_at)""",
    """CREATE INDEX idx_resp_input_ws ON response_input_items(response_id, workspace_id)""",
    """CREATE INDEX idx_resp_input_expires ON response_input_items(expires_at)""",
    """CREATE INDEX idx_resp_output_ws ON response_output_items(response_id, workspace_id)""",
    """CREATE INDEX idx_resp_output_expires ON response_output_items(expires_at)""",
    """CREATE INDEX idx_resp_events_ws ON response_events(response_id, workspace_id)""",
    """CREATE INDEX idx_resp_events_expires ON response_events(expires_at)""",
    """CREATE INDEX idx_state_chain_ws ON response_state_chain(workspace_id, previous_response_id)""",
    """CREATE INDEX idx_bt_ws ON background_jobs(workspace_id, status)""",
    """CREATE INDEX idx_bt_expires ON background_jobs(expires_at)""",
    """CREATE INDEX idx_te_ws ON tool_executions(workspace_id, response_id)""",
    """CREATE INDEX idx_te_expires ON tool_executions(expires_at)""",
    """CREATE INDEX idx_idem_expires ON idempotency_records(expires_at)""",
)


MIGRATION = Migration(
    version=4,
    name="response_store",
    sqlite_sql=SQLITE_TABLES,
    mysql_sql=MYSQL_TABLES,
    sqlite_baseline_sql=SQLITE_TABLES,
    mysql_baseline_sql=MYSQL_TABLES,
    baseline_probe="responses",
)
