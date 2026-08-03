"""Admin user CRUD (async)."""
from __future__ import annotations

import bcrypt

from .store import Store


async def create_admin(s: Store, username: str, password: str) -> None:
    """Create a new admin user with bcrypt hashed password."""
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    now = Store.now()
    await s.execute(
        "INSERT INTO admin_users(username, password_hash, created_at) VALUES(?,?,?)",
        (username, password_hash, now),
    )


async def verify_admin(s: Store, username: str, password: str) -> bool:
    """Verify admin credentials."""
    r = await s.fetchone(
        "SELECT password_hash FROM admin_users WHERE username=?", (username,)
    )
    if not r:
        return False
    return bcrypt.checkpw(password.encode("utf-8"), r[0].encode("utf-8"))


async def admin_exists(s: Store) -> bool:
    """Check if any admin user exists."""
    r = await s.fetchone("SELECT COUNT(*) FROM admin_users")
    return r[0] > 0 if r else False


async def update_password(s: Store, username: str, new_password: str) -> None:
    """Update admin password."""
    password_hash = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    await s.execute(
        "UPDATE admin_users SET password_hash=? WHERE username=?",
        (password_hash, username),
    )