"""v006 -- hosted tool 执行记录扩展（T26 / R-P1-46、R-P1-47、§4-Q4）。

为什么是 ALTER 而不是 CREATE TABLE
----------------------------------
``tool_executions`` 表 **v004 已经建过**（见
:mod:`.v004_response_store`），承载的是 function call 维度的执行审计：
``execution_id`` 主键 + ``call_id`` / ``tool_name`` / ``idempotency_key`` /
``status`` / ``approval`` / ``result_digest``。

T26 需要的是 hosted tool 维度：一次请求里 ``tools[N]`` 的第 N 个 hosted tool
「是否被识别、是否被持久化、审批到哪一步」。这**不是**另一张表 —— 它和
function call 共享同一份执行语义（审批、幂等键、状态、TTL、租户键），只是多了
三个定位字段。再建一张同名表会被 ``CREATE TABLE IF NOT EXISTS`` 静默跳过，
写进去的行永远读不出来；建一张不同名的表则会把「工具执行审计」拆成两处，
T27 的 MCP 审批要同时查两张表才能回答一个问题。

所以 v006 **扩展** v004 的表：

    tool_seq    -- hosted tool 在原始 ``tools`` 数组中的下标；``-1`` 表示这行
                   是 v004 风格的 function call 记录（没有数组位置）
    tool_type   -- ``web_search`` / ``code_interpreter`` / ``mcp`` / ...
                   hosted tool **没有 name**（R-P1-46 反对的正是「以没有 name
                   为由丢弃」），所以类型必须有自己的列，不能挤进 ``tool_name``
    capability  -- 承载该 tool 所需的 :class:`Capability` 值，便于按能力维度
                   审计「哪些请求因为缺执行器被拒」

唯一性由 ``execution_id`` 承担
------------------------------
架构任务书给的是 ``PRIMARY KEY(response_id, tool_seq)``。SQLite 无法在
``ALTER TABLE`` 里改主键，而 v004 的旧行 ``tool_seq`` 全为 ``-1``，直接加
UNIQUE 索引会让同一个 response 的第二条 function call 记录插入失败 ——
把新需求的约束强加到旧数据上是纯粹的回归。

等价做法：:class:`~zhongzhuan.store.tool_executions.ToolExecutionStore` 用确定性
的 ``execution_id = "{response_id}#{tool_seq}"`` 作为主键，同一 ``(response_id,
tool_seq)`` 必然落在同一行，``INSERT OR REPLACE`` 天然幂等。约束强度与复合主键
一致，且不触碰历史行。索引 ``idx_te_seq`` 只负责让按 seq 排序的读走索引。

为什么先补 ``workspace_id`` / ``expires_at``（启动阻断修复）
------------------------------------------------------------
**不能假设 v004 的列真的存在。** 实测发现的真实升级路径：某些数据库是被 v004
的**早期版本**迁移的（B2 改名之前，租户键叫 ``tenant_id``、无 ``expires_at``、
``background_jobs`` 还叫 ``background_tasks``）。v004 后来在仓库里被重写成现在
的形态，但它早已记录在 ``schema_migrations`` 里 —— 而
:meth:`MigrationRunner.applied_versions` 只比对 ``version``、**不校验
``sql_digest``**，所以重写后的 v004 在这些库上永远不会重跑。
``CREATE TABLE IF NOT EXISTS`` 遇到已存在的表是静默 no-op，无法把旧表改形，
于是这些库的 ``tool_executions`` 永久停留在旧列集。

v006 最初直接 ``CREATE INDEX ... ON tool_executions(workspace_id, approval)``，
在这类库上报 ``no such column: workspace_id``。该错误不在引擎的可忽略白名单内，
迁移失败 -> ``run_migrations_or_exit`` 以 :data:`MIGRATION_EXIT_CODE` 退出 ->
**整个服务拒绝启动**。一个新迁移把既有部署打死，这是比缺功能严重得多的回归。

结论：**迁移必须自己建立前置条件，而不是假设前置条件成立。** 因此 v006 在
建索引前先补两列：

``workspace_id``
    索引 ``idx_te_approval`` 与
    :class:`~zhongzhuan.store.tool_executions.ToolExecutionStore` 的全部租户
    过滤都依赖它。
``expires_at``
    :meth:`ToolExecutionStore.record` 的 INSERT 会写这一列。

两条都是带常量默认值的 ``ADD COLUMN``，在列已存在的正常库上命中引擎的
``duplicate column name`` / errno 1060 白名单，天然幂等。

不做的事（范围边界，见报告）
----------------------------
* **不回填** ``workspace_id = tenant_id``。同一批老库的 ``responses`` /
  ``response_state_chain`` 同样缺 ``workspace_id``，``background_jobs`` /
  ``idempotency_records`` 整张表缺失 —— 那是 B2 改名遗留的**全库**问题，
  属于 v004/T19 的归属范围。只把 ``tool_executions`` 一张表回填，会造出一个
  「看起来迁移过了」的半吊子状态，比原样保留更难排查。
* **不改** v004。它已经在各处 applied，再改一次只会重复制造本 BUG。
* **不加** ``sql_digest`` 校验。加了会让所有存量部署 fail closed，必须单独
  评估，不能夹带在 T26 里。

幂等
----
五条 ``ADD COLUMN`` + 两条 ``CREATE INDEX IF NOT EXISTS``，全部落在迁移引擎的
错误码白名单内（SQLite ``duplicate column name`` / ``index ... already
exists``，MySQL errno 1060 / 1061），因此同一份语句可直接复用为 baseline SQL。
``baseline_probe="tool_executions"``：老库里这张表一定存在（v004 建的）。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite：``ADD COLUMN`` 允许 ``NOT NULL DEFAULT <常量>``。
#: ``tool_seq`` 默认 ``-1`` —— 已有的 function call 行没有 ``tools`` 数组位置，
#: 用 ``-1`` 而不是 ``0`` 才不会被误读成「第 0 个 hosted tool」。
SQLITE_ALTERS: tuple[str, ...] = (
    # 启动阻断修复：先补 v004 早期版本可能缺失的租户键 / TTL 列（见文件头注释）。
    # 列已存在时命中引擎的 "duplicate column name" 白名单，幂等可复用为 baseline。
    "ALTER TABLE tool_executions ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tool_executions ADD COLUMN expires_at INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tool_executions ADD COLUMN tool_seq INTEGER NOT NULL DEFAULT -1",
    "ALTER TABLE tool_executions ADD COLUMN tool_type TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tool_executions ADD COLUMN capability TEXT NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_te_seq ON tool_executions(response_id, tool_seq)",
    "CREATE INDEX IF NOT EXISTS idx_te_approval ON tool_executions(workspace_id, approval)",
)

#: MySQL / TiDB：类型对齐 v004 的既有列宽（``tool_name VARCHAR(128)`` /
#: ``approval VARCHAR(32)``），能力名最长 ``stateful_responses`` 共 19 字符。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE tool_executions ADD COLUMN workspace_id VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE tool_executions ADD COLUMN expires_at BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE tool_executions ADD COLUMN tool_seq BIGINT NOT NULL DEFAULT -1",
    "ALTER TABLE tool_executions ADD COLUMN tool_type VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE tool_executions ADD COLUMN capability VARCHAR(64) NOT NULL DEFAULT ''",
    "CREATE INDEX idx_te_seq ON tool_executions(response_id, tool_seq)",
    "CREATE INDEX idx_te_approval ON tool_executions(workspace_id, approval)",
)


MIGRATION = Migration(
    version=6,
    name="tool_executions",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="tool_executions",
)
