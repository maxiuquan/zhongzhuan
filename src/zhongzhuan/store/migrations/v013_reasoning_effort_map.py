"""v013 -- 模型思考等级映射表(reasoning_effort_map)。

新增一列 ``reasoning_effort_map``(TEXT JSON), 由「测试连通性」探针自动写入:
记录该模型上游对各标准思考等级(none|low|medium|high|ultra)的原生表示片段,
或 null(剥除)。代理在选中该渠道成员时按 map 把客户端发来的标准等级翻译成
上游原生参数, 从而对外暴露统一标准、对内适配异构上游(枚举字符串 / token
预算对象 / 开关+预算 / 整段关掉 / 全剥除)。

与 v012 的 ``supports_reasoning_effort``(二进制粗粒度开关)互补:
- map 非空时, 翻译层按 map 精确翻译(每档可不同);
- map 为空时, 退回 v012 的 A 开关 + D 自愈兜底。

列允许为 NULL（TiDB/MySQL 的 TEXT 列不允许 DEFAULT 值）; NULL/空 → 退回既有
行为, 升级无副作用。
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite / MySQL / TiDB: TEXT 列不允许 DEFAULT, 故声明为可空、无默认值。
#: 读取侧(_parse_reasoning_map)对 None/空 统一视为「未配置」。
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN reasoning_effort_map TEXT",
)

MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN reasoning_effort_map TEXT",
)


MIGRATION = Migration(
    version=13,
    name="reasoning_effort_map",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="models",
)
