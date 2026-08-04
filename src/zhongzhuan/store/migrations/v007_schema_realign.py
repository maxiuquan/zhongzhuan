"""v007 -- 把被「原地重写的 v004」抛下的历史库拉平到 v004 当前形态。

这个迁移在修什么
----------------
提交 ``41da9d6`` **原地重写了一个已经发布的 v004**（B2 决策：``tenant_id``
改名 ``workspace_id``、``background_tasks`` 改名 ``background_jobs``、全表
新增 ``expires_at``、新增 ``idempotency_records``）。

迁移引擎依赖的不变量是「**version 相同 ⇒ schema 相同**」。原地重写打破了它：
``schema_migrations`` 里已经躺着 ``version=4``，
:meth:`MigrationRunner.applied_versions` 只比对 ``version``，于是重写后的 v004
在这些库上**永远不会重跑**；``CREATE TABLE IF NOT EXISTS`` 遇到已存在的表是静默
no-op，也无法把旧表改形。结果是一个自称已迁移到 v006 的库，实际停在改名前的
表结构上，而迁移引擎报告一切正常。

实测某开发库（``schema_migrations`` 自称 1/3/4/5/6 全部就位）：

===============================  ==================================
项目                             实际状态
===============================  ==================================
``responses.workspace_id``       不存在（还叫 ``tenant_id``）
``responses.expires_at``         不存在
``response_*_items.expires_at``  不存在
``background_jobs``              整张表不存在（还叫 ``background_tasks``）
``idempotency_records``          整张表不存在
v004 的 14 条 ``*_ws``/``*_expires`` 索引  一条都没有
===============================  ==================================

服务能正常启动，然后每一次 ``create_response`` / ``get_response`` /
事件追加 / 状态链查询 / TTL 清理都报 ``no such column`` 或 ``no such table``。

**检测不等于修复。** 给引擎加 ``sql_digest`` 校验只能让这次破坏被看见（v007
配套加了告警，见 :mod:`..migration_engine`），一列都补不上。所以 v007 是纯粹的
数据层修复：把缺的列、缺的表、缺的索引补齐，把租户身份搬到新列上，把老任务表
的数据搬进新任务表。

为什么不去改 v004
-----------------
再原地改一次 v004 就是把制造本 BUG 的操作重做一遍。已发布的迁移只能用**新的
版本号**修正 —— 这正是版本化迁移存在的理由。

五个组成部分
------------
1. **补列** —— 6 张表逐条 ``ALTER TABLE ... ADD COLUMN``（带常量默认值）。
   ``tool_executions`` 的两列 v006 已经补过，这里重复一遍是幂等 no-op。
2. **补表** —— ``background_jobs`` / ``idempotency_records``，DDL 逐字取自
   v004 当前形态（**抄成字面量而不是 import**，见下文「为什么 SQL 是字面量」）。
3. **补索引** —— 重跑 v004 的全部 15 条索引。老库上只有 ``idx_responses_prev``
   存在，另外 14 条一条都没建成，而它们是租户过滤与 TTL 清理走索引的全部依赖。
4. **回填租户** —— ``:func:`_realign_hook``` 里的条件 ``UPDATE``，把老行的
   ``tenant_id`` 搬到 ``workspace_id``。
5. **搬迁 background_tasks → background_jobs** —— 同一个 hook 内完成。

为什么 SQL 必须是字面量而不是 ``from .v004_response_store import ...``
----------------------------------------------------------------------
如果 v007 引用 v004 的语句元组，将来任何人改动 v004 都会**连带改掉 v007 的
``digest``**，而 v007 早已 applied、同样不会重跑 —— 一模一样的 BUG 会在
v007 身上复现一次。迁移是被冻结的历史快照，不是对活代码的引用。

为什么补列和回填必须在同一个迁移里
----------------------------------
``workspace_id=""`` 是 :class:`~zhongzhuan.store.response_store.ResponseStore` /
:class:`~zhongzhuan.store.tool_executions.ToolExecutionStore` 一大批方法的**默认
参数值**。如果先补列（老行 ``workspace_id`` 全为 ``''``）、隔一个版本再回填，
中间就存在一个窗口期：任何走默认租户的调用会把**所有老租户的行**一网打尽。
这是租户隔离缺口，不是数据整洁度问题。引擎把 SQL 语句与 ``hook`` 放在同一个
事务里（:meth:`MigrationRunner._run_one`），所以这里天然满足。

v006 曾以「不回填是为了避免半吊子状态」为由跳过 ``tool_executions`` 的回填。
该理由不成立：不回填造成的不是「保持原样」，而是**错误归属** —— 实测该库里
``tenant_id='t1'`` 的那行执行记录，租户 t1 自己查会查到 0 行，而任何走默认
``workspace_id=""`` 的调用反而能查到它。

幂等性
------
* 全部 ``ALTER TABLE ADD COLUMN`` 在列已存在时命中引擎白名单
  （SQLite ``duplicate column name`` / MySQL errno 1060）。
* 全部 ``CREATE TABLE`` / ``CREATE INDEX`` 带 ``IF NOT EXISTS``。
* 回填 ``UPDATE`` 带 ``WHERE workspace_id = '' AND tenant_id <> ''``，第二次跑
  匹配 0 行。
* 搬迁用 ``INSERT OR IGNORE`` / ``INSERT IGNORE``，主键冲突即跳过。

因此同一份语句可直接复用为 ``baseline_sql``，且在**全新库上是完整 no-op**
（列都在、表都在、索引都在、没有 ``tenant_id`` 列、没有 ``background_tasks``
表）。回归测试 ``test_v007_is_noop_on_healthy_database`` 锁死这一点。

``baseline_probe`` 为什么留空
-----------------------------
v007 是纠正性迁移，在任何库上都必须**完整执行**，没有「这个库太老、只跑部分
语句」的语义。``baseline_probe=""`` 让 :meth:`MigrationRunner.run_all` 的
baseline 分支恒不成立，v007 永远走完整路径。``sqlite_baseline_sql`` /
``run_hook_on_baseline`` 仍按同一份内容填上：万一将来有人给它设了 probe，
语义也不会静默退化成「跳过一半」—— 本文件存在的原因就是一次静默跳过。

不做的事
--------
* **不 DROP** ``background_tasks``。数据已经搬走，旧表原样留着给运维回退余地。
  **该表自 v007 起废弃**，不再有任何代码读写它，可在确认无回退需求后手工删除。
* **不重建表** 来纠正列顺序。SQLite 的 ``ADD COLUMN`` 只能把新列追加到末尾，
  所以老库上 ``responses`` 的物理列序是
  ``(..., cancelled, workspace_id, expires_at)``，而全新库是
  ``(response_id, workspace_id, ...)``。已知影响面仅
  :meth:`ResponseStore._row_to_record` 一处按下标取
  ``row[1]`` 当 ``workspace_id``（老库上取到的是 ``tenant_id``）；
  ``ResponseRecord.workspace_id`` 全仓无任何读取点，故不构成行为差异。
  纠正列序需要「建新表 + 全量拷贝 + DROP + RENAME」，风险与收益不成比例，
  已作为独立事项上报。
"""

