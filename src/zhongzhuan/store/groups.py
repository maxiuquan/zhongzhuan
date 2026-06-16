"""Group CRUD."""
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


def create_group(s: Store, g: GroupData) -> GroupData:
    now = Store.now()
    cur = s.connect().execute(
        "INSERT INTO model_groups(name, strategy, fallback_enabled, created_at) VALUES(?,?,?,?)",
        (g.name, g.strategy, int(g.fallback_enabled), now),
    )
    g.id = cur.lastrowid
    g.created_at = now
    return g


def list_groups(s: Store) -> list[dict]:
    rows = s.connect().execute(
        "SELECT id, name, strategy, fallback_enabled, created_at FROM model_groups ORDER BY id"
    ).fetchall()
    result = []
    for r in rows:
        members = s.connect().execute(
            "SELECT model_id, weight, ord FROM group_models WHERE group_id=? ORDER BY ord",
            (r[0],),
        ).fetchall()
        result.append({
            "id": r[0], "name": r[1], "strategy": r[2],
            "fallback_enabled": bool(r[3]), "created_at": r[4],
            "members": [{"model_id": m[0], "weight": m[1], "ord": m[2]} for m in members],
        })
    return result


def get_group(s: Store, name: str) -> dict | None:
    r = s.connect().execute(
        "SELECT id, name, strategy, fallback_enabled, created_at FROM model_groups WHERE name=?",
        (name,),
    ).fetchone()
    if not r:
        return None
    members = s.connect().execute(
        "SELECT model_id, weight, ord FROM group_models WHERE group_id=? ORDER BY ord",
        (r[0],),
    ).fetchall()
    return {
        "id": r[0], "name": r[1], "strategy": r[2],
        "fallback_enabled": bool(r[3]), "created_at": r[4],
        "members": [{"model_id": m[0], "weight": m[1], "ord": m[2]} for m in members],
    }


def update_group(s: Store, group_id: int, g: GroupData) -> None:
    s.connect().execute(
        "UPDATE model_groups SET name=?, strategy=?, fallback_enabled=? WHERE id=?",
        (g.name, g.strategy, int(g.fallback_enabled), group_id),
    )


def set_group_members(s: Store, group_id: int, members: list[GroupMemberData]) -> None:
    s.connect().execute("DELETE FROM group_models WHERE group_id=?", (group_id,))
    for m in members:
        s.connect().execute(
            "INSERT INTO group_models(group_id, model_id, weight, ord) VALUES(?,?,?,?)",
            (group_id, m.model_id, m.weight, m.ord),
        )


def delete_group(s: Store, group_id: int) -> None:
    s.connect().execute("DELETE FROM model_groups WHERE id=?", (group_id,))