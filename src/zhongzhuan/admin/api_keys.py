"""Key CRUD API."""

from __future__ import annotations

import time
from urllib.parse import urlparse

import httpx
from aiohttp import web

from ..crypto import mask
from ..store.keys import ApiKey, create_key, list_keys, delete_key, update_key, get_key_cipher
from ..store.models import get_model_by_id
from .notify import notify_proxy_reload


def _build_upstream_url(
    upstream_base: str,
    path_override: str,
    protocol: str,
) -> str:
    """Build the full upstream URL exactly like the proxy hot path.

    The proxy sends requests through :class:`UpstreamClient`, which strips a
    base path prefix (e.g. ``/v1``) before handing the path to httpx so that
    ``base_url`` merging never duplicates it.  The admin "test key" endpoint
    must produce the same URL or it will wrongly report healthy keys as
    failing (e.g. ``https://host/v1`` + ``/v1/chat/completions`` ->
    ``/v1/v1/...``).

    ``path_override`` may be a full URL, an absolute path (``/zen/v1/...``) or
    empty.
    """
    base = (upstream_base or "").rstrip("/")
    override = (path_override or "").strip()
    if override.startswith("http://") or override.startswith("https://"):
        return override
    if override:
        path = override if override.startswith("/") else "/" + override
        # Same dedup as UpstreamClient.request: strip the base path prefix.
        base_path = urlparse(base).path.rstrip("/")
        if base_path and path.startswith(base_path):
            path = path[len(base_path) :] or "/"
        return base + path
    if protocol == "anthropic":
        path = "/v1/messages"
    else:
        path = "/v1/chat/completions"
    base_path = urlparse(base).path.rstrip("/")
    if base_path and path.startswith(base_path):
        path = path[len(base_path) :] or "/"
    return base + path


