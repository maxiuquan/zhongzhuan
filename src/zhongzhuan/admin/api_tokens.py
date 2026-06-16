"""Access token CRUD API."""
from __future__ import annotations

from aiohttp import web

from ..store.access_tokens import (
    list_tokens, create_token, delete_token, update_token,
)


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        tokens = await list_tokens(ctx.store)
        return web.json_response({"data": tokens})

    async def create(request):
        data = await request.json()
        label = data.get("label", "")
        t = await create_token(ctx.store, label)
        return web.json_response({
            "id": t.id, "token": t.token, "label": t.label,
            "enabled": t.enabled, "created_at": t.created_at,
        }, status=201)

    async def delete(request):
        token_id = int(request.match_info["id"])
        await delete_token(ctx.store, token_id)
        return web.json_response({"ok": True})

    async def update(request):
        token_id = int(request.match_info["id"])
        data = await request.json()
        await update_token(
            ctx.store, token_id,
            label=data.get("label"),
            enabled=data.get("enabled"),
        )
        return web.json_response({"ok": True})

    app.router.add_get("/api/tokens", list_)
    app.router.add_post("/api/tokens", create)
    app.router.add_delete("/api/tokens/{id}", delete)
    app.router.add_put("/api/tokens/{id}", update)