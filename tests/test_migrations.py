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


def test_mysql_index_ddl_uses_supported_syntax():
    """MySQL/TiDB rejects SQLite's CREATE INDEX IF NOT EXISTS form."""
    for migration in MIGRATIONS:
        statements = migration.mysql_sql + migration.mysql_baseline_sql
        for sql in statements:
            normalized = " ".join(sql.upper().split())
            if normalized.startswith("CREATE INDEX"):
                assert not normalized.startswith("CREATE INDEX IF NOT EXISTS"), (
                    f"v{migration.version:03d} contains unsupported MySQL index DDL: {sql}"
                )


def test_mysql_lob_columns_do_not_declare_defaults():
    """MySQL/TiDB rejects defaults on TEXT, BLOB, and JSON columns."""
    lob_types = (" TEXT ", " BLOB ", " JSON ")
    for migration in MIGRATIONS:
        statements = migration.mysql_sql + migration.mysql_baseline_sql
        for sql in statements:
            for line in sql.upper().splitlines():
                normalized = f" {' '.join(line.split())} "
                if any(lob_type in normalized for lob_type in lob_types):
                    assert " DEFAULT " not in normalized, (
                        f"v{migration.version:03d} contains unsupported MySQL LOB default: {line.strip()}"
                    )


def test_migrations_apply_in_order(tmp_db):
    """v001, v003, v004 apply in order; schema_migrations records all."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(db.execute_fetchall("SELECT version FROM schema_migrations ORDER BY version"))
        names = _run(db.execute_fetchall("SELECT name FROM schema_migrations ORDER BY version"))
        assert [v for (v,) in rows] == [1, 3, 4, 5, 6, 7, 8]
        assert [n for (n,) in names] == [
            "baseline",
            "token_hash",
            "response_store",
            "model_capabilities",
            "tool_executions",
            "schema_realign",
            "route_bindings",
        ]
    finally:
        _run(db.close())


def test_migrations_idempotent(tmp_db):
    """A second run must not re-apply or error."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(db.execute_fetchall("SELECT version FROM schema_migrations"))
        assert len(rows) == len(MIGRATIONS)
    finally:
        _run(db.close())


def test_v001_creates_tables(tmp_db):
    """Fresh DB gets all core tables."""
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        rows = _run(
            db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        )
        names = {r[0] for r in rows}
        for t in (
            "models",
            "api_keys",
            "request_logs",
            "access_tokens",
            "admin_users",
            "system_config",
            "key_health",
            "schema_migrations",
        ):
            assert t in names, f"missing table {t}"
        # v004 response-store tables.
        for t in (
            "responses",
            "response_input_items",
            "response_output_items",
            "response_events",
            "response_state_chain",
            "background_jobs",
            "tool_executions",
            "idempotency_records",
        ):
            assert t in names, f"missing v004 table {t}"
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
            _run(
                run_migrations_or_exit(
                    ex,
                    list(MIGRATIONS) + [bad],
                    sqlite_db_path=tmp_db,
                )
            )
        assert ei.value.code == MIGRATION_EXIT_CODE
    finally:
        _run(db.close())


