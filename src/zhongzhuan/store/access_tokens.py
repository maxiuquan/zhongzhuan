"""Access token CRUD (async) — 支持配额管理。

扩展字段：
- quota_tokens: 额度上限（-1=无限）
- used_tokens: 已用 token 数
- model_whitelist: 模型白名单（逗号分隔，空=全部允许）
- expires_at: 到期时间戳（0=永不过期）
"""
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
    quota_tokens: int = -1  # -1 = 无限
    used_tokens: int = 0
    model_whitelist: str = ""  # 逗号分隔，空 = 全部允许
    expires_at: int = 0  # 0 = 永不过期
    created_at: int | None = None

    def check_quota(self, model: str = "") -> tuple[bool, str]:
        """校验令牌是否可用。返回 (ok, reason)。
        - enabled=False → 令牌已禁用
        - expires_at>0 且已过期 → 令牌已过期
        - model_whitelist 非空且 model 不在白名单 → 模型不在白名单
        - quota_tokens>=0 且 used_tokens>=quota_tokens → 配额已用尽
        """
        if not self.enabled:
            return False, "token disabled"
        import time
        if self.expires_at > 0 and time.time() > self.expires_at:
            return False, "token expired"
        if model and self.model_whitelist:
            allowed = [m.strip() for m in self.model_whitelist.split(",") if m.strip()]
            if model not in allowed:
                return False, f"model '{model}' not in whitelist"
        if self.quota_tokens >= 0 and self.used_tokens >= self.quota_tokens:
            return False, "quota exceeded"
        return True, ""


def generate_token() -> str:
    """Generate a random access token prefixed with zz-."""
    return "zz-" + secrets.token_hex(24)


async def create_token(
    s: Store, label: str = "",
    quota_tokens: int = -1,
    model_whitelist: str = "",
    expires_at: int = 0,
) -> AccessToken:
    """Create a new access token."""
    token = generate_token()
    now = Store.now()
    tid = await s.execute(
        "INSERT INTO access_tokens(token, label, enabled, quota_tokens, used_tokens, model_whitelist, expires_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (token, label, 1, quota_tokens, 0, model_whitelist, expires_at, now),
    )
    return AccessToken(
        id=tid, token=token, label=label, enabled=True,
        quota_tokens=quota_tokens, used_tokens=0,
        model_whitelist=model_whitelist, expires_at=expires_at, created_at=now,
    )


async def list_tokens(s: Store) -> list[dict]:
    """List all access tokens with full token values."""
    rows = await s.fetchall(
        "SELECT id, token, label, enabled, quota_tokens, used_tokens, model_whitelist, expires_at, created_at FROM access_tokens ORDER BY id"
    )
    return [
        {
            "id": r[0], "token": r[1], "label": r[2], "enabled": bool(r[3]),
            "quota_tokens": r[4], "used_tokens": r[5],
            "model_whitelist": r[6] if r[6] else "",
            "expires_at": r[7] if r[7] else 0,
            "created_at": r[8],
        }
        for r in rows
    ]


async def get_token_by_value(s: Store, token: str) -> AccessToken | None:
    """根据 token 字符串查询完整 AccessToken 对象（含配额字段）。"""
    r = await s.fetchone(
        "SELECT id, token, label, enabled, quota_tokens, used_tokens, model_whitelist, expires_at, created_at FROM access_tokens WHERE token=?",
        (token,),
    )
    if not r:
        return None
    return AccessToken(
        id=r[0], token=r[1], label=r[2], enabled=bool(r[3]),
        quota_tokens=r[4], used_tokens=r[5],
        model_whitelist=r[6] if r[6] else "",
        expires_at=r[7] if r[7] else 0,
        created_at=r[8],
    )


async def verify_token(s: Store, token: str) -> bool:
    """Verify if an access token is valid and enabled (向后兼容）。"""
    r = await s.fetchone(
        "SELECT enabled FROM access_tokens WHERE token=?", (token,)
    )
    return bool(r) and bool(r[0])


async def delete_token(s: Store, token_id: int) -> None:
    """Delete an access token."""
    await s.execute("DELETE FROM access_tokens WHERE id=?", (token_id,))


async def update_token(
    s: Store, token_id: int, *,
    label: str | None = None,
    enabled: bool | None = None,
    quota_tokens: int | None = None,
    model_whitelist: str | None = None,
    expires_at: int | None = None,
) -> None:
    """Update token fields."""
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if enabled is not None:
        sets.append("enabled=?"); params.append(int(enabled))
    if quota_tokens is not None:
        sets.append("quota_tokens=?"); params.append(quota_tokens)
    if model_whitelist is not None:
        sets.append("model_whitelist=?"); params.append(model_whitelist)
    if expires_at is not None:
        sets.append("expires_at=?"); params.append(expires_at)
    if not sets:
        return
    params.append(token_id)
    await s.execute(f"UPDATE access_tokens SET {','.join(sets)} WHERE id=?", tuple(params))


async def deduct_token_quota(s: Store, token_id: int, tokens: int) -> None:
    """扣减令牌已用 token 数（tokens <= 0 时不扣减）。"""
    if tokens <= 0 or token_id <= 0:
        return
    await s.execute(
        "UPDATE access_tokens SET used_tokens = used_tokens + ? WHERE id=?",
        (tokens, token_id),
    )


async def token_count(s: Store) -> int:
    """Count existing tokens."""
    r = await s.fetchone("SELECT COUNT(*) FROM access_tokens")
    return r[0] if r else 0
