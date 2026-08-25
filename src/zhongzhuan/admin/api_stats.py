"""Stats API."""

from __future__ import annotations

from aiohttp import web

from ..store.logs import get_stats, get_usage_stats


def register_routes(app: web.Application, ctx) -> None:
    async def stats(request):
        try:
            range_h = int(str(request.query.get("range", "1")).rstrip("h"))
        except (TypeError, ValueError):
            range_h = 1
        range_h = min(720, max(1, range_h))
        s = await get_stats(ctx.store, range_hours=range_h)
        return web.json_response(s)

    async def usage(request):
        try:
            days = int(request.query.get("days", "7"))
        except (TypeError, ValueError):
            days = 7
        if days < 1:
            days = 1
        if days > 90:
            days = 90
        s = await get_usage_stats(ctx.store, days=days)
        return web.json_response(s)

    app.router.add_get("/api/stats", stats)
    app.router.add_get("/api/stats/usage", usage)