def test_v006_survives_legacy_tool_executions_without_workspace_id(tmp_db):
    """回归锁：v006 必须能在「旧 v004 残留的 tool_executions（无 workspace_id）」

    上完成迁移，而不是让 ``CREATE INDEX ... (workspace_id, approval)`` 报
    ``no such column: workspace_id`` 进而触发 :data:`MIGRATION_EXIT_CODE` 把
    服务拒启动（T26 真实升级路径 BUG 的根因）。

    复现方式：先只跑 v001..v005，把当前 v004 建出的 ``workspace_id`` 列改名成
    ``tenant_id`` —— 等价于「曾经被 B2 改名前的旧 v004 迁移过、而迁移引擎只比对
    ``version`` 不比对 ``sql_digest``、旧表形永远不会被 ``CREATE TABLE IF NOT
    EXISTS`` 改形」的真实部署缓存。再跑完整迁移，断言 v006 自愈、服务可启动。
    """
    # 1) 只应用到 v005（不含 v006），得到当前形态的 tool_executions。
    db, ex = _executor(tmp_db)
    _run(run_migrations_or_exit(ex, list(MIGRATIONS[:4]), sqlite_db_path=tmp_db))
    _run(db.close())

    # 2) 把 workspace_id 退化成旧 v004 的 tenant_id（移除 workspace_id 列）。
    db2, _ = _executor(tmp_db)
    try:
        _run(db2.execute("ALTER TABLE tool_executions RENAME COLUMN workspace_id TO tenant_id"))
        # expires_at 在旧形态里也可能缺失：尽量删掉以贴近真实场景；
        # 不支持 DROP COLUMN 的旧 SQLite 上跳过（不影响 workspace_id 验证）。
        try:
            _run(db2.execute("ALTER TABLE tool_executions DROP COLUMN expires_at"))
        except Exception:  # pragma: no cover - 依赖 SQLite 版本
            pass
        _run(db2.commit())
    finally:
        _run(db2.close())

    # 3) 断言此时确实处于「有表、无 workspace_id」的脆弱状态。
    db3, _ = _executor(tmp_db)
    try:
        cols = _run(db3.execute_fetchall("PRAGMA table_info(tool_executions)"))
        col_names = {r[1] for r in cols}
        assert "tenant_id" in col_names
        assert "workspace_id" not in col_names
    finally:
        _run(db3.close())

    # 4) 跑完整迁移：v006 必须自愈，不再把服务打死。
    db4, ex4 = _executor(tmp_db)
    try:
        # run_migrations_or_exit 失败会以 MIGRATION_EXIT_CODE 退出 -> SystemExit。
        _run(run_migrations_or_exit(ex4, MIGRATIONS, sqlite_db_path=tmp_db))

        rows = _run(db4.execute_fetchall("SELECT version FROM schema_migrations"))
        assert {int(v[0]) for v in rows} == {m.version for m in MIGRATIONS}

        cols = _run(db4.execute_fetchall("PRAGMA table_info(tool_executions)"))
        col_names = {r[1] for r in cols}
        assert "workspace_id" in col_names, "v006 必须补回 workspace_id"
        assert "expires_at" in col_names, "v006 必须补回 expires_at"
        assert "tool_seq" in col_names

        idx = _run(db4.execute_fetchall("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_te_approval'"))
        assert idx, "v006 必须建出 idx_te_approval"
    finally:
        _run(db4.close())