from __future__ import annotations

from loguru import logger

from ..migration_engine import Migration, MigrationExecutor

# --------------------------------------------------------------------------- #
# 1) 补列
# --------------------------------------------------------------------------- #
#: 需要补 ``workspace_id`` / ``expires_at`` 的 6 张表。
#: ``tool_executions`` 的两列 v006 已补过，重复执行是幂等 no-op —— 但仍然列在
#: 这里，这样 v007 单独在任意库上跑都能自证前置条件，而不是依赖 v006 跑过。
_REALIGN_TABLES: tuple[str, ...] = (
    "responses",
    "response_input_items",
    "response_output_items",
    "response_events",
    "response_state_chain",
    "tool_executions",
)

SQLITE_ADD_COLUMNS: tuple[str, ...] = tuple(
    stmt
    for table in _REALIGN_TABLES
    for stmt in (
        f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''",
        f"ALTER TABLE {table} ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0",
    )
)

MYSQL_ADD_COLUMNS: tuple[str, ...] = tuple(
    stmt
    for table in _REALIGN_TABLES
    for stmt in (
        f"ALTER TABLE {table} ADD COLUMN workspace_id VARCHAR(64) NOT NULL DEFAULT ''",
        f"ALTER TABLE {table} ADD COLUMN expires_at BIGINT NOT NULL DEFAULT 0",
    )
)

# --------------------------------------------------------------------------- #
# 2) 补表 -- DDL 逐字取自 v004 当前形态（字面量副本，不 import）
# --------------------------------------------------------------------------- #
SQLITE_CREATE_TABLES: tuple[str, ...] = (
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
)

MYSQL_CREATE_TABLES: tuple[str, ...] = (
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
)

