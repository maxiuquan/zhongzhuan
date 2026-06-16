"""Config export/import API."""
from __future__ import annotations

import io
import json
import zipfile

import yaml
from aiohttp import web

from ..config import load_config, save_config
from .notify import notify_proxy_reload
from ..store.keys import list_keys
from ..store.models import list_models


def register_routes(app: web.Application, ctx) -> None:
    async def export_config(_request):
        """Export config + models + keys as zip."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # config.yaml
            if ctx.config:
                zf.writestr("config.yaml", yaml.safe_dump({
                    "server": {
                        "proxy": {"host": ctx.config.server.proxy.host, "port": ctx.config.server.proxy.port},
                        "admin": {"host": ctx.config.server.admin.host, "port": ctx.config.server.admin.port},
                    },
                    "limits": {
                        "global_concurrent": ctx.config.limits.global_concurrent,
                        "default_rpm_per_key": ctx.config.limits.default_rpm_per_key,
                    },
                }, allow_unicode=True))
            # models.json
            models = [_model_dict(m) for m in await list_models(ctx.store)]
            zf.writestr("models.json", json.dumps(models, ensure_ascii=False, indent=2))
            # keys.json (decrypted)
            keys = []
            for k in await list_keys(ctx.store):
                keys.append({
                    "model_id": k.model_id, "label": k.label,
                    "key_masked": k.key_masked,
                    "enabled": k.enabled, "priority": k.priority,
                })
            zf.writestr("keys.json", json.dumps(keys, ensure_ascii=False, indent=2))
        buf.seek(0)
        return web.Response(
            body=buf.read(),
            content_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=zhongzhuan-export.zip"},
        )

    async def import_config(request):
        """Import config from uploaded zip."""
        data = await request.read()
        buf = io.BytesIO(data)
        with zipfile.ZipFile(buf, "r") as zf:
            # Models
            if "models.json" in zf.namelist():
                models_data = json.loads(zf.read("models.json"))
                from ..store.models import Model, create_model, list_models as lm, delete_model
                existing = await lm(ctx.store)
                for m in existing:
                    await delete_model(ctx.store, m.id)
                for md in models_data:
                    await create_model(ctx.store, Model(
                        name=md["name"], upstream_base=md["upstream_base"],
                        upstream_model=md["upstream_model"],
                        rpm_limit=md.get("rpm_limit", 0),
                        tpm_limit=md.get("tpm_limit", 0),
                        enabled=md.get("enabled", True),
                        weight=md.get("weight", 1),
                    ))
            # Keys
            if "keys.json" in zf.namelist():
                keys_data = json.loads(zf.read("keys.json"))
                from ..store.keys import ApiKey, create_key, list_keys as lk, delete_key
                all_keys = await lk(ctx.store)
                for k in all_keys:
                    await delete_key(ctx.store, k.id)
                for kd in keys_data:
                    if "key_value" in kd:
                        await create_key(ctx.store, ApiKey(
                            id=None, model_id=kd["model_id"],
                            label=kd.get("label", ""),
                            key_value=kd["key_value"],
                            enabled=kd.get("enabled", True),
                            priority=kd.get("priority", 0),
                        ))
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    app.router.add_get("/api/export", export_config)
    app.router.add_post("/api/import", import_config)
    app.router.add_get("/api/config", _config_info)


def _model_dict(m) -> dict:
    return {
        "id": m.id, "name": m.name,
        "upstream_base": m.upstream_base, "upstream_model": m.upstream_model,
        "rpm_limit": m.rpm_limit, "tpm_limit": m.tpm_limit,
        "enabled": m.enabled, "weight": m.weight,
    }


async def _config_info(request):
    ctx = request.app.get("ctx")
    if ctx and ctx.config:
        return web.json_response({
            "proxy": {"host": ctx.config.server.proxy.host, "port": ctx.config.server.proxy.port},
            "admin": {"host": ctx.config.server.admin.host, "port": ctx.config.server.admin.port},
            "global_concurrent": ctx.config.limits.global_concurrent,
        })
    return web.json_response({"ok": True})