# --------------------------------------------------------------------------- #
# v007 -- 历史库 schema 拉平
# --------------------------------------------------------------------------- #
#: c983adf（B2 改名 / v004 被原地重写之前）的 SQLite DDL **原文**。
#:
#: 取自 ``git show c983adf:src/zhongzhuan/store/migrations/v004_response_store.py``，
#: 不是凭记忆重建 —— 这是本回归测试唯一的价值来源：只有真正复刻出老形态，
#: 才能证明 v007 在真实升级路径上有效。
#:
#: 与当前 v004 的差异（=41da9d6 造成的全部破坏面）：
#: ``tenant_id`` 而非 ``workspace_id``；全表无 ``expires_at``；
#: ``background_tasks`` 而非 ``background_jobs``；无 ``idempotency_records``；
#: 索引是 7 条 ``*_tenant`` 形态而非 15 条 ``*_ws`` / ``*_expires``。
_LEGACY_V004_SQLITE: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS responses (
        response_id      TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL DEFAULT '',
        status           TEXT NOT NULL DEFAULT 'queued',
        model            TEXT NOT NULL DEFAULT '',
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL,
        completed_at     INTEGER NOT NULL DEFAULT 0,
        previous_response_id TEXT NOT NULL DEFAULT '',
        background       INTEGER NOT NULL DEFAULT 0,
        request          TEXT NOT NULL DEFAULT '{}',
        output           TEXT NOT NULL DEFAULT '[]',
        usage            TEXT NOT NULL DEFAULT '{}',
        error            TEXT NOT NULL DEFAULT '',
        incomplete_details TEXT NOT NULL DEFAULT '{}',
        terminal_reason  TEXT NOT NULL DEFAULT '',
        cancelled        INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS response_input_items (
        response_id   TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        item_type     TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL DEFAULT '',
        payload       TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (response_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS response_output_items (
        response_id   TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        output_index  INTEGER NOT NULL DEFAULT 0,
        item_type     TEXT NOT NULL DEFAULT '',
        role          TEXT NOT NULL DEFAULT '',
        payload       TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (response_id, output_index)
    )""",
    """CREATE TABLE IF NOT EXISTS response_events (
        response_id   TEXT NOT NULL,
        seq           INTEGER NOT NULL,
        event_type    TEXT NOT NULL DEFAULT '',
        data          TEXT NOT NULL DEFAULT '{}',
        ts            INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (response_id, seq)
    )""",
    """CREATE TABLE IF NOT EXISTS response_state_chain (
        response_id   TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL DEFAULT '',
        previous_response_id TEXT NOT NULL DEFAULT '',
        depth         INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS background_tasks (
        task_id       TEXT PRIMARY KEY,
        response_id   TEXT NOT NULL DEFAULT '',
        tenant_id     TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'queued',
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL,
        lease_until   INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        max_wall_seconds INTEGER NOT NULL DEFAULT 900,
        max_tool_rounds INTEGER NOT NULL DEFAULT 32,
        attempt        INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS tool_executions (
        execution_id  TEXT PRIMARY KEY,
        response_id   TEXT NOT NULL DEFAULT '',
        tenant_id     TEXT NOT NULL DEFAULT '',
        call_id       TEXT NOT NULL DEFAULT '',
        tool_name     TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending',
        approval      TEXT NOT NULL DEFAULT '',
        result_digest TEXT NOT NULL DEFAULT '',
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_responses_tenant ON responses(tenant_id, created_at)""",
    """CREATE INDEX IF NOT EXISTS idx_responses_prev ON responses(previous_response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_input_tenant ON response_input_items(response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_output_tenant ON response_output_items(response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_resp_events_tenant ON response_events(response_id)""",
    """CREATE INDEX IF NOT EXISTS idx_bt_tenant ON background_tasks(tenant_id, status)""",
    """CREATE INDEX IF NOT EXISTS idx_te_tenant ON tool_executions(tenant_id, response_id)""",
)

#: 实测老库里 v004 记录的 digest（旧语句集的 SHA-256）。任意稳定的「非当前
#: digest」都能触发漂移告警，这里用真值以贴近现场。
_LEGACY_V004_DIGEST = "5e883f07f89feeb0ab6f32a4d7a91da70a792eedc91c26e3e461d70ca5427a8f"

#: v004 当前形态应当存在的全部业务表（``background_tasks`` 是保留的废弃表，
#: 不在此列）。``route_bindings`` 由 v008 新增（T35 / R-P1-61）。
_EXPECTED_TABLES: tuple[str, ...] = (
    "responses",
    "response_input_items",
    "response_output_items",
    "response_events",
    "response_state_chain",
    "background_jobs",
    "tool_executions",
    "idempotency_records",
    "route_bindings",
)

#: v004 当前形态的 14 条租户 / TTL 索引。老库上一条都没有，而它们是租户过滤和
#: TTL 清理走索引的全部依赖（``idx_responses_prev`` 老库已有，不在此列）。
_EXPECTED_INDEXES: tuple[str, ...] = (
    "idx_responses_ws",
    "idx_responses_expires",
    "idx_resp_input_ws",
    "idx_resp_input_expires",
    "idx_resp_output_ws",
    "idx_resp_output_expires",
    "idx_resp_events_ws",
    "idx_resp_events_expires",
    "idx_state_chain_ws",
    "idx_bt_ws",
    "idx_bt_expires",
    "idx_te_ws",
    "idx_te_expires",
    "idx_idem_expires",
)


def _seed_legacy_v004_database(path: str) -> None:
    """按 c983adf 的旧形态建库，并伪造 v001/v003/v004 「已应用」的记录。

    复刻的是真实事故现场：``schema_migrations`` 自称迁移到位，物理表结构却停在
    改名之前。v001 / v003 借引擎真跑（v005 要 ALTER 它们建的 ``models`` 表），
    v004 用旧 DDL 手工建表 + 手写版本行 —— 这正是「原地重写已发布迁移」在存量
    库上留下的状态。
    """
    db, ex = _executor(path)
    try:
        _run(run_migrations_or_exit(ex, list(MIGRATIONS[:2]), sqlite_db_path=path))
        for sql in _LEGACY_V004_SQLITE:
            _run(db.execute(sql))
        _run(
            db.execute(
                "INSERT INTO schema_migrations "
                "(version, name, sql_digest, applied_at, duration_ms, status) "
                "VALUES (4, 'response_store', ?, 1, 0, 'applied')",
                (_LEGACY_V004_DIGEST,),
            )
        )
        # 带真实租户身份的历史数据行。
        _run(
            db.execute(
                "INSERT INTO responses (response_id, tenant_id, status, model, "
                "created_at, updated_at) VALUES ('resp_legacy', 't1', 'completed', 'm', 1, 1)"
            )
        )
        _run(
            db.execute(
                "INSERT INTO response_state_chain "
                "(response_id, tenant_id, previous_response_id, depth) "
                "VALUES ('resp_legacy', 't1', 'resp_parent', 1)"
            )
        )
        _run(
            db.execute(
                "INSERT INTO tool_executions (execution_id, response_id, tenant_id, "
                "call_id, tool_name, status, created_at, updated_at) "
                "VALUES ('exec_legacy', 'resp_legacy', 't1', 'call_1', 'f', 'done', 1, 1)"
            )
        )
        _run(
            db.execute(
                "INSERT INTO background_tasks (task_id, response_id, tenant_id, status, "
                "created_at, updated_at, lease_until, cancel_requested, "
                "max_wall_seconds, max_tool_rounds, attempt) "
                "VALUES ('task_legacy', 'resp_legacy', 't1', 'in_progress', "
                "1785740412, 1785740412, 1785740472, 1, 900, 32, 0)"
            )
        )
        _run(db.commit())
    finally:
        _run(db.close())


def _table_names(db) -> set[str]:
    rows = _run(db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
    return {r[0] for r in rows}


def _index_names(db) -> set[str]:
    rows = _run(db.execute_fetchall("SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
    return {r[0] for r in rows}


def _columns(db, table: str) -> set[str]:
    rows = _run(db.execute_fetchall(f"PRAGMA table_info({table})"))
    return {r[1] for r in rows}


def _schema_snapshot(db) -> dict:
    rows = _run(db.execute_fetchall("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    return {(r[0], r[1]): r[2] for r in rows}


def test_v007_realigns_a_legacy_pre_rename_database(tmp_db):
    """回归锁：一个「自称迁移到位、实际停在 c983adf 形态」的库必须被 v007 拉平。

    现有 ``test_v006_survives_legacy_tool_executions_without_workspace_id`` 只把
    ``tool_executions`` 一张表退化，覆盖面正好等于 T26 的修复面 —— 这就是 775 个
    测试全绿却挡不住本缺陷的原因。本测试复刻**完整**旧形态：7 张 ``tenant_id``
    表、``background_tasks``、无 ``expires_at``、无 ``idempotency_records``、
    7 条 ``*_tenant`` 索引。
    """
    _seed_legacy_v004_database(tmp_db)

    # 前置断言：确实处于旧形态（否则后面的"通过"毫无意义）。
    db0, _ = _executor(tmp_db)
    try:
        assert "workspace_id" not in _columns(db0, "responses")
        assert "expires_at" not in _columns(db0, "responses")
        assert "background_jobs" not in _table_names(db0)
        assert "idempotency_records" not in _table_names(db0)
        assert not (_index_names(db0) & set(_EXPECTED_INDEXES))
    finally:
        _run(db0.close())

    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))

        # 1) 8 张表齐全（background_tasks 保留，不计入）。
        tables = _table_names(db)
        for t in _EXPECTED_TABLES:
            assert t in tables, f"v007 未补齐表 {t}"

        # 2) workspace_id / expires_at 两列在 6 张表上齐全。
        for t in (
            "responses",
            "response_input_items",
            "response_output_items",
            "response_events",
            "response_state_chain",
            "tool_executions",
        ):
            cols = _columns(db, t)
            assert "workspace_id" in cols, f"{t} 缺 workspace_id"
            assert "expires_at" in cols, f"{t} 缺 expires_at"

        # 3) 14 条租户 / TTL 索引齐全。
        indexes = _index_names(db)
        missing = [i for i in _EXPECTED_INDEXES if i not in indexes]
        assert not missing, f"v007 未建出索引: {missing}"

        # 4) 租户回填正确 —— 老行的真实租户 t1 必须落到 workspace_id 上，
        #    而不是留在空租户里被任何走默认参数的调用一网打尽。
        row = _run(db.execute_fetchall("SELECT workspace_id, tenant_id FROM responses WHERE response_id='resp_legacy'"))
        assert row[0] == ("t1", "t1")
        row = _run(db.execute_fetchall("SELECT workspace_id FROM response_state_chain WHERE response_id='resp_legacy'"))
        assert row[0][0] == "t1"
        row = _run(db.execute_fetchall("SELECT workspace_id FROM tool_executions WHERE execution_id='exec_legacy'"))
        assert row[0][0] == "t1", "T26 遗留的孤儿行必须被 v007 回填"

        # 5) background_tasks 的数据出现在 background_jobs 里，字段逐一对应。
        row = _run(
            db.execute_fetchall(
                "SELECT task_id, response_id, workspace_id, status, created_at, "
                "updated_at, lease_until, cancel_requested, max_wall_seconds, "
                "max_tool_rounds, attempt, expires_at FROM background_jobs"
            )
        )
        assert row == [
            (
                "task_legacy",
                "resp_legacy",
                "t1",
                "in_progress",
                1785740412,
                1785740412,
                1785740472,
                1,
                900,
                32,
                0,
                0,
            )
        ]
        # 老表保留，给运维留回退余地。
        assert "background_tasks" in tables
        assert _run(db.execute_fetchall("SELECT COUNT(*) FROM background_tasks"))[0][0] == 1

        # 6) 被原地重写的 v004 必须在版本表里留下痕迹（可观测性，非阻断）。
        status = _run(db.execute_fetchall("SELECT status FROM schema_migrations WHERE version = 4"))
        assert status[0][0] == migration_engine.STATUS_DIGEST_MISMATCH
    finally:
        _run(db.close())


def test_v007_rerun_on_realigned_database_is_stable(tmp_db):
    """拉平后再跑一次整条链，schema 与数据都不能再变（可重入）。"""
    _seed_legacy_v004_database(tmp_db)

    db, ex = _executor(tmp_db)
    _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
    before_schema = _schema_snapshot(db)
    before_jobs = _run(db.execute_fetchall("SELECT * FROM background_jobs ORDER BY task_id"))
    # 强制 v007 再跑一遍（正常情况下它会被 applied 跳过，那样测不到任何东西）。
    _run(db.execute("DELETE FROM schema_migrations WHERE version = 7"))
    _run(db.commit())
    _run(db.close())

    db2, ex2 = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex2, MIGRATIONS, sqlite_db_path=tmp_db))
        assert _schema_snapshot(db2) == before_schema
        assert _run(db2.execute_fetchall("SELECT * FROM background_jobs ORDER BY task_id")) == before_jobs
    finally:
        _run(db2.close())


def test_v007_is_noop_on_healthy_database(tmp_db):
    """健康库（全新建的）上 v007 必须是完整 no-op：不改 schema、不动数据。

    v007 会在每一个新库上执行 ——「补列」全部撞上 ``duplicate column name``、
    「补表 / 补索引」全部撞上 ``IF NOT EXISTS``、hook 探测不到 ``tenant_id``
    也探测不到 ``background_tasks``。任何一条越界都会在这里暴露。
    """
    db, ex = _executor(tmp_db)
    _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
    # 塞入正常的新租户数据：若回填 hook 误伤，workspace_id 会被 '' 覆盖。
    _run(
        db.execute(
            "INSERT INTO responses (response_id, workspace_id, status, model, "
            "created_at, updated_at) VALUES ('resp_new', 'ws1', 'queued', 'm', 1, 1)"
        )
    )
    _run(
        db.execute(
            "INSERT INTO background_jobs (task_id, response_id, workspace_id, status, "
            "created_at, updated_at) VALUES ('task_new', 'resp_new', 'ws1', 'queued', 1, 1)"
        )
    )
    _run(db.commit())
    before_schema = _schema_snapshot(db)
    before_responses = _run(db.execute_fetchall("SELECT * FROM responses"))
    before_jobs = _run(db.execute_fetchall("SELECT * FROM background_jobs"))

    _run(db.execute("DELETE FROM schema_migrations WHERE version = 7"))
    _run(db.commit())
    _run(db.close())

    db2, ex2 = _executor(tmp_db)
    try:
        report = _run(run_migrations_or_exit(ex2, MIGRATIONS, sqlite_db_path=tmp_db))
        assert report.applied == [7], "只有 v007 应该重跑"
        assert _schema_snapshot(db2) == before_schema
        assert _run(db2.execute_fetchall("SELECT * FROM responses")) == before_responses
        assert _run(db2.execute_fetchall("SELECT * FROM background_jobs")) == before_jobs
        # 全新库上没有 tenant_id 列，也没有 background_tasks 表。
        assert "tenant_id" not in _columns(db2, "responses")
        assert "background_tasks" not in _table_names(db2)
    finally:
        _run(db2.close())


def test_digest_drift_warns_without_blocking_startup(tmp_db):
    """被原地重写的迁移必须**告警**而不是拒启动。

    ``data.db`` 里 v004 记录的 digest 与当前 v004 必然不符，而且 v007 修好之后
    这条记录**仍然**不会更新。做成硬校验会把所有存量库打死 —— 包括已经被 v007
    修复的那些。所以这里锁死：漂移被记录下来，服务照常启动。
    """
    db, ex = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))
        _run(db.execute("UPDATE schema_migrations SET sql_digest = 'deadbeef' WHERE version = 4"))
        _run(db.commit())

        # 不抛 SystemExit == 服务照常启动。
        _run(run_migrations_or_exit(ex, MIGRATIONS, sqlite_db_path=tmp_db))

        rows = _run(db.execute_fetchall("SELECT version, status FROM schema_migrations ORDER BY version"))
        by_version = dict(rows)
        assert by_version[4] == migration_engine.STATUS_DIGEST_MISMATCH
        # 未漂移的版本不能被误标。
        assert by_version[6] != migration_engine.STATUS_DIGEST_MISMATCH
    finally:
        _run(db.close())


def test_v003_hashes_legacy_tokens(tmp_db):
    """v003 hashes remaining plaintext tokens and clears the plaintext column."""
    # Seed a database with the baseline schema + a legacy plaintext token.
    db1, ex1 = _executor(tmp_db)
    _run(run_migrations_or_exit(ex1, [MIGRATIONS[0]], sqlite_db_path=tmp_db))
    _run(
        db1.execute(
            "INSERT INTO access_tokens (token, label, enabled, created_at) VALUES (?, ?, 1, ?)",
            ("zz-legacy-plaintext-token", "legacy", 1),
        )
    )
    _run(db1.commit())
    _run(db1.close())

    # Now run the full registry (v003 runs, baseline mode because schema_migrations exists).
    db2, ex2 = _executor(tmp_db)
    try:
        _run(run_migrations_or_exit(ex2, MIGRATIONS, sqlite_db_path=tmp_db))

        async def _query():
            cur = await db2.execute("SELECT token, token_prefix, token_hash FROM access_tokens WHERE label='legacy'")
            return await cur.fetchone()

        row = _run(_query())
        assert row is not None
        token, prefix, digest = row
        assert token == ""  # plaintext cleared
        assert prefix == "zz-legac"  # first 8 chars (TOKEN_PREFIX_LEN)
        assert digest  # hashed
    finally:
        _run(db2.close())


class _MySqlDialectExecutor(SqliteMigrationExecutor):
    """SqliteMigrationExecutor pretending to be the MySQL dialect.

    SQLite and MySQL/TiDB share the relevant UNIQUE semantics here: a UNIQUE
    index tolerates multiple NULLs but not multiple empty strings.  Driving the
    v003 hook through this executor reproduces the ER_DUP_ENTRY (1062) failure
    seen on TiDB when the hook cleared legacy tokens to ''.
    """

    dialect = "mysql"


def test_v003_mysql_dialect_clears_legacy_tokens_to_null(tmp_db):
    """v003 hook must clear legacy tokens to NULL on MySQL/TiDB, not ''.

    Regression for the production TiDB failure: clearing the second legacy
    plaintext token to '' collided with the first '' under the UNIQUE index
    (MySQL allows multiple NULLs, not multiple empty strings), aborting the
    migration at startup with ER_DUP_ENTRY.

    The table is built in the *post-ALTER* state -- all v003 columns present,
    ``token`` nullable but still UNIQUE -- matching TiDB after MYSQL_ALTERS
    ran but before the hook succeeded.
    """
    from zhongzhuan.store.migrations.v003_token_hash import MIGRATION as V003

    db = _run(aiosqlite.connect(tmp_db))
    _run(
        db.execute(
            """
        CREATE TABLE access_tokens (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            token           TEXT UNIQUE,          -- v001 UNIQUE, now nullable
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
        )
        """
        )
    )
    _run(
        db.execute(
            "INSERT INTO access_tokens (token, label, enabled, created_at) VALUES (?, ?, 1, ?)",
            ("zz-legacy-one", "legacy1", 1),
        )
    )
    _run(
        db.execute(
            "INSERT INTO access_tokens (token, label, enabled, created_at) VALUES (?, ?, 1, ?)",
            ("zz-legacy-two", "legacy2", 2),
        )
    )
    _run(db.commit())

    mysql_ex = _MySqlDialectExecutor(db)
    try:
        # Only the hook is under test; skip the MySQL ALTER statements.
        _run(V003.hook(mysql_ex))

        async def _rows():
            cur = await db.execute("SELECT token, token_prefix, token_hash FROM access_tokens ORDER BY id")
            return await cur.fetchall()

        rows = _run(_rows())
        assert len(rows) == 2
        for token, prefix, digest in rows:
            assert token is None  # cleared to NULL, not '' (UNIQUE-safe)
            assert prefix.startswith("zz-")
            assert digest  # hashed
    finally:
        _run(db.close())
