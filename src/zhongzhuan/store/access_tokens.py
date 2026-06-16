"""Access token CRUD (async)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .store import Store


@dataclass
class AccessToken:
    id: int | None
    token: str
    label: str
    enabled: bool = True
    created_at: int | None = None


def generate_token() -> str:
    """Generate a random access token prefixed with zz-."""
    return "zz-" + secrets.token_hex(24)


async def create_token(s: Store, label: str = "") -> AccessToken:
    """Create a new access token."""
    token = generate_token()
    now = Store.now()
    tid = await s.execute(
        "INSERT INTO access_tokens(token, label, enabled, created_at) VALUES(?,?,?,?)",
        (token, label, 1, now),
    )
    return AccessToken(id=tid, token=token, label=label, enabled=True, created_at=now)


async def list_tokens(s: Store) -> list[dict]:
    """List all access tokens with full token values."""
    rows = await s.fetchall(
        "SELECT id, token, label, enabled, created_at FROM access_tokens ORDER BY id"
    )
    return [
        {"id": r[0], "token": r[1], "label": r[2], "enabled": bool(r[3]), "created_at": r[4]}
        for r in rows
    ]


async def verify_token(s: Store, token: str) -> bool:
    """Verify if an access token is valid and enabled."""
    r = await s.fetchone(
        "SELECT enabled FROM access_tokens WHERE token=?", (token,)
    )
    return bool(r) and bool(r[0])


async def delete_token(s: Store, token_id: int) -> None:
    """Delete an access token."""
    await s.execute("DELETE FROM access_tokens WHERE id=?", (token_id,))


async def update_token(s: Store, token_id: int, *, label: str | None = None, enabled: bool | None = None) -> None:
    """Update token label or enabled status."""
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if enabled is not None:
        sets.append("enabled=?"); params.append(int(enabled))
    if not sets:
        return
    params.append(token_id)
    await s.execute(f"UPDATE access_tokens SET {','.join(sets)} WHERE id=?", tuple(params))


async def token_count(s: Store) -> int:
    """Count existing tokens."""
    r = await s.fetchone("SELECT COUNT(*) FROM access_tokens")
    return r[0] if r else 0