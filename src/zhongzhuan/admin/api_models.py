"""Model CRUD API."""

from __future__ import annotations

import json

from aiohttp import web

from ..proxy.client_presets import (
    is_valid_preset_name,
    list_presets,
    validate_custom_header_name,
)
from ..store.models import (
    Model,
    create_model,
    list_models,
    update_model,
    delete_model,
)
from .notify import notify_proxy_reload


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        ms = await list_models(ctx.store)
        return web.json_response({"data": [_to_dict(m) for m in ms]})

    async def create(request):
        data = await request.json()
        err = _validate_payload(data)
        if err:
            return web.json_response({"error": {"message": err}}, status=400)
        m = _payload_to_model(data)
        m = await create_model(ctx.store, m)
        await notify_proxy_reload()
        return web.json_response(_to_dict(m), status=201)

    async def update(request):
        model_id = int(request.match_info["id"])
        data = await request.json()
        err = _validate_payload(data)
        if err:
            return web.json_response({"error": {"message": err}}, status=400)
        m = _payload_to_model(data)
        await update_model(ctx.store, model_id, m)
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def delete(request):
        model_id = int(request.match_info["id"])
        await delete_model(ctx.store, model_id)
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def preset_options(request):
        """返回客户端模拟下拉选项。
        前端在头部加"不模拟"、尾部加"自定义"；本接口只返回内置预设（中间项）。
        """
        return web.json_response({"presets": list_presets()})

    app.router.add_get("/api/models", list_)
    app.router.add_post("/api/models", create)
    app.router.add_put("/api/models/{id}", update)
    app.router.add_delete("/api/models/{id}", delete)
    app.router.add_get("/api/models/client-preset-options", preset_options)


def _payload_to_model(data: dict) -> Model:
    """从请求体构造 Model（含 v009 client_preset / custom_headers）。"""
    return Model(
        name=data["name"],
        upstream_base=data["upstream_base"],
        upstream_model=data["upstream_model"],
        rpm_limit=int(data.get("rpm_limit", 0)),
        tpm_limit=int(data.get("tpm_limit", 0)),
        enabled=bool(data.get("enabled", True)),
        weight=int(data.get("weight", 1)),
        protocol=data.get("protocol", "openai"),
        anthropic_version=data.get("anthropic_version", "2023-06-01"),
        max_tokens_default=int(data.get("max_tokens_default", 4096)),
        upstream_path_override=data.get("upstream_path_override", ""),
        is_fallback=bool(data.get("is_fallback", False)),
        aliases=data.get("aliases", ""),
        capabilities=data.get("capabilities", ""),
        upstream_mode=data.get("upstream_mode", "bonded"),
        client_preset=data.get("client_preset", ""),
        custom_headers=data.get("custom_headers", ""),
        exposed=bool(data.get("exposed", True)),
    )


def _validate_payload(data: dict) -> str | None:
    """校验模型创建/更新负载。返回错误消息，合法返回 None。"""
    if not data.get("name") or not str(data["name"]).strip():
        return "名称不能为空"
    if not data.get("upstream_base") or not str(data["upstream_base"]).strip():
        return "上游地址不能为空"

    # client_preset 白名单校验
    preset = data.get("client_preset", "")
    if not is_valid_preset_name(preset):
        return f"无效的客户端模拟值: {preset}"

    # custom 模式下校验 custom_headers JSON 与受控头
    if preset == "custom":
        raw = data.get("custom_headers", "") or ""
        if raw:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return "custom_headers 不是合法 JSON"
            if not isinstance(parsed, list):
                return "custom_headers 必须是 JSON 数组"
            for i, item in enumerate(parsed):
                if not isinstance(item, dict):
                    return f"custom_headers[{i}] 必须是对象"
                name = str(item.get("name", "")).strip()
                err = validate_custom_header_name(name)
                if err:
                    return f"custom_headers[{i}]: {err}"
                # value 必须是字符串（允许空字符串）
                if "value" in item and not isinstance(item["value"], str):
                    return f"custom_headers[{i}].value 必须是字符串"
    return None


def _to_dict(m: Model) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "upstream_base": m.upstream_base,
        "upstream_model": m.upstream_model,
        "rpm_limit": m.rpm_limit,
        "tpm_limit": m.tpm_limit,
        "enabled": m.enabled,
        "weight": m.weight,
        "protocol": m.protocol,
        "anthropic_version": m.anthropic_version,
        "max_tokens_default": m.max_tokens_default,
        "upstream_path_override": m.upstream_path_override,
        "is_fallback": m.is_fallback,
        "aliases": m.aliases,
        "capabilities": m.capabilities,
        "upstream_mode": m.upstream_mode,
        "client_preset": m.client_preset,
        "custom_headers": m.custom_headers,
        "exposed": m.exposed,
        "created_at": m.created_at,
        "updated_at": m.updated_at,
    }
