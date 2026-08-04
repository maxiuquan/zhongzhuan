"""v008 -- 粘性会话 session→route binding 持久化（T35 / R-P1-61 判据⑤）。

为什么要建新表
--------------
R-P1-61 要求粘性会话的 session→route binding 在 ResponseStore 持久化，
并在进程重启后仍可恢复（否则每次重启都会丢失「同一会话命中同一 key」的
连续性承诺）。binding 需要三样既有表没有的东西：

* **按 session 主键**（``session_key`` 是哈希指纹或 header 值，不是
  response_id / execution_id，任何现有表的键都不适用）；
* **TTL**（``expires_at``，与 v004 全表 TTL 约定一致，0 表示永不过期）；
* **故障迁移记录**（``failover_count`` / ``last_failover_reason``，
  R-P1-61「binding 具备 TTL、能力校验与故障迁移记录」）。

``responses`` 表按 ``response_id`` 主键、每一行是一个资源对象，塞 session
绑定会破坏它「一行一 response」的不变量；``response_state_chain`` 同样按
response 定位。所以新建一张 ``route_bindings`` 表是语义最干净的承载。

为什么不放进 ``system_config``
-------------------------------
那是 key-value 配置表，没有 TTL / 能力 / 故障迁移的三维结构，且它承载的是
系统配置而非运行时状态，混在一起会让 retention 无从下手。

幂等
----
单条 ``CREATE TABLE IF NOT EXISTS`` + 一条 ``CREATE INDEX IF NOT EXISTS``，
全新库与老库都直接可用；``baseline_probe="route_bindings"`` 让老库（v007 已
应用、但没有这张表）也走完整建表路径。
"""

from __future__ import annotations

from ..migration_engine import Migration

SQLITE_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS route_bindings (
        session_key    TEXT PRIMARY KEY,
        key_id         INTEGER NOT NULL DEFAULT 0,
        capabilities   TEXT NOT NULL DEFAULT '[]',
        workspace_id   TEXT NOT NULL DEFAULT '',
        created_at     INTEGER NOT NULL DEFAULT 0,
        updated_at     INTEGER NOT NULL DEFAULT 0,
        expires_at     INTEGER NOT NULL DEFAULT 0,
        failover_count INTEGER NOT NULL DEFAULT 0,
        last_failover_reason TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_route_bindings_expires ON route_bindings(expires_at)",
)

MYSQL_DDL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS route_bindings (
        session_key    VARCHAR(128) PRIMARY KEY,
        key_id         BIGINT NOT NULL DEFAULT 0,
        capabilities   TEXT NOT NULL,
        workspace_id   VARCHAR(64) NOT NULL DEFAULT '',
        created_at     BIGINT NOT NULL DEFAULT 0,
        updated_at     BIGINT NOT NULL DEFAULT 0,
        expires_at     BIGINT NOT NULL DEFAULT 0,
        failover_count BIGINT NOT NULL DEFAULT 0,
        last_failover_reason VARCHAR(255) NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX idx_route_bindings_expires ON route_bindings(expires_at)",
)


MIGRATION = Migration(
    version=8,
    name="route_bindings",
    sqlite_sql=SQLITE_DDL,
    mysql_sql=MYSQL_DDL,
    sqlite_baseline_sql=SQLITE_DDL,
    mysql_baseline_sql=MYSQL_DDL,
    baseline_probe="route_bindings",
)
