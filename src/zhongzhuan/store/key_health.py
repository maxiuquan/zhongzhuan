"""Key health state persistence (async).

Stores the in-memory KeyHealth state to SQLite/TiDB so that learned rate
limits, cooldown timers, and success/failure counters survive restarts.
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


async def save_health(s: "Store", r: KeyHealthRow) -> None:
    """Upsert a key health snapshot."""
    now = int(time.time())
    await s.execute(
        """INSERT INTO key_health(key_id, status, cooldown_until, rpm_limit, tpm_limit,
                                  success_count, failure_count, recent_429_count, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key_id) DO UPDATE SET
             status=excluded.status, cooldown_until=excluded.cooldown_until,
             rpm_limit=excluded.rpm_limit, tpm_limit=excluded.tpm_limit,
             success_count=excluded.success_count, failure_count=excluded.failure_count,
             recent_429_count=excluded.recent_429_count, updated_at=excluded.updated_at""",
        (r.key_id, r.status, r.cooldown_until, r.rpm_limit, r.tpm_limit,
         r.success_count, r.failure_count, r.recent_429_count, now),
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
            key_id=row[0], status=row[1], cooldown_until=row[2],
            rpm_limit=row[3], tpm_limit=row[4], success_count=row[5],
            failure_count=row[6], recent_429_count=row[7],
        )
        for row in rows
    }


async def delete_health(s: "Store", key_id: int) -> None:
    """Remove a key health row (e.g. when the key is deleted)."""
    await s.execute("DELETE FROM key_health WHERE key_id=?", (key_id,))


async def clear_all_health(s: "Store") -> None:
    """Reset all key health (e.g. admin manual reset)."""
    await s.execute("DELETE FROM key_health")
