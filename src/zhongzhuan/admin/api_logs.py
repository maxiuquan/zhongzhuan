"""Logs API."""

from __future__ import annotations

from aiohttp import web

from ..store.logs import list_logs


def register_routes(app: web.Application, ctx) -> None:
    async def logs(request):
        def _int(name: str, default: int) -> int:
            try:
                return int(request.query.get(name, default))
            except (TypeError, ValueError):
                return default

        cursor = max(0, _int("cursor", 0))
        limit = min(500, max(1, _int("limit", 50)))
        model = request.query.get("model")
        status_raw = request.query.get("status")
        status: int | None
        try:
            status = int(status_raw) if status_raw else None
        except ValueError:
            status = None
        result = await list_logs(
            ctx.store,
            cursor=cursor,
            limit=limit,
            model=model,
            status=status,
        )
        return web.json_response(result)

    app.router.add_get("/api/logs", logs)