def _build_fingerprint_headers(client_preset: str, custom_headers: str) -> list[tuple[str, str]]:
    """构造与代理主流程一致的客户端指纹头列表。

    与 ``handler._apply_client_fingerprint`` 相同的规则：

    * ``client_preset == ""``    → 不模拟, 返回空列表（零影响）
    * ``client_preset == "custom"`` → 解析 ``custom_headers`` JSON（容错为空）
    * 其他预设 key           → 从 ``client_presets.PRESETS`` 取内置头

    返回的 ``(name, value)`` 中的 value 是模板字符串（如 ``{{uuid}}``），由调用方
    通过 :func:`zhongzhuan.proxy.header_templates.render` 渲染后再注入。
    """
    if not client_preset:
        return []
    if client_preset == "custom":
        from ..proxy.client_presets import parse_custom_headers

        return parse_custom_headers(custom_headers)
    from ..proxy.client_presets import get_headers

    return get_headers(client_preset)


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        model_id = request.query.get("model_id")
        rows = await list_keys(ctx.store, int(model_id) if model_id else None)
        return web.json_response(
            {
                "data": [
                    {
                        "id": r.id,
                        "model_id": r.model_id,
                        "label": r.label,
                        "key_masked": r.key_masked,
                        "enabled": r.enabled,
                        "priority": r.priority,
                        "created_at": r.created_at,
                    }
                    for r in rows
                ]
            }
        )

    async def create(request):
        data = await request.json()
        k = ApiKey(
            id=None,
            model_id=int(data["model_id"]),
            label=data.get("label", ""),
            key_value=data["key_value"],
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 0)),
        )
        k = await create_key(ctx.store, k)
        await notify_proxy_reload()
        return web.json_response(
            {
                "id": k.id,
                "model_id": k.model_id,
                "label": k.label,
                "key_masked": mask(k.key_value),
                "enabled": k.enabled,
                "priority": k.priority,
                "created_at": k.created_at,
            },
            status=201,
        )

    async def delete(request):
        key_id = int(request.match_info["id"])
        await delete_key(ctx.store, key_id)
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def update(request):
        key_id = int(request.match_info["id"])
        data = await request.json()
        await update_key(
            ctx.store,
            key_id,
            label=data.get("label"),
            enabled=data.get("enabled"),
            priority=data.get("priority"),
        )
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def test(request):
        """测试单个 Key 的连通性：向其上游发一个 max_tokens=1 的极简 chat 请求。"""
        key_id = int(request.match_info["id"])
        plain = await get_key_cipher(ctx.store, key_id)
        if not plain:
            return web.json_response({"ok": False, "error": "key not found or decrypt failed"}, status=404)

        row = await ctx.store.fetchone("SELECT model_id FROM api_keys WHERE id=?", (key_id,))
        if not row:
            return web.json_response({"ok": False, "error": "key not found"}, status=404)
        model = await get_model_by_id(ctx.store, row[0])
        if not model:
            return web.json_response({"ok": False, "error": "model not found"}, status=404)

        # 构造测试请求
        upstream_base = (model.upstream_base or "").rstrip("/")
        upstream_model = model.upstream_model or model.name
        protocol = model.protocol or "openai"

        # 决定请求 URL（与代理主流程 UpstreamClient 相同的路径规范化，
        # 避免 base 已含 /v1 时重复成 /v1/v1/...）
        url = _build_upstream_url(
            upstream_base,
            model.upstream_path_override or "",
            protocol,
        )

        # 请求头
        headers = {"Content-Type": "application/json"}
        if protocol == "anthropic":
            headers["x-api-key"] = plain
            headers["anthropic-version"] = model.anthropic_version or "2023-06-01"
        else:
            headers["Authorization"] = "Bearer " + plain

        # 客户端指纹模拟：测试请求与代理主流程（_apply_client_fingerprint）保持一致——
        # 模型勾选了"模拟某客户端"，测试连接时也注入相同的指纹头，否则某些上游
        # 对陌生 UA 的测试请求会拒绝/限流，导致误报失败。
        fingerprint_headers = _build_fingerprint_headers(
            model.client_preset or "",
            model.custom_headers or "",
        )
        from ..proxy.header_templates import render

        for fname, fvalue in fingerprint_headers:
            if fname:
                headers[fname] = render(fvalue)

        # 极简请求体（OpenAI 格式）。
        # 注意：预设标记 require_system 时（如 workbuddy），请求体需携带一条
        # system 消息。部分上游（如 freemodel.dev）通过请求体中的 system 消息
        # 识别客户端来源（WorkBuddy 请求必带系统提示词），缺失会返回 403
        # unsupported_client。
        from ..proxy.client_presets import needs_system_message

        has_fingerprint = needs_system_message(model.client_preset or "")
        if protocol == "anthropic":
            payload = {
                "model": upstream_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
        else:
            messages = [{"role": "user", "content": "hi"}]
            if has_fingerprint:
                messages.insert(
                    0,
                    {"role": "system", "content": "This conversation is powered by " + upstream_model},
                )
            payload = {
                "model": upstream_model,
                "max_tokens": 1,
                "messages": messages,
                "stream": False,
            }

        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
            latency = int((time.time() - t0) * 1000)
            ok = 200 <= resp.status_code < 300
            # 尝试解析错误信息
            err_msg = ""
            if not ok:
                try:
                    err_obj = resp.json()
                    err_msg = err_obj.get("error", {}).get("message") or err_obj.get("message") or str(resp.status_code)
                except Exception:
                    err_msg = resp.text[:200] if resp.text else str(resp.status_code)
            return web.json_response(
                {
                    "ok": ok,
                    "status": resp.status_code,
                    "latency_ms": latency,
                    "url": url,
                    "model": upstream_model,
                    "error": err_msg,
                }
            )
        except httpx.TimeoutException:
            latency = int((time.time() - t0) * 1000)
            return web.json_response(
                {
                    "ok": False,
                    "status": 0,
                    "latency_ms": latency,
                    "url": url,
                    "model": upstream_model,
                    "error": "timeout (30s)",
                }
            )
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return web.json_response(
                {
                    "ok": False,
                    "status": 0,
                    "latency_ms": latency,
                    "url": url,
                    "model": upstream_model,
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    app.router.add_get("/api/keys", list_)
    app.router.add_post("/api/keys", create)
    app.router.add_put("/api/keys/{id}", update)
    app.router.add_delete("/api/keys/{id}", delete)
    app.router.add_post("/api/keys/{id}/test", test)
