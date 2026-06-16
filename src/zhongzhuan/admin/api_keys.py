"""Key CRUD API."""
from __future__ import annotations

from aiohttp import web

from ..crypto import mask
from ..store.keys import ApiKey, create_key, list_keys, delete_key, update_key


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        model_id = request.query.get("model_id")
        rows = list_keys(ctx.store, int(model_id) if model_id else None)
        return web.json_response({"data": [
            {
                "id": r.id, "model_id": r.model_id, "label": r.label,
                "key_masked": r.key_masked, "enabled": r.enabled,
                "priority": r.priority, "created_at": r.created_at,
            }
            for r in rows
        ]})

    async def create(request):
        data = await request.json()
        k = ApiKey(
            id=None, model_id=int(data["model_id"]),
            label=data.get("label", ""), key_value=data["key_value"],
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 0)),
        )
        k = create_key(ctx.store, k)
        return web.json_response({
            "id": k.id, "model_id": k.model_id, "label": k.label,
            "key_masked": mask(k.key_value), "enabled": k.enabled,
            "priority": k.priority, "created_at": k.created_at,
        }, status=201)

    async def delete(request):
        key_id = int(request.match_info["id"])
        delete_key(ctx.store, key_id)
        return web.json_response({"ok": True})

    async def update(request):
        key_id = int(request.match_info["id"])
        data = await request.json()
        update_key(
            ctx.store, key_id,
            label=data.get("label"),
            enabled=data.get("enabled"),
            priority=data.get("priority"),
        )
        return web.json_response({"ok": True})

    app.router.add_get("/api/keys", list_)
    app.router.add_post("/api/keys", create)
    app.router.add_put("/api/keys/{id}", update)
    app.router.add_delete("/api/keys/{id}", delete)