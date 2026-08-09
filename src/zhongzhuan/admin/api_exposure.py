"""Codex 暴露（模型 / 分组可见性）批量读写 API。

后台「暴露管理」tab 用：一次性读取当前所有模型 / 分组的暴露开关，
并支持一次保存批量勾选结果。

说明：``mf`` 是 WorkBuddy 内部聚合分组，永远不暴露给 Codex，故 GET
响应里直接不返回它，UI 也不会出现这个勾选项。
"""

from __future__ import annotations

from aiohttp import web

from ..store.groups import GroupData, list_groups, update_group
from ..store.models import list_models, update_model
from .notify import notify_proxy_reload

#: 内部聚合分组，永不暴露给 Codex。
_INTERNAL_GROUPS = frozenset({"mf"})


def register_routes(app: web.Application, ctx) -> None:
    async def get_(request):
        ms = await list_models(ctx.store)
        models = [
            {
                "id": m.id,
                "name": m.name,
                "exposed": bool(getattr(m, "exposed", True)),
                "is_fallback": bool(getattr(m, "is_fallback", False)),
                "enabled": bool(getattr(m, "enabled", True)),
            }
            for m in ms
        ]
        gs = await list_groups(ctx.store)
        groups = [
            {
                "id": g["id"],
                "name": g["name"],
                "exposed": bool(g.get("exposed", True)),
                "members": len(g.get("members") or []),
            }
            for g in gs
            if g.get("name") not in _INTERNAL_GROUPS
        ]
        return web.json_response({"models": models, "groups": groups})

    async def save(request):
        body = await request.json()
        model_flags: dict = body.get("models") or {}
        group_flags: dict = body.get("groups") or {}

        ms = {m.id: m for m in await list_models(ctx.store)}
        for mid_str, flag in model_flags.items():
            try:
                mid = int(mid_str)
            except (TypeError, ValueError):
                continue
            m = ms.get(mid)
            if m is None:
                continue
            m.exposed = bool(flag)
            await update_model(ctx.store, mid, m)

        gs = {g["id"]: g for g in await list_groups(ctx.store)}
        for gid_str, flag in group_flags.items():
            try:
                gid = int(gid_str)
            except (TypeError, ValueError):
                continue
            g = gs.get(gid)
            if g is None or g.get("name") in _INTERNAL_GROUPS:
                continue
            gd = GroupData(
                name=g["name"],
                strategy=g["strategy"],
                fallback_enabled=g.get("fallback_enabled", True),
                exposed=bool(flag),
                id=gid,
            )
            await update_group(ctx.store, gid, gd)

        await notify_proxy_reload()
        return web.json_response({"ok": True, "saved": {
            "models": len(model_flags),
            "groups": len(group_flags),
        }})

    app.router.add_get("/api/exposure", get_)
    app.router.add_post("/api/exposure", save)
