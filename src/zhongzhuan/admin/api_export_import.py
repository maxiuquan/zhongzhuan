"""Config export/import API."""

from __future__ import annotations

import io
import json
import zipfile

import yaml
from aiohttp import web

from .notify import notify_proxy_reload
from ..store.keys import list_keys
from ..store.models import list_models

# 导入 zip 的安全上限：成员数与解压后累计字节数（防 zip 炸弹）
_MAX_IMPORT_MEMBERS = 64
_MAX_IMPORT_BYTES = 32 * 1024 * 1024


def register_routes(app: web.Application, ctx) -> None:
    async def export_config(_request):
        """Export config + models + keys as zip."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # config.yaml
            if ctx.config:
                zf.writestr(
                    "config.yaml",
                    yaml.safe_dump(
                        {
                            "server": {
                                "proxy": {"host": ctx.config.server.proxy.host, "port": ctx.config.server.proxy.port},
                                "admin": {"host": ctx.config.server.admin.host, "port": ctx.config.server.admin.port},
                            },
                            "limits": {
                                "global_concurrent": ctx.config.limits.global_concurrent,
                                "default_rpm_per_key": ctx.config.limits.default_rpm_per_key,
                            },
                        },
                        allow_unicode=True,
                    ),
                )
            # models.json
            models = [_model_dict(m) for m in await list_models(ctx.store)]
            zf.writestr("models.json", json.dumps(models, ensure_ascii=False, indent=2))
            # keys.json (decrypted)
            keys = []
            for k in await list_keys(ctx.store):
                keys.append(
                    {
                        "model_id": k.model_id,
                        "label": k.label,
                        "key_masked": k.key_masked,
                        "enabled": k.enabled,
                        "priority": k.priority,
                    }
                )
            zf.writestr("keys.json", json.dumps(keys, ensure_ascii=False, indent=2))
        buf.seek(0)
        return web.Response(
            body=buf.read(),
            content_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=zhongzhuan-export.zip"},
        )

    def _bad(message: str) -> web.Response:
        return web.json_response({"error": {"message": message, "type": "invalid_import"}}, status=400)

    async def import_config(request):
        """Import config from uploaded zip.

        先整体解析校验（zip 结构 / 大小 / JSON / 字段），全部通过后才删后插，
        且删除+插入包在 store 事务里；任何解析错误都不会破坏现有数据。
        """
        data = await request.read()
        buf = io.BytesIO(data)
        try:
            zf = zipfile.ZipFile(buf, "r")
        except zipfile.BadZipFile:
            return _bad("上传的不是有效的 zip 文件")
        try:
            return await _parse_and_apply(zf)
        except zipfile.BadZipFile:
            # 成员元数据可能撒谎，解压时才暴露损坏（防 zip 炸弹的最后防线）。
            return _bad("zip 内文件损坏或超出可解压范围")

    async def _parse_and_apply(zf: zipfile.ZipFile) -> web.Response:
        with zf:
            infos = zf.infolist()
            if len(infos) > _MAX_IMPORT_MEMBERS:
                return web.json_response(
                    {"error": {"message": f"zip 成员数超过上限 {_MAX_IMPORT_MEMBERS}", "type": "payload_too_large"}},
                    status=413,
                )
            if sum(i.file_size for i in infos) > _MAX_IMPORT_BYTES:
                return web.json_response(
                    {"error": {"message": "解压后总大小超过 32MB 上限", "type": "payload_too_large"}},
                    status=413,
                )
            names = zf.namelist()
            skipped_keys = 0
            # ---- 整体解析校验（在任何删除之前）----
            models_data = None
            if "models.json" in names:
                try:
                    models_data = json.loads(zf.read("models.json"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    return _bad(f"models.json 解析失败: {e}")
                if not isinstance(models_data, list):
                    return _bad("models.json 必须是 JSON 数组")
                for i, md in enumerate(models_data):
                    if not isinstance(md, dict):
                        return _bad(f"models.json[{i}] 必须是对象")
                    for field in ("name", "upstream_base", "upstream_model"):
                        if not md.get(field):
                            return _bad(f"models.json[{i}] 缺少必填字段 {field}")
            keys_data = None
            if "keys.json" in names:
                try:
                    keys_data = json.loads(zf.read("keys.json"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    return _bad(f"keys.json 解析失败: {e}")
                if not isinstance(keys_data, list):
                    return _bad("keys.json 必须是 JSON 数组")
                skipped_keys = sum(1 for kd in keys_data if not (isinstance(kd, dict) and "key_value" in kd))
                for i, kd in enumerate(keys_data):
                    if not isinstance(kd, dict):
                        return _bad(f"keys.json[{i}] 必须是对象")
                    if "key_value" in kd and not kd.get("model_id"):
                        return _bad(f"keys.json[{i}] 缺少必填字段 model_id")

        from ..store.models import Model, create_model, list_models as lm, delete_model
        from ..store.keys import ApiKey, create_key, list_keys as lk, delete_key

        # ---- 校验通过：删后插，包进 store 事务 ----
        async with ctx.store.transaction():
            if models_data is not None:
                existing = await lm(ctx.store)
                for m in existing:
                    await delete_model(ctx.store, m.id)
                for md in models_data:
                    await create_model(
                        ctx.store,
                        Model(
                            name=md["name"],
                            upstream_base=md["upstream_base"],
                            upstream_model=md["upstream_model"],
                            rpm_limit=md.get("rpm_limit", 0),
                            tpm_limit=md.get("tpm_limit", 0),
                            enabled=md.get("enabled", True),
                            weight=md.get("weight", 1),
                            protocol=md.get("protocol", "openai"),
                            anthropic_version=md.get("anthropic_version", "2023-06-01"),
                            max_tokens_default=md.get("max_tokens_default", 4096),
                            upstream_path_override=md.get("upstream_path_override", ""),
                        ),
                    )
            if keys_data is not None:
                all_keys = await lk(ctx.store)
                for k in all_keys:
                    await delete_key(ctx.store, k.id)
                for kd in keys_data:
                    if "key_value" in kd:
                        await create_key(
                            ctx.store,
                            ApiKey(
                                id=None,
                                model_id=kd["model_id"],
                                label=kd.get("label", ""),
                                key_value=kd["key_value"],
                                enabled=kd.get("enabled", True),
                                priority=kd.get("priority", 0),
                            ),
                        )
        await notify_proxy_reload()
        return web.json_response({"ok": True, "skipped_keys": skipped_keys})

    app.router.add_get("/api/export", export_config)
    app.router.add_post("/api/import", import_config)


def _model_dict(m) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "upstream_base": m.upstream_base,
        "upstream_model": m.upstream_model,
        "rpm_limit": m.rpm_limit,
        "tpm_limit": m.tpm_limit,
        "enabled": m.enabled,
        "weight": m.weight,
    }
