"""v012 -- 模型「上游支持 reasoning_effort」开关。

新增一列 ``supports_reasoning_effort``（1=支持, 0=不支持）：

* ``models.supports_reasoning_effort`` -- 控制代理是否把客户端发来的
  ``reasoning_effort``（及 Responses 的 ``reasoning.effort``）转发给该模型的
  上游。部分上游（某些第三方中转、国内模型网关、聚合分组下的非推理成员）
  不认识这个参数，会回 ``400 Unsupported parameter: 'reasoning_effort'``。
  关掉后代理在构建上游请求体前剥除该参数，请求即可正常通过（仅失去推理
  强度控制，不影响其他功能）。

为什么是列而不是配置文件
------------------------
这是「每条模型」的属性，与 ``enabled`` / ``exposed`` 同级；放数据库列可让
后台 UI 直接勾选保存、实时生效（admin 保存后 notify_proxy_reload 会重建
内存负缓存）。

默认值 1（支持）-- 保持升级前的既有行为（原来对每家上游都无条件转发
reasoning_effort）。老库升级后不会突然剥除任何参数，只有管理员在后台或通过
实测把不兼容模型显式关掉后才生效。

Idempotency
-----------
单条 bare ``ADD COLUMN``，engine 的 error-code 白名单已识别
（SQLite ``duplicate column name`` / MySQL errno 1060），同一组 SQL
同时作为 baseline。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: INTEGER NOT NULL DEFAULT 1（1 = 支持 reasoning_effort）。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN supports_reasoning_effort INTEGER NOT NULL DEFAULT 1",
)

#: MySQL / TiDB: TINYINT(1) NOT NULL DEFAULT 1。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN supports_reasoning_effort TINYINT(1) NOT NULL DEFAULT 1",
)


MIGRATION = Migration(
    version=12,
    name="reasoning_effort_flag",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="models",
)
