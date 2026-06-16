"""Group CRUD API."""
from __future__ import annotations

from aiohttp import web

from ..store.groups import (
    GroupData, GroupMemberData,
    create_group, list_groups, get_group, update_group, set_group_members, delete_group,
)


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        groups = list_groups(ctx.store)
        return web.json_response({"data": groups})

    async def create(request):
        data = await request.json()
        g = GroupData(
            name=data["name"], strategy=data["strategy"],
            fallback_enabled=bool(data.get("fallback_enabled", True)),
        )
        g = create_group(ctx.store, g)
        members = data.get("members", [])
        if members:
            set_group_members(ctx.store, g.id, [
                GroupMemberData(
                    group_id=g.id, model_id=m["model_id"],
                    weight=m.get("weight", 1), ord=m.get("ord", i),
                )
                for i, m in enumerate(members)
            ])
        return web.json_response(get_group(ctx.store, g.name), status=201)

    async def update(request):
        group_id = int(request.match_info["id"])
        data = await request.json()
        g = GroupData(
            name=data["name"], strategy=data["strategy"],
            fallback_enabled=bool(data.get("fallback_enabled", True)),
        )
        update_group(ctx.store, group_id, g)
        members = data.get("members", [])
        if members:
            set_group_members(ctx.store, group_id, [
                GroupMemberData(
                    group_id=group_id, model_id=m["model_id"],
                    weight=m.get("weight", 1), ord=m.get("ord", i),
                )
                for i, m in enumerate(members)
            ])
        return web.json_response({"ok": True})

    async def delete(request):
        group_id = int(request.match_info["id"])
        delete_group(ctx.store, group_id)
        return web.json_response({"ok": True})

    app.router.add_get("/api/groups", list_)
    app.router.add_post("/api/groups", create)
    app.router.add_put("/api/groups/{id}", update)
    app.router.add_delete("/api/groups/{id}", delete)