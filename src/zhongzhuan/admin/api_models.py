"""Model CRUD API."""
from __future__ import annotations

from aiohttp import web

from ..store.models import (
    Model, create_model, get_model, get_model_by_id,
    list_models, update_model, delete_model,
)
from .notify import notify_proxy_reload


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        ms = await list_models(ctx.store)
        return web.json_response({"data": [_to_dict(m) for m in ms]})

    async def create(request):
        data = await request.json()
        m = Model(
            name=data["name"], upstream_base=data["upstream_base"],
            upstream_model=data["upstream_model"],
            rpm_limit=int(data.get("rpm_limit", 0)),
            tpm_limit=int(data.get("tpm_limit", 0)),
            enabled=bool(data.get("enabled", True)),
            weight=int(data.get("weight", 1)),
        )
        m = await create_model(ctx.store, m)
        await notify_proxy_reload()
        return web.json_response(_to_dict(m), status=201)

    async def update(request):
        model_id = int(request.match_info["id"])
        data = await request.json()
        m = Model(
            name=data["name"], upstream_base=data["upstream_base"],
            upstream_model=data["upstream_model"],
            rpm_limit=int(data.get("rpm_limit", 0)),
            tpm_limit=int(data.get("tpm_limit", 0)),
            enabled=bool(data.get("enabled", True)),
            weight=int(data.get("weight", 1)),
        )
        await update_model(ctx.store, model_id, m)
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def delete(request):
        model_id = int(request.match_info["id"])
        await delete_model(ctx.store, model_id)
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    app.router.add_get("/api/models", list_)
    app.router.add_post("/api/models", create)
    app.router.add_put("/api/models/{id}", update)
    app.router.add_delete("/api/models/{id}", delete)


def _to_dict(m: Model) -> dict:
    return {
        "id": m.id, "name": m.name,
        "upstream_base": m.upstream_base, "upstream_model": m.upstream_model,
        "rpm_limit": m.rpm_limit, "tpm_limit": m.tpm_limit,
        "enabled": m.enabled, "weight": m.weight,
        "created_at": m.created_at, "updated_at": m.updated_at,
    }