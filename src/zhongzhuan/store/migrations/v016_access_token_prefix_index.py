"""v016 -- access_tokens(token_prefix) 认证热路径索引 + 清理 v003 僵尸 UNIQUE(token)。

动机
----
认证热路径（每个进站请求都要走）是
``SELECT ... FROM access_tokens WHERE token_prefix=?``
（见 ``store/access_tokens.get_token_by_value``）。v003 之后该表没有任何
``token_prefix`` 上的索引，这条查询在 SQLite / TiDB 上都是**全表扫描**：
token 数量随租户增长，每次请求的认证延迟随之线性劣化。本迁移为两个方言补上
``idx_access_tokens_prefix``。

顺带（仅 MySQL / TiDB）：v001 的基线 DDL 给 ``token`` 列声明了列级
``UNIQUE``；v003 只把该列改成可空（MySQL 的 UNIQUE 索引容忍多个 NULL，当时
选择保留索引，见 v003 文件头）。哈希化之后明文列恒为 NULL，这条 UNIQUE 索引
只剩两个副作用 —— 白占一份写放大，还会把第二个非 NULL 明文挡死。SQLite 侧在
v003 重建表时已一并甩掉了它（rename 换表），MySQL 侧一直留着。这里用 hook 先查
``information_schema.statistics`` 再 DROP，避免「索引不存在时裸 DROP 报
errno 1091（不在引擎白名单）」把迁移打死。

幂等
----
* SQLite：``CREATE INDEX IF NOT EXISTS``。
* MySQL / TiDB：裸 ``CREATE INDEX``，重复建由引擎的 errno 1061
  （``ER_DUP_KEYNAME``）白名单吞掉（与 v001/v007 同一既定规则）。
* hook 自带存在性检查，天然可重入。
"""

from __future__ import annotations

from ..migration_engine import Migration, MigrationExecutor

SQLITE_SQL: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_access_tokens_prefix ON access_tokens(token_prefix)",
)

MYSQL_SQL: tuple[str, ...] = (
    "CREATE INDEX idx_access_tokens_prefix ON access_tokens(token_prefix)",
)

#: v001 列级 ``UNIQUE`` 生成的隐式索引名 = 列名（MySQL/TiDB 命名规则）。
_LEGACY_UNIQUE_TOKEN_INDEX = "token"


async def _drop_legacy_unique_token_index(ex: MigrationExecutor) -> None:
    """DROP MySQL/TiDB 上 v003 时代遗留的僵尸 ``UNIQUE(token)`` 索引。

    不用裸 DDL 的原因：``DROP INDEX`` 遇到索引不存在会报 errno 1091
    （ER_CANT_DROP_FIELD_OR_KEY），而引擎白名单只有 1060/1061，一条不存在的
    索引就会让整个迁移失败、服务拒启动。所以先查
    ``information_schema.statistics`` 确认在场再删 —— 幂等且对任意历史形态
    安全。SQLite 侧无需处理（v003 重建表时该约束已随旧表一起消失），hook 里按
    方言短路。
    """
    if ex.dialect != "mysql":
        return
    rows = await ex.fetchall(
        "SELECT index_name FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = 'access_tokens' "
        "AND index_name = ?",
        (_LEGACY_UNIQUE_TOKEN_INDEX,),
    )
    if not rows:
        return
    await ex.execute(f"DROP INDEX {_LEGACY_UNIQUE_TOKEN_INDEX} ON access_tokens")


MIGRATION = Migration(
    version=16,
    name="access_token_prefix_index",
    sqlite_sql=SQLITE_SQL,
    mysql_sql=MYSQL_SQL,
    sqlite_baseline_sql=SQLITE_SQL,
    mysql_baseline_sql=MYSQL_SQL,
    hook=_drop_legacy_unique_token_index,
)
