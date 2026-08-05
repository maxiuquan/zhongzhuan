"""Access token CRUD API."""

from __future__ import annotations

from aiohttp import web

from ..store.access_tokens import (
    list_tokens,
    create_token,
    delete_token,
    update_token,
    reveal_token,
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

    async def reveal(request):
        """Return the plaintext token for the copy button.

        The list endpoint stays masked; the full key is only delivered here,
        behind the same JWT + CSRF guards as every other admin write.  A token
        created before v010 has no recoverable plaintext -- we answer 404 with
        a clear message instead of guessing or rotating the key.
        """
        token_id = int(request.match_info["id"])
        plaintext = await reveal_token(ctx.store, token_id)
        if plaintext is None:
            return web.json_response(
                {
                    "error": {
                        "message": "该令牌创建于安全哈希迁移之前，原始 Key 未留存，无法复制。"
                        "可在使用该 Key 的下游客户端中复制原值后，重新创建同名令牌。",
                        "type": "token_not_recoverable",
                    }
                },
                status=404,
            )
        return web.json_response({"id": token_id, "token": plaintext})

    app.router.add_get("/api/tokens", list_)
    app.router.add_post("/api/tokens", create)
    app.router.add_delete("/api/tokens/{id}", delete)
    app.router.add_put("/api/tokens/{id}", update)
    app.router.add_post("/api/tokens/{id}/reveal", reveal)
