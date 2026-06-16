"""Logs API."""
from __future__ import annotations

from aiohttp import web

from ..store.logs import list_logs


def register_routes(app: web.Application, ctx) -> None:
    async def logs(request):
        cursor = int(request.query.get("cursor", 0))
        limit = int(request.query.get("limit", 50))
        model = request.query.get("model")
        status = request.query.get("status")
        result = await list_logs(
            ctx.store, cursor=cursor, limit=limit,
            model=model, status=int(status) if status else None,
        )
        return web.json_response(result)

    app.router.add_get("/api/logs", logs)