"""Admin user CRUD (async)."""

from __future__ import annotations

import asyncio

import bcrypt

from .store import Store


async def create_admin(s: Store, username: str, password: str) -> None:
    """Create a new admin user with bcrypt hashed password."""
    password_hash = await asyncio.to_thread(bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt())
    now = Store.now()
    await s.execute(
        "INSERT INTO admin_users(username, password_hash, created_at) VALUES(?,?,?)",
        (username, password_hash.decode("utf-8"), now),
    )


async def verify_admin(s: Store, username: str, password: str) -> bool:
    """Verify admin credentials."""
    r = await s.fetchone("SELECT password_hash FROM admin_users WHERE username=?", (username,))
    if not r:
        return False
    # bcrypt 的 KDF 故意设计得慢（数百毫秒级）；同步调用会冻结整个事件循环，
    # 登录窗口内所有并发请求（含正在流式转发的响应）一起卡顿，故移入工作线程。
    return await asyncio.to_thread(bcrypt.checkpw, password.encode("utf-8"), str(r[0]).encode("utf-8"))


async def admin_exists(s: Store) -> bool:
    """Check if any admin user exists."""
    r = await s.fetchone("SELECT COUNT(*) FROM admin_users")
    return r[0] > 0 if r else False


async def update_password(s: Store, username: str, new_password: str) -> None:
    """Update admin password."""
    password_hash = await asyncio.to_thread(bcrypt.hashpw, new_password.encode("utf-8"), bcrypt.gensalt())
    await s.execute(
        "UPDATE admin_users SET password_hash=? WHERE username=?",
        (password_hash.decode("utf-8"), username),
    )