# --------------------------------------------------------------------------- #
# 3) 补索引 -- v004 当前形态的全部 15 条（字面量副本）
# --------------------------------------------------------------------------- #
#: ``(索引名, 表(列...))`` —— 两个方言只差 ``IF NOT EXISTS``，故共用定义。
#: 老库上只有 ``idx_responses_prev`` 存在，其余 14 条 ``*_ws`` / ``*_expires``
#: 一条都没建成。
_INDEX_SPECS: tuple[tuple[str, str], ...] = (
    ("idx_responses_ws", "responses(workspace_id, created_at)"),
    ("idx_responses_prev", "responses(previous_response_id)"),
    ("idx_responses_expires", "responses(expires_at)"),
    ("idx_resp_input_ws", "response_input_items(response_id, workspace_id)"),
    ("idx_resp_input_expires", "response_input_items(expires_at)"),
    ("idx_resp_output_ws", "response_output_items(response_id, workspace_id)"),
    ("idx_resp_output_expires", "response_output_items(expires_at)"),
    ("idx_resp_events_ws", "response_events(response_id, workspace_id)"),
    ("idx_resp_events_expires", "response_events(expires_at)"),
    ("idx_state_chain_ws", "response_state_chain(workspace_id, previous_response_id)"),
    ("idx_bt_ws", "background_jobs(workspace_id, status)"),
    ("idx_bt_expires", "background_jobs(expires_at)"),
    ("idx_te_ws", "tool_executions(workspace_id, response_id)"),
    ("idx_te_expires", "tool_executions(expires_at)"),
    ("idx_idem_expires", "idempotency_records(expires_at)"),
)

SQLITE_INDEXES: tuple[str, ...] = tuple(
    f"CREATE INDEX IF NOT EXISTS {name} ON {target}" for name, target in _INDEX_SPECS
)

#: ``CREATE INDEX IF NOT EXISTS`` 不是合法的 MySQL 语法（v001 的文件头已记录过
#: 这个坑；v004 / v006 的 MySQL 分支沿用了 SQLite 写法，是既有缺陷，不在本次
#: 修复范围）。这里按 v001 的既定规则写裸 ``CREATE INDEX``，重复建索引由引擎的
#: errno 1061（``ER_DUP_KEYNAME``）白名单吞掉，效果等价且语法合法。
MYSQL_INDEXES: tuple[str, ...] = tuple(f"CREATE INDEX {name} ON {target}" for name, target in _INDEX_SPECS)

SQLITE_SQL: tuple[str, ...] = SQLITE_ADD_COLUMNS + SQLITE_CREATE_TABLES + SQLITE_INDEXES
MYSQL_SQL: tuple[str, ...] = MYSQL_ADD_COLUMNS + MYSQL_CREATE_TABLES + MYSQL_INDEXES


# --------------------------------------------------------------------------- #
# 4) + 5) 数据迁移 hook
# --------------------------------------------------------------------------- #
#: ``background_jobs`` 列 -> ``background_tasks`` 列 的字段映射。
#:
#: 逐字段核对结果（老表来自 ``git show c983adf:...v004_response_store.py``）：
#:
#: ==================  ==================  ====================================
#: background_jobs     background_tasks    说明
#: ==================  ==================  ====================================
#: task_id             task_id             同名，PK
#: response_id         response_id         同名
#: workspace_id        tenant_id           **唯一的改名**（B2 决策）
#: status              status              同名，取值域一致
#: created_at          created_at          同名
#: updated_at          updated_at          同名
#: lease_until         lease_until         同名
#: cancel_requested    cancel_requested    同名
#: max_wall_seconds    max_wall_seconds    同名
#: max_tool_rounds     max_tool_rounds     同名
#: attempt             attempt             同名
#: expires_at          （老表没有）        **新表独有**，取新表默认值 ``0``
#: ==================  ==================  ====================================
#:
#: ``expires_at = 0`` 在 :class:`~zhongzhuan.store.background_jobs.BackgroundJobStore`
#: 里的语义是「无 TTL / 永不过期」（``expires_at = 0 OR expires_at > now``），
#: 正是早于 TTL 机制存在的老任务应有的语义 —— 不会被 :meth:`expire_stale` 误杀。
#:
#: 没有任何一个字段的映射是歧义的：除 ``tenant_id -> workspace_id`` 外全部同名
#: 同义，新表也只多出一个带默认值的列。
_JOB_COLUMN_MAP: tuple[tuple[str, str], ...] = (
    ("task_id", "task_id"),
    ("response_id", "response_id"),
    ("workspace_id", "tenant_id"),
    ("status", "status"),
    ("created_at", "created_at"),
    ("updated_at", "updated_at"),
    ("lease_until", "lease_until"),
    ("cancel_requested", "cancel_requested"),
    ("max_wall_seconds", "max_wall_seconds"),
    ("max_tool_rounds", "max_tool_rounds"),
    ("attempt", "attempt"),
)

#: 老任务表名。数据搬走后该表即废弃，但**不删除**（保留运维回退余地）。
_LEGACY_JOB_TABLE = "background_tasks"


