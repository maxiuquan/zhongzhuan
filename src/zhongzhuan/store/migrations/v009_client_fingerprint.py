"""v009 -- per-model 上游客户端指纹模拟（WorkBuddy 限免渠道等）。

新增两列：

* ``client_preset`` -- "" (不模拟, 默认零影响) | "workbuddy" (内置预设) |
  "custom" (用户自定义头)。字符串而非枚举表: 未来新增预设只需在
  ``proxy.client_presets.PRESETS`` 字典加一条, 无需再加列/加表。
* ``custom_headers`` -- JSON 数组字符串, 仅当 ``client_preset = "custom"``
  时生效。格式 ``[{"name":"X-Foo","value":"bar"},{"name":"X-Request-ID",
  "value":"{{uuid}}"}]``。选 WorkBuddy 时此字段不使用但保留值, 方便来回
  切换不丢失。

为什么不放 key 级
-----------------
指纹是"上游服务身份", 与 ``upstream_base`` 同属一个上游配置维度; 同一
model 的多个 key 共享指纹。与现有 ``upstream_path_override`` / ``protocol``
同级, 架构一致性最高。

为什么不全局开关
----------------
功能是 per-model 行为, 开关粒度在模型本身。用户建一个 WorkBuddy 模型、
其他模型不选, 就是"按模型开关", 比全局开关更精细且无需额外 UI。

Idempotency
-----------
两条 bare ``ADD COLUMN``, engine 的 error-code 白名单已识别
(SQLite ``duplicate column name`` / MySQL errno 1060), 同一组 SQL 同时
作为 baseline。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: TEXT NOT NULL DEFAULT '' 在 ADD COLUMN 下合法 (默认值是常量)。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN client_preset TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN custom_headers TEXT NOT NULL DEFAULT ''",
)

#: MySQL / TiDB: 64 容纳预设 token; 4096 容纳多条自定义头 JSON。
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN client_preset VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN custom_headers VARCHAR(4096) NOT NULL DEFAULT ''",
)


MIGRATION = Migration(
    version=9,
    name="client_fingerprint",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="models",
)
