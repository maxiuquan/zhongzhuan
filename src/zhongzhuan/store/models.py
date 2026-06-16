"""Model CRUD."""
from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass
class Model:
    name: str
    upstream_base: str
    upstream_model: str
    rpm_limit: int = 0
    tpm_limit: int = 0
    enabled: bool = True
    weight: int = 1
    id: int | None = None
    created_at: int | None = None
    updated_at: int | None = None


def _row(r: tuple) -> Model:
    return Model(
        id=r[0], name=r[1], upstream_base=r[2], upstream_model=r[3],
        rpm_limit=r[4], tpm_limit=r[5], enabled=bool(r[6]), weight=r[7],
        created_at=r[8], updated_at=r[9],
    )


def create_model(s: Store, m: Model) -> Model:
    now = Store.now()
    cur = s.connect().execute(
        """INSERT INTO models(name, upstream_base, upstream_model, rpm_limit, tpm_limit, enabled, weight, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (m.name, m.upstream_base, m.upstream_model, m.rpm_limit, m.tpm_limit,
         int(m.enabled), m.weight, now, now),
    )
    m.id = cur.lastrowid
    m.created_at = now
    m.updated_at = now
    return m


def get_model(s: Store, name: str) -> Model | None:
    r = s.connect().execute(
        "SELECT id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,created_at,updated_at FROM models WHERE name=?",
        (name,),
    ).fetchone()
    return _row(r) if r else None


def get_model_by_id(s: Store, model_id: int) -> Model | None:
    r = s.connect().execute(
        "SELECT id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,created_at,updated_at FROM models WHERE id=?",
        (model_id,),
    ).fetchone()
    return _row(r) if r else None


def list_models(s: Store) -> list[Model]:
    rows = s.connect().execute(
        "SELECT id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,created_at,updated_at FROM models ORDER BY id"
    ).fetchall()
    return [_row(r) for r in rows]


def update_model(s: Store, model_id: int, m: Model) -> None:
    now = Store.now()
    s.connect().execute(
        """UPDATE models SET name=?, upstream_base=?, upstream_model=?, rpm_limit=?, tpm_limit=?, enabled=?, weight=?, updated_at=? WHERE id=?""",
        (m.name, m.upstream_base, m.upstream_model, m.rpm_limit, m.tpm_limit,
         int(m.enabled), m.weight, now, model_id),
    )


def delete_model(s: Store, model_id: int) -> None:
    s.connect().execute("DELETE FROM models WHERE id=?", (model_id,))