"""Fallback upstream management API.

提供兜底上游的配置查看/修改、模型刷新端点。
降权系数(fallback_penalty)和启用开关持久化到 system_config 表，可在后台直接修改。
"""

from __future__ import annotations

from aiohttp import web

from .notify import notify_proxy_reload


# system_config 表中的 key 名
_KEY_ENABLED = "fallback_enabled"
_KEY_PENALTY = "fallback_penalty"


async def _get_config_value(store, key: str) -> str | None:
    row = await store.fetchone("SELECT value FROM system_config WHERE `key`=?", (key,))
    return row[0] if row else None


async def _set_config_value(store, key: str, value: str) -> None:
    # 跨数据库兼容：先 DELETE 再 INSERT（SQLite 和 MySQL/TiDB 都支持）
    await store.execute("DELETE FROM system_config WHERE `key`=?", (key,))
    await store.execute("INSERT INTO system_config(`key`, value) VALUES(?, ?)", (key, value))


def register_routes(app: web.Application, ctx) -> None:
    async def refresh(request):
        """POST /api/fallback/refresh — 重新拉取并同步 OpenCode Free 兜底模型。"""
        cfg = ctx.config
        if cfg is None or not cfg.fallback.enabled:
            return web.json_response(
                {"error": {"message": "兜底上游未启用 (fallback.enabled=false)", "type": "disabled"}},
                status=400,
            )
        from ..__main__ import _fetch_opencode_models, _sync_fallback_models

        try:
            model_ids = await _fetch_opencode_models(cfg)
            upserted = await _sync_fallback_models(ctx.store, cfg, model_ids)
            await notify_proxy_reload()
            return web.json_response(
                {
                    "ok": True,
                    "synced": len(upserted),
                    "models": model_ids,
                }
            )
        except Exception as e:
            return web.json_response(
                {"error": {"message": str(e), "type": "refresh_failed"}},
                status=500,
            )

    async def status(request):
        """GET /api/fallback/status — 返回兜底配置 + 当前 DB 中的兜底模型列表。"""
        from ..store.models import list_models

        cfg = ctx.config
        # DB 中持久化的配置优先于 config.yaml
        db_enabled = await _get_config_value(ctx.store, _KEY_ENABLED)
        db_penalty = await _get_config_value(ctx.store, _KEY_PENALTY)
        enabled = (db_enabled == "1") if db_enabled is not None else (cfg.fallback.enabled if cfg else False)
        penalty = float(db_penalty) if db_penalty is not None else (cfg.fallback.fallback_penalty if cfg else 0.1)

        all_models = await list_models(ctx.store)
        fallback_models = [m for m in all_models if m.is_fallback]
        return web.json_response(
            {
                "enabled": enabled,
                "upstream_base": cfg.fallback.upstream_base if cfg else "",
                "model_prefix": cfg.fallback.model_prefix if cfg else "oc-",
                "fallback_penalty": penalty,
                "fallback_model_count": len(fallback_models),
                "fallback_models": [
                    {"id": m.id, "name": m.name, "enabled": m.enabled, "upstream_model": m.upstream_model}
                    for m in fallback_models
                ],
            }
        )

    async def update_config(request):
        """PUT /api/fallback/config — 修改兜底配置（启用开关 + 降权系数）。

        请求体: {"enabled": bool, "fallback_penalty": float}
        持久化到 system_config 表，同时更新内存中的 cfg 对象，并通知 proxy reload。
        """
        data = await request.json()
        cfg = ctx.config
        if cfg is None:
            return web.json_response(
                {"error": {"message": "配置不可用", "type": "no_config"}},
                status=500,
            )

        # 更新启用开关
        if "enabled" in data:
            enabled = bool(data["enabled"])
            await _set_config_value(ctx.store, _KEY_ENABLED, "1" if enabled else "0")
            cfg.fallback.enabled = enabled

        # 更新降权系数（0.01 ~ 1.0）
        if "fallback_penalty" in data:
            penalty = float(data["fallback_penalty"])
            penalty = max(0.01, min(1.0, penalty))  # 限制范围
            await _set_config_value(ctx.store, _KEY_PENALTY, str(penalty))
            cfg.fallback.fallback_penalty = penalty

        # 通知 proxy reload，让 _load_keys_from_store 重新读取 DB 配置
        await notify_proxy_reload()
        return web.json_response(
            {
                "ok": True,
                "enabled": cfg.fallback.enabled,
                "fallback_penalty": cfg.fallback.fallback_penalty,
            }
        )

    app.router.add_post("/api/fallback/refresh", refresh)
    app.router.add_get("/api/fallback/status", status)
    app.router.add_put("/api/fallback/config", update_config)
