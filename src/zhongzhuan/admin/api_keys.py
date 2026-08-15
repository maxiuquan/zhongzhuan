"""Key CRUD API."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

import httpx
from aiohttp import web

from ..crypto import mask
from ..store.keys import ApiKey, create_key, list_keys, delete_key, update_key, get_key_cipher
from ..store.models import get_model_by_id
from .notify import notify_proxy_reload


# 标准思考等级(与代理翻译层对齐, M013)
# 注意: ultra 是「规范档」——客户端发来的 xhigh 会被代理规范成 ultra。
# 探针必须用「规范值」(ultra) 去探测上游, 绝不能用客户端别名 xhigh:
# 多数中转上游不认字面 xhigh, 若探针用 xhigh 探测并误记成原生片段, 运行时就会
# 向上游注入 {reasoning_effort: "xhigh"} → 上游拒收 → 403/400 → 故障转移溢出到
# 备用成员(同样注入 xhigh → 也 403)。仅对真正接受 xhigh 的上游(OpenAI 直连类)
# 在 ultra 档额外兜底探测 xhigh, 且排在规范值 ultra 之后。
_PROBE_LEVELS = ("low", "medium", "high", "ultra")
_NORM_STR = {"low": "low", "medium": "medium", "high": "high", "ultra": "ultra"}
_NORM_BUDGET = {"low": 1024, "medium": 2048, "high": 4096, "ultra": 8192}


def _chat_reasoning_candidates(level: str) -> list[tuple[str, dict]]:
    """Candidate reasoning-field shapes to blind-probe against a chat upstream."""
    s = _NORM_STR[level]
    b = _NORM_BUDGET[level]
    cands = [
        ("reasoning_effort", {"reasoning_effort": s}),
        ("reasoning_effort_obj", {"reasoning": {"effort": s}}),
        ("thinking_budget", {"thinking": {"budget_tokens": b}}),
        ("thinking_type", {"thinking": {"type": "enabled", "budget_tokens": b}}),
        ("enable_thinking", {"enable_thinking": True, "thinking_budget": b}),
    ]
    # 顶档额外兜底探测 xhigh(仅 OpenAI 直连等少数上游接受该字面量)。
    # 放最前: 若上游接受 xhigh 则记 xhigh; 否则回落到规范值 ultra。
    if level == "ultra":
        cands = [
            ("reasoning_effort_xhigh", {"reasoning_effort": "xhigh"}),
            ("reasoning_effort_obj_xhigh", {"reasoning": {"effort": "xhigh"}}),
            *cands,
        ]
    return cands


def _probe_body_ok(resp) -> bool:
    """A 2xx only counts as a real success if the body isn't an error page.

    Some upstreams answer a rejected ``reasoning_effort`` with HTTP 200 but an
    embedded ``{"error": ...}`` body (or a Cloudflare HTML challenge). Treating
    those as "accepted" would poison the recorded map with a non-working shape.
    """
    ct = (resp.headers.get("content-type") or "").lower()
    if "text/html" in ct:
        return False
    try:
        data = resp.json()
    except Exception:
        # Non-JSON 2xx (unlikely for chat) — trust the status code.
        return True
    if isinstance(data, dict) and "error" in data:
        return False
    return True



def _anthropic_reasoning_candidates(level: str) -> list[tuple[str, dict]]:
    b = _NORM_BUDGET[level]
    return [("thinking", {"thinking": {"type": "enabled", "budget_tokens": b}})]


async def _probe_reasoning_levels(client, url, headers, protocol, base_factory):
    """Probe which standard reasoning levels this upstream actually accepts.

    For each level, send minimal requests (one per candidate shape) and record
    the first shape that returns 2xx.  Returns ``{level: fragment_or_None}``.
    ``None`` means every candidate was rejected (strip the param for that level).
    """
    shapes_for = _anthropic_reasoning_candidates if protocol == "anthropic" else _chat_reasoning_candidates

    async def probe_one(level):
        for _name, frag in shapes_for(level):
            payload = base_factory()
            payload.update(frag)
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except Exception:
                continue
            if 200 <= resp.status_code < 300 and _probe_body_ok(resp):
                return frag
        return None

    results = await asyncio.gather(*(probe_one(lvl) for lvl in _PROBE_LEVELS))
    return {lvl: frag for lvl, frag in zip(_PROBE_LEVELS, results)}


def _already_probed(model) -> bool:
    """True if this model's reasoning-level mapping has already been probed and
    recorded.  Once recorded we never re-probe on subsequent connectivity tests
    (M013 auto-detect, silent) — a channel+model is probed only on its first
    successful ``test`` request, then the result is reused forever."""
    raw = getattr(model, "reasoning_effort_map", "") or ""
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except Exception:
        return False
    return isinstance(parsed, dict) and len(parsed) > 0


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
        path = override.lstrip("/")
    elif protocol == "anthropic":
        path = "v1/messages"
    else:
        path = "v1/chat/completions"
    # 与 UpstreamClient._resolve_url 同款去重：base 尾段（如 /v1）与 path 首段相同
    # 时剥掉 path 首段，避免 /v1/v1/ 双重前缀（2026-08-15 实测 p0/deepseek 的
    # base 含 /api/agents/v1 多段前缀，旧逻辑两处都拼错 URL）。
    base_path = urlparse(base).path.rstrip("/")
    base_last = base_path.rsplit("/", 1)[-1] if base_path else ""
    if base_last and path.startswith(base_last + "/"):
        path = path[len(base_last):].lstrip("/")
    return base + "/" + path if path else base


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

        def _make_base():
            if protocol == "anthropic":
                return {"model": upstream_model, "max_tokens": 16384,
                        "messages": [{"role": "user", "content": "hi"}]}
            messages = [{"role": "user", "content": "hi"}]
            if has_fingerprint:
                messages.insert(0, {"role": "system",
                                    "content": "This conversation is powered by " + upstream_model})
            return {"model": upstream_model, "max_tokens": 1, "messages": messages, "stream": False}

        # 思考等级探针：仅「首次」连通性测试时探测并落库到 model 行，
        # 之后(已记录 reasoning_effort_map)不再重复探测(M013 自动探测, 静默执行)。
        # 带 ?reprobe=1 时无论是否已探测都强制重新探测(后台「重新探测」按钮用)。
        reprobe = request.query.get("reprobe", "0").lower() in ("1", "true", "yes")
        already_probed = _already_probed(model) and not reprobe
        t0 = time.time()
        try:
            if already_probed:
                # 已探测过：只做基础连通性 ping，不重复探测思考等级。
                async with httpx.AsyncClient(timeout=15.0) as client:
                    ping_resp = await client.post(url, headers=headers, json=payload)
                probe_map = None
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    # 基础连通性 ping 与思考等级探针同轮并发
                    ping_task = client.post(url, headers=headers, json=payload)
                    probe_task = _probe_reasoning_levels(client, url, headers, protocol, _make_base)
                    ping_resp, probe_map = await asyncio.gather(ping_task, probe_task)
            latency = int((time.time() - t0) * 1000)
            ok = 200 <= ping_resp.status_code < 300
            # 首次探测结果静默写回 model 行(思考等级映射, M013), 不向前端展示结论。
            if probe_map is not None:
                try:
                    await ctx.store.execute(
                        "UPDATE models SET reasoning_effort_map=? WHERE id=?",
                        (json.dumps(probe_map, ensure_ascii=False), model.id),
                    )
                    try:
                        await notify_proxy_reload()
                    except Exception:
                        pass
                except Exception:
                    pass
            err_msg = ""
            if not ok:
                try:
                    err_obj = ping_resp.json()
                    err_msg = err_obj.get("error", {}).get("message") or err_obj.get("message") or str(ping_resp.status_code)
                except Exception:
                    err_msg = ping_resp.text[:200] if ping_resp.text else str(ping_resp.status_code)
            return web.json_response(
                {
                    "ok": ok,
                    "status": ping_resp.status_code,
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
