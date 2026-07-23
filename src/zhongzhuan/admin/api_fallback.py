"""Fallback upstream management API.

提供「刷新兜底模型」端点：重新从 OpenCode Free 拉取免费模型列表并 upsert 到 DB。
"""
from __future__ import annotations

from aiohttp import web

from .notify import notify_proxy_reload


def register_routes(app: web.Application, ctx) -> None:
    async def refresh(request):
        """POST /api/fallback/refresh — 重新拉取并同步 OpenCode Free 兜底模型。"""
        cfg = ctx.config
        if cfg is None or not cfg.fallback.enabled:
            return web.json_response(
                {"error": {"message": "兜底上游未启用 (fallback.enabled=false)", "type": "disabled"}},
                status=400,
            )
        # 延迟导入避免循环依赖
        from ..__main__ import _fetch_opencode_models, _sync_fallback_models
        try:
            model_ids = await _fetch_opencode_models(cfg)
            upserted = await _sync_fallback_models(ctx.store, cfg, model_ids)
            await notify_proxy_reload()
            return web.json_response({
                "ok": True,
                "synced": len(upserted),
                "models": model_ids,
            })
        except Exception as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "refresh_failed"}},
                status=500,
            )

    async def status(request):
        """GET /api/fallback/status — 返回兜底配置 + 当前 DB 中的兜底模型数量。"""
        from ..store.models import list_models
        cfg = ctx.config
        fb = cfg.fallback if cfg else None
        all_models = await list_models(ctx.store)
        fallback_models = [m for m in all_models if m.is_fallback]
        return web.json_response({
            "enabled": bool(fb.enabled) if fb else False,
            "upstream_base": fb.upstream_base if fb else "",
            "model_prefix": fb.model_prefix if fb else "oc-",
            "fallback_penalty": fb.fallback_penalty if fb else 0.1,
            "fallback_model_count": len(fallback_models),
            "fallback_models": [
                {"id": m.id, "name": m.name, "enabled": m.enabled, "upstream_model": m.upstream_model}
                for m in fallback_models
            ],
        })

    app.router.add_post("/api/fallback/refresh", refresh)
    app.router.add_get("/api/fallback/status", status)
