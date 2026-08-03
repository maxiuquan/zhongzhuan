"""Admin auth API: login / status."""
from __future__ import annotations

from aiohttp import web

from ..store.admin_users import verify_admin, admin_exists, update_password
from .auth import create_token, auth_enabled, verify_token


def register_routes(app: web.Application, ctx) -> None:
    async def login(request):
        data = await request.json()
        username = data.get("username", "")
        password = data.get("password", "")

        if not await verify_admin(ctx.store, username, password):
            return web.json_response(
                {"error": "invalid credentials"}, status=401,
            )

        token = create_token(username)
        return web.json_response({"token": token, "username": username})

    async def status(_request):
        return web.json_response({"auth_enabled": auth_enabled()})

    async def me(request):
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        username = verify_token(token)
        return web.json_response({"username": username, "auth_enabled": auth_enabled()})

    async def change_password(request):
        data = await request.json()
        username = data.get("username", "")
        old_password = data.get("old_password", "")
        new_password = data.get("new_password", "")

        if not await verify_admin(ctx.store, username, old_password):
            return web.json_response(
                {"error": "invalid current password"}, status=401,
            )

        await update_password(ctx.store, username, new_password)
        return web.json_response({"ok": True})

    app.router.add_post("/api/auth/login", login)
    app.router.add_get("/api/auth/status", status)
    app.router.add_get("/api/auth/me", me)
    app.router.add_post("/api/auth/change-password", change_password)