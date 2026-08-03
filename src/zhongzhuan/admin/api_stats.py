"""Stats API."""
from __future__ import annotations

from aiohttp import web

from ..store.logs import get_stats, get_usage_stats


def register_routes(app: web.Application, ctx) -> None:
    async def stats(request):
        range_h = int(request.query.get("range", "1").rstrip("h"))
        s = await get_stats(ctx.store, range_hours=range_h)
        return web.json_response(s)

    async def usage(request):
        days = int(request.query.get("days", "7"))
        if days < 1:
            days = 1
        if days > 90:
            days = 90
        s = await get_usage_stats(ctx.store, days=days)
        return web.json_response(s)

    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/stats/usage", usage)