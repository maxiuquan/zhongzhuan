"""Access token CRUD API."""

from __future__ import annotations

from aiohttp import web

from ..store.access_tokens import (
    list_tokens,
    create_token,
    delete_token,
    update_token,
)


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        tokens = await list_tokens(ctx.store)
        return web.json_response({"data": tokens})

    async def create(request):
        data = await request.json()
        label = data.get("label", "")
        quota_tokens = int(data.get("quota_tokens", -1))
        model_whitelist = data.get("model_whitelist", "")
        # expires_at: 接受天数（>0）或时间戳（<=0 表示永不过期）
        expires_days = int(data.get("expires_days", 0))
        import time

        expires_at = int(time.time()) + expires_days * 86400 if expires_days > 0 else 0
        t = await create_token(
            ctx.store,
            label,
            quota_tokens=quota_tokens,
            model_whitelist=model_whitelist,
            expires_at=expires_at,
        )
        return web.json_response(
            {
                "id": t.id,
                "token": t.token,
                "label": t.label,
                "enabled": t.enabled,
                "quota_tokens": t.quota_tokens,
                "used_tokens": t.used_tokens,
                "model_whitelist": t.model_whitelist,
                "expires_at": t.expires_at,
                "created_at": t.created_at,
            },
            status=201,
        )

    async def delete(request):
        token_id = int(request.match_info["id"])
        await delete_token(ctx.store, token_id)
        return web.json_response({"ok": True})

    async def update(request):
        token_id = int(request.match_info["id"])
        data = await request.json()
        kwargs = {}
        if "label" in data:
            kwargs["label"] = data["label"]
        if "enabled" in data:
            kwargs["enabled"] = data["enabled"]
        if "quota_tokens" in data:
            kwargs["quota_tokens"] = int(data["quota_tokens"])
        if "model_whitelist" in data:
            kwargs["model_whitelist"] = data["model_whitelist"]
        if "expires_days" in data:
            import time

            days = int(data["expires_days"])
            kwargs["expires_at"] = int(time.time()) + days * 86400 if days > 0 else 0
        await update_token(ctx.store, token_id, **kwargs)
        return web.json_response({"ok": True})

    app.router.add_get("/api/tokens", list_)
    app.router.add_post("/api/tokens", create)
    app.router.add_delete("/api/tokens/{id}", delete)
    app.router.add_put("/api/tokens/{id}", update)
