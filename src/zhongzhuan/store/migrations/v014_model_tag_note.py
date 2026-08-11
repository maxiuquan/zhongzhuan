"""v014 -- 模型上游标签与备注(upstream_tag / note)。

为 ``models`` 表新增两列, 用于后台对模型做渠道来源标注与自由备注:
- ``upstream_tag``: 上游标签(官方 / 中转站 / 自定义), VARCHAR(32), 默认 ''。
- ``note``: 模型备注(free text), VARCHAR(1024), 默认 ''。

两列均 NOT NULL DEFAULT '', 升级无副作用; 读取侧对空串统一视为「未设置」。
TiDB/MySQL 用 VARCHAR(可带 DEFAULT); SQLite 用 TEXT NOT NULL DEFAULT ''。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: TEXT NOT NULL DEFAULT '' 在 ADD COLUMN 下合法(默认值是常量)。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN upstream_tag TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN note TEXT NOT NULL DEFAULT ''",
)

#: MySQL / TiDB: VARCHAR 允许 NOT NULL DEFAULT ''。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN upstream_tag VARCHAR(32) NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN note VARCHAR(1024) NOT NULL DEFAULT ''",
)


MIGRATION = Migration(
    version=14,
    name="model_tag_note",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="models",
)
