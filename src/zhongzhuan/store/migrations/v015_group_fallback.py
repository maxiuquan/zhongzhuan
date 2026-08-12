"""v015 -- 分组兜底(fallback_group)。

为 ``model_groups`` 表新增一列, 用于「某分组全部成员失败时, 用另一个分组兜底」:
- ``fallback_group``: 兜底分组名(name, 如 juhe/glm5.2), VARCHAR(128), 默认 ''。
  空串 = 未配置兜底。

两列语义: 值为 '' 表示未设置; 调度侧遇空串直接跳过兜底。
TiDB/MySQL 用 VARCHAR(可带 DEFAULT); SQLite 用 TEXT NOT NULL DEFAULT ''。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: TEXT NOT NULL DEFAULT '' 在 ADD COLUMN 下合法(默认值是常量)。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE model_groups ADD COLUMN fallback_group TEXT NOT NULL DEFAULT ''",
)

#: MySQL / TiDB: VARCHAR 允许 NOT NULL DEFAULT ''。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE model_groups ADD COLUMN fallback_group VARCHAR(128) NOT NULL DEFAULT ''",
)


MIGRATION = Migration(
    version=15,
    name="group_fallback",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="model_groups",
)
