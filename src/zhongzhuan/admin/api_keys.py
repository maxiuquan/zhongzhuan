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

        # 极简请求体（OpenAI 格式）
        if protocol == "anthropic":
            payload = {
                "model": upstream_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
        else:
            payload = {
                "model": upstream_model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
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
