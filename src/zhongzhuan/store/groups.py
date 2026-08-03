"""Group CRUD (async)."""
from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass
class GroupData:
    name: str
    strategy: str
    fallback_enabled: bool = True
    id: int | None = None
    created_at: int | None = None


@dataclass
class GroupMemberData:
    group_id: int
    model_id: int
    weight: int = 1
    ord: int = 0


async def create_group(s: Store, g: GroupData) -> GroupData:
    now = Store.now()
    g.id = await s.execute(
        "INSERT INTO model_groups(name, strategy, fallback_enabled, created_at) VALUES(?,?,?,?)",
        (g.name, g.strategy, int(g.fallback_enabled), now),
    )
    g.created_at = now
    return g


async def list_groups(s: Store) -> list[dict]:
    rows = await s.fetchall(
        "SELECT id, name, strategy, fallback_enabled, created_at FROM model_groups ORDER BY id"
    )
    result = []
    for r in rows:
        members = await s.fetchall(
            "SELECT model_id, weight, ord FROM group_models WHERE group_id=? ORDER BY ord",
            (r[0],),
        )
        result.append({
            "id": r[0], "name": r[1], "strategy": r[2],
            "fallback_enabled": bool(r[3]), "created_at": r[4],
            "members": [{"model_id": m[0], "weight": m[1], "ord": m[2]} for m in members],
        })
    return result


async def get_group(s: Store, name: str) -> dict | None:
    r = await s.fetchone(
        "SELECT id, name, strategy, fallback_enabled, created_at FROM model_groups WHERE name=?",
        (name,),
    )
    if not r:
        return None
    members = await s.fetchall(
        "SELECT model_id, weight, ord FROM group_models WHERE group_id=? ORDER BY ord",
        (r[0],),
    )
    return {
        "id": r[0], "name": r[1], "strategy": r[2],
        "fallback_enabled": bool(r[3]), "created_at": r[4],
        "members": [{"model_id": m[0], "weight": m[1], "ord": m[2]} for m in members],
    }


async def update_group(s: Store, group_id: int, g: GroupData) -> None:
    await s.execute(
        "UPDATE model_groups SET name=?, strategy=?, fallback_enabled=? WHERE id=?",
        (g.name, g.strategy, int(g.fallback_enabled), group_id),
    )


async def set_group_members(s: Store, group_id: int, members: list[GroupMemberData]) -> None:
    await s.execute("DELETE FROM group_models WHERE group_id=?", (group_id,))
    for m in members:
        await s.execute(
            "INSERT INTO group_models(group_id, model_id, weight, ord) VALUES(?,?,?,?)",
            (group_id, m.model_id, m.weight, m.ord),
        )


async def delete_group(s: Store, group_id: int) -> None:
    await s.execute("DELETE FROM model_groups WHERE id=?", (group_id,))