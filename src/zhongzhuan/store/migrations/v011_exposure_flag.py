"""v011 -- 模型 / 分组「暴露给 Codex 发现」开关。

新增一列 ``exposed``（1=暴露, 0=隐藏）：

* ``models.exposed`` -- 控制该模型是否出现在 Codex 模型发现列表
  （``/v1/models`` 的 Codex 分支 与 ``/v1/api/codex/models``）。
* ``model_groups.exposed`` -- 同上，作用于分组（如 ``juhe/glm5.2``）。

为什么是列而不是配置文件
------------------------
暴露是「每条模型/分组」的属性，与 ``enabled`` / ``is_fallback`` 同级；
放数据库列可让后台 UI 直接勾选保存、实时生效（Codex 发现每次都查库）。

默认值 1（暴露）-- 保持升级前的既有行为（原来对所有启用且非兜底
模型 + juhe/* 分组一律暴露），老库升级后不会突然隐藏任何东西。

Idempotency
-----------
两条 bare ``ADD COLUMN``，engine 的 error-code 白名单已识别
（SQLite ``duplicate column name`` / MySQL errno 1060），同一组 SQL
同时作为 baseline。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: INTEGER NOT NULL DEFAULT 1（1 = 暴露）。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN exposed INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE model_groups ADD COLUMN exposed INTEGER NOT NULL DEFAULT 1",
)

#: MySQL / TiDB: TINYINT(1) NOT NULL DEFAULT 1。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN exposed TINYINT(1) NOT NULL DEFAULT 1",
    "ALTER TABLE model_groups ADD COLUMN exposed TINYINT(1) NOT NULL DEFAULT 1",
)


MIGRATION = Migration(
    version=11,
    name="exposure_flag",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="models",
)
