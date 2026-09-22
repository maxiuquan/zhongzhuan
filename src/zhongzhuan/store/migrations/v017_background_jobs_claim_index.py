"""v017 -- background_jobs(status, lease_until) 认领热路径索引。

动机
----
v3 后台 worker 的空闲循环**每秒**跑一次认领探测
（``BackgroundJobStore.peek_claimable`` / ``claim_job`` /
``_reap_exhausted`` 共用同一谓词）::

    WHERE status IN ('queued', 'in_progress') AND lease_until < ?
      AND (expires_at = 0 OR expires_at > ?) AND attempt < ?

v004 建表时只给了 ``idx_bt_ws(workspace_id, status)`` 与 ``idx_bt_expires(expires_at)``，
两个索引对这个谓词都**不可用** —— 现网 ``EXPLAIN`` 实测为
``TableFullScan (stats:pseudo, estRows 10000) + TopN``。

后果（2026-09-22 定案）：TiDB Cloud 对**扫描型**语句的计费实测是
``EXPLAIN ANALYZE`` 值的 100–200 倍，而这条语句在全库是唯一「每秒一次的全表
扫描」。控制台 ``Statements`` 面板实测：

* 执行次数 252.3K（≈0.97/s × 3 天，与 1s 轮询吻合）
* 该语句 RU **12,397K = 全窗口 98.6%** → 折算 ≈4.13M RU/天 ≈124M RU/月
* 免费额度 50M RU/月 → **12.1 天烧穿**，与历史「每月 13 号爆额度」一致

``docs/v3/02-架构设计与任务分解.md`` 第 1008 行早就规定了
``idx_bgj_claim (status, lease_expires_at)``，只是 v004 实际建表时漏了 ——
这是一次 **schema 漂移**，不是设计缺失。

幂等
----
* SQLite：``CREATE INDEX IF NOT EXISTS``。
* MySQL / TiDB：裸 ``CREATE INDEX``，重复建由引擎 errno 1061
  （``ER_DUP_KEYNAME``）白名单吞掉（与 v001/v007/v016 同一既定规则）。

同类先例
--------
``v016_access_token_prefix_index.py`` 修的是同一类问题（认证热路径
``access_tokens.token_prefix`` 无索引），本迁移直接沿用其形态。
"""

from __future__ import annotations

from ..migration_engine import Migration

SQLITE_SQL: tuple[str, ...] = ("CREATE INDEX IF NOT EXISTS idx_bgj_claim ON background_jobs(status, lease_until)",)

MYSQL_SQL: tuple[str, ...] = ("CREATE INDEX idx_bgj_claim ON background_jobs(status, lease_until)",)

MIGRATION = Migration(
    version=17,
    name="background_jobs_claim_index",
    sqlite_sql=SQLITE_SQL,
    mysql_sql=MYSQL_SQL,
    sqlite_baseline_sql=SQLITE_SQL,
    mysql_baseline_sql=MYSQL_SQL,
)
