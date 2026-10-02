"""v018 -- key_health 增加失败原因字段（失效 Key 健康模块支撑）。

动机
----
2026-10-02 后台「Key 健康」模块立项：面板要展示每个失效 key 的**报错
原因**（凭据失效 / 封禁 / 限流 / 上游错误 / 余额耗尽）与最近失败时间。
此前 ``failure_class`` / ``last_failure_at`` 只存在于 proxy 内存
（``KeyHealth``），不在快照落库范围内 —— 服务一重启原因就丢，面板上
只剩一个光秃秃的 invalid/error 状态（实测 3 把 invalid 原因列全空）。

方案：两列进 ``key_health`` 表，随快照循环一起落库：

* ``failure_class TEXT NOT NULL DEFAULT ''`` —— 失败分类（与
  ``proxy/retry.py`` 的 CLASS_* 常量同词表：permanent / banned /
  rate_limit / transient / balance / no_retry），healthy 且完全恢复后
  为空串。
* ``last_failure_at REAL NOT NULL DEFAULT 0`` —— 最近一次失败时间戳
  （epoch 秒），0 = 从未失败。

指纹同步扩为 9 字段（handler ``_health_fingerprint`` 与
``key_health.row_to_fingerprint`` 两侧一致），读回对账才能覆盖这两个
字段的变化。归位规则（``normalize_expired_cooldown``）不动
``failure_class``，因此归位当轮会把 healthy + 旧原因一起落库——这是
期望行为：面板能继续显示「上次失败原因」，与 ``mark_success`` 的保留
语义一致。

幂等
----
* SQLite：``ALTER TABLE ... ADD COLUMN``，重复执行报 duplicate column
  由迁移引擎白名单吞掉（v001 先例）。
* MySQL / TiDB：errno 1060（``ER_DUP_FIELDNAME``）同规则白名单。

用法
----
``load_all_health`` 恢复内存时带回两字段，启动后面板立即可见历史原因。
"""

from __future__ import annotations

from ..migration_engine import Migration

SQLITE_SQL: tuple[str, ...] = (
    "ALTER TABLE key_health ADD COLUMN failure_class TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE key_health ADD COLUMN last_failure_at REAL NOT NULL DEFAULT 0",
)

MYSQL_SQL: tuple[str, ...] = (
    "ALTER TABLE key_health ADD COLUMN failure_class VARCHAR(32) NOT NULL DEFAULT ''",
    "ALTER TABLE key_health ADD COLUMN last_failure_at DOUBLE NOT NULL DEFAULT 0",
)

MIGRATION = Migration(
    version=18,
    name="key_health_failure_class",
    sqlite_sql=SQLITE_SQL,
    mysql_sql=MYSQL_SQL,
    sqlite_baseline_sql=SQLITE_SQL,
    mysql_baseline_sql=MYSQL_SQL,
)