async def _column_names(ex: MigrationExecutor, table: str) -> set[str]:
    """返回 *table* 的列名集合；表不存在时返回空集。

    SQLite 用 ``PRAGMA table_info``，MySQL / TiDB 没有 ``PRAGMA``，用
    ``information_schema.columns`` 等价探测。两者在表不存在时都返回空结果集
    而非报错，所以空集同时也是「表不存在」的信号。

    *table* 只来自本模块的常量，不含外部输入，因此 SQLite 分支里的字符串拼接
    （``PRAGMA`` 不接受占位符）没有注入面。
    """
    if ex.dialect == "sqlite":
        rows = await ex.fetchall(f"PRAGMA table_info({table})")
        return {str(r[1]) for r in rows}
    rows = await ex.fetchall(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = ?",
        (table,),
    )
    return {str(r[0]) for r in rows}


async def _backfill_tenant_ids(ex: MigrationExecutor) -> None:
    """把老行的 ``tenant_id`` 搬到 ``workspace_id``（条件执行）。

    只在表同时拥有两列时执行 —— 全新库没有 ``tenant_id``，老库的
    ``response_input_items`` / ``response_output_items`` / ``response_events``
    在改名前也从来没有过租户列（见 c983adf 的 v004 DDL），两种情况都直接跳过。

    ``WHERE workspace_id = '' AND tenant_id <> ''`` 保证：
    * 只碰「还没有新租户身份」的行，不覆盖 v006 之后正常写入的新行；
    * 第二次执行匹配 0 行，天然可重入。
    """
    for table in _REALIGN_TABLES:
        columns = await _column_names(ex, table)
        if "tenant_id" not in columns or "workspace_id" not in columns:
            continue
        await ex.execute(f"UPDATE {table} SET workspace_id = tenant_id WHERE workspace_id = '' AND tenant_id <> ''")
        logger.info(f"v007: backfilled workspace_id from tenant_id on {table}")


async def _migrate_legacy_jobs(ex: MigrationExecutor) -> None:
    """把 ``background_tasks`` 的行搬进 ``background_jobs``。

    只在老表存在时执行。``INSERT OR IGNORE`` / ``INSERT IGNORE`` 让主键已存在
    的行原样跳过，所以重跑安全、也不会覆盖 v007 之后写入的新任务。

    拷贝的列由「映射表 ∩ 老表实际列」决定，而不是硬写死一串列名 —— 一个迁移
    必须自己建立前置条件，不能假设前置条件成立（这正是 v006 曾踩过的坑）。
    未被拷贝的列落到 ``background_jobs`` 的 DDL 默认值上。
    """
    if not await ex.table_exists(_LEGACY_JOB_TABLE):
        return
    legacy_columns = await _column_names(ex, _LEGACY_JOB_TABLE)
    pairs = [(new, old) for new, old in _JOB_COLUMN_MAP if old in legacy_columns]
    if not any(new == "task_id" for new, _ in pairs):
        logger.warning(
            f"v007: {_LEGACY_JOB_TABLE} has no task_id column "
            f"(columns={sorted(legacy_columns)}); skipping job migration"
        )
        return

    target = ", ".join(new for new, _ in pairs)
    source = ", ".join(old for _, old in pairs)
    prefix = "INSERT OR IGNORE INTO" if ex.dialect == "sqlite" else "INSERT IGNORE INTO"
    await ex.execute(f"{prefix} background_jobs ({target}) SELECT {source} FROM {_LEGACY_JOB_TABLE}")
    logger.info(
        f"v007: migrated {_LEGACY_JOB_TABLE} -> background_jobs "
        f"(columns={target}); the legacy table is now deprecated but kept"
    )


async def _realign_hook(ex: MigrationExecutor) -> None:
    """数据部分：租户回填 + 老任务表搬迁。

    引擎在同一个事务里先跑 :data:`SQLITE_SQL` / :data:`MYSQL_SQL` 再调本 hook
    （见 :meth:`MigrationRunner._run_one`），所以这里可以安全地假设列和表都已
    补齐；而「补列」与「回填」之间不存在任何窗口期。
    """
    await _backfill_tenant_ids(ex)
    await _migrate_legacy_jobs(ex)


MIGRATION = Migration(
    version=7,
    name="schema_realign",
    sqlite_sql=SQLITE_SQL,
    mysql_sql=MYSQL_SQL,
    sqlite_baseline_sql=SQLITE_SQL,
    mysql_baseline_sql=MYSQL_SQL,
    hook=_realign_hook,
    run_hook_on_baseline=True,
    baseline_probe="",
)
