"""Key health state persistence (async).

Stores the in-memory KeyHealth state to SQLite/TiDB so that learned rate
limits, cooldown timers, and success/failure counters survive restarts.

方言注意（UPSERT）
------------------
``INSERT ... ON CONFLICT(key_id) DO UPDATE SET col=excluded.col`` 是 SQLite
专有语法，MySQL / TiDB 会直接语法报错。TiDB 侧的等价形式是
``INSERT ... ON DUPLICATE KEY UPDATE col=VALUES(col)``。:func:`save_health`
按 ``Store.dialect`` 分流生成两条语句 —— 与 ``Store.upsert_into`` 的既有分流
模式一致，但那里复用的是 REPLACE 语义（整行替换），这里必须保列级更新，
所以不能用 ``upsert_into``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store


@dataclass
class KeyHealthRow:
    key_id: int
    status: str
    cooldown_until: float
    rpm_limit: int
    tpm_limit: int
    success_count: int
    failure_count: int
    recent_429_count: int


def _upsert_sql(dialect: str) -> str:
    """按后端方言生成 key_health 的列级 UPSERT。

    两个方言的占位符都由 Store 层统一适配（SQLite 原生 ``?``，TiDB 由
    ``execute`` 把 ``?`` 换成 ``%s``），因此 VALUES 部分两边共用一份。
    """
    if dialect == "mysql":
        # MySQL / TiDB：ON DUPLICATE KEY UPDATE + VALUES(col) 引用新行值。
        return """INSERT INTO key_health(key_id, status, cooldown_until, rpm_limit, tpm_limit,
                                  success_count, failure_count, recent_429_count, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON DUPLICATE KEY UPDATE
             status=VALUES(status), cooldown_until=VALUES(cooldown_until),
             rpm_limit=VALUES(rpm_limit), tpm_limit=VALUES(tpm_limit),
             success_count=VALUES(success_count), failure_count=VALUES(failure_count),
             recent_429_count=VALUES(recent_429_count), updated_at=VALUES(updated_at)"""
    # SQLite：ON CONFLICT ... DO UPDATE + excluded.col 引用新行值。
    return """INSERT INTO key_health(key_id, status, cooldown_until, rpm_limit, tpm_limit,
                                  success_count, failure_count, recent_429_count, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key_id) DO UPDATE SET
             status=excluded.status, cooldown_until=excluded.cooldown_until,
             rpm_limit=excluded.rpm_limit, tpm_limit=excluded.tpm_limit,
             success_count=excluded.success_count, failure_count=excluded.failure_count,
             recent_429_count=excluded.recent_429_count, updated_at=excluded.updated_at"""


async def save_health(s: "Store", r: KeyHealthRow) -> None:
    """Upsert a key health snapshot."""
    now = int(time.time())
    await s.execute(
        _upsert_sql(s.dialect),
        (
            r.key_id,
            r.status,
            r.cooldown_until,
            r.rpm_limit,
            r.tpm_limit,
            r.success_count,
            r.failure_count,
            r.recent_429_count,
            now,
        ),
    )


async def load_all_health(s: "Store") -> dict[int, KeyHealthRow]:
    """Load all key health rows into a dict keyed by key_id."""
    rows = await s.fetchall(
        """SELECT key_id, status, cooldown_until, rpm_limit, tpm_limit,
                  success_count, failure_count, recent_429_count
           FROM key_health"""
    )
    return {
        row[0]: KeyHealthRow(
            key_id=row[0],
            status=row[1],
            cooldown_until=row[2],
            rpm_limit=row[3],
            tpm_limit=row[4],
            success_count=row[5],
            failure_count=row[6],
            recent_429_count=row[7],
        )
        for row in rows
    }


async def delete_health(s: "Store", key_id: int) -> None:
    """Remove a key health row (e.g. when the key is deleted)."""
    await s.execute("DELETE FROM key_health WHERE key_id=?", (key_id,))


async def clear_all_health(s: "Store") -> None:
    """Reset all key health (e.g. admin manual reset)."""
    await s.execute("DELETE FROM key_health")
