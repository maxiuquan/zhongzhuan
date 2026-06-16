"""Stats API."""
from __future__ import annotations

from aiohttp import web

from ..store.logs import get_stats


def register_routes(app: web.Application, ctx) -> None:
    async def stats(request):
        range_h = int(request.query.get("range", "1").rstrip("h"))
        s = await get_stats(ctx.store, range_hours=range_h)
        return web.json_response(s)

    app.router.add_get("/api/stats", stats)