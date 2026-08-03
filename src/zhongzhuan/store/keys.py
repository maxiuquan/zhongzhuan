"""API Key CRUD (async)."""
from __future__ import annotations

from dataclasses import dataclass

from ..crypto import encrypt, decrypt, mask
from .store import Store


@dataclass
class ApiKey:
    id: int | None
    model_id: int
    label: str
    key_value: str
    enabled: bool = True
    priority: int = 0
    created_at: int | None = None


@dataclass
class ApiKeyRow:
    id: int
    model_id: int
    label: str
    key_masked: str
    enabled: bool
    priority: int
    created_at: int


async def create_key(s: Store, k: ApiKey) -> ApiKey:
    cipher = encrypt(k.key_value.encode("utf-8"))
    now = Store.now()
    k.id = await s.execute(
        "INSERT INTO api_keys(model_id, label, key_cipher, enabled, priority, created_at) VALUES(?,?,?,?,?,?)",
        (k.model_id, k.label, cipher, int(k.enabled), k.priority, now),
    )
    k.created_at = now
    return k


async def list_keys(s: Store, model_id: int | None = None) -> list[ApiKeyRow]:
    if model_id is None:
        rows = await s.fetchall(
            "SELECT id,model_id,label,key_cipher,enabled,priority,created_at FROM api_keys ORDER BY id"
        )
    else:
        rows = await s.fetchall(
            "SELECT id,model_id,label,key_cipher,enabled,priority,created_at FROM api_keys WHERE model_id=? ORDER BY id",
            (model_id,),
        )
    out = []
    for row in rows:
        plain = decrypt(row[3]).decode("utf-8", errors="replace")
        out.append(ApiKeyRow(
            id=row[0], model_id=row[1], label=row[2], key_masked=mask(plain),
            enabled=bool(row[4]), priority=row[5], created_at=row[6],
        ))
    return out


async def get_key_cipher(s: Store, key_id: int) -> str | None:
    r = await s.fetchone("SELECT key_cipher FROM api_keys WHERE id=?", (key_id,))
    return decrypt(r[0]).decode("utf-8") if r else None


async def delete_key(s: Store, key_id: int) -> None:
    await s.execute("DELETE FROM api_keys WHERE id=?", (key_id,))


async def update_key(s: Store, key_id: int, *, label: str | None = None, enabled: bool | None = None, priority: int | None = None) -> None:
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if enabled is not None:
        sets.append("enabled=?"); params.append(int(enabled))
    if priority is not None:
        sets.append("priority=?"); params.append(priority)
    if not sets:
        return
    params.append(key_id)
    await s.execute(f"UPDATE api_keys SET {','.join(sets)} WHERE id=?", tuple(params))