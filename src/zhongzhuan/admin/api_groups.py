"""Group CRUD API."""

from __future__ import annotations

import asyncio
import time

import httpx
from aiohttp import web

from ..store.groups import (
    GroupData,
    GroupMemberData,
    create_group,
    list_groups,
    get_group,
    update_group,
    set_group_members,
    delete_group,
)
from ..store.keys import get_key_cipher
from ..store.models import get_model_by_id
from .api_keys import _build_fingerprint_headers, _build_upstream_url
from .notify import fetch_proxy_key_health, notify_proxy_reload


async def _test_group_key(ctx, key_id: int, model) -> dict:
    """对单个 key 做极简连通性 ping（分组测试专用：纯 ping，不触发 M013 探测）。

    与单 key 测试共用 URL 规范化 / 指纹头构建，保证「后台测试」与代理主流程
    打到同一个上游 URL。与 keys/{id}/test 的差异：不探测 reasoning_effort_map
    （分组测试语义是连通性，不应改写模型配置）。
    """
    try:
        plain = await get_key_cipher(ctx.store, key_id)
    except Exception:
        plain = None
    if not plain:
        return {"key_id": key_id, "ok": False, "status": 0,
                "error": "key decrypt failed", "url": "", "latency_ms": 0}

    upstream_base = (model.upstream_base or "").rstrip("/")
    upstream_model = model.upstream_model or model.name
    protocol = model.protocol or "openai"
    url = _build_upstream_url(upstream_base, model.upstream_path_override or "", protocol)

    headers = {"Content-Type": "application/json"}
    if protocol == "anthropic":
        headers["x-api-key"] = plain
        headers["anthropic-version"] = model.anthropic_version or "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + plain
    from ..proxy.header_templates import render

    for fname, fvalue in _build_fingerprint_headers(
        model.client_preset or "", model.custom_headers or ""
    ):
        if fname:
            headers[fname] = render(fvalue)

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
            messages.insert(0, {"role": "system", "content": "This conversation is powered by " + upstream_model})
        payload = {
            "model": upstream_model,
            "max_tokens": 1,
            "messages": messages,
            "stream": False,
        }

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        latency = int((time.time() - t0) * 1000)
        ok = 200 <= resp.status_code < 300
        err_msg = ""
        if not ok:
            try:
                err_obj = resp.json()
                err_msg = err_obj.get("error", {}).get("message") or err_obj.get("message") or str(resp.status_code)
            except Exception:
                err_msg = resp.text[:200] if resp.text else str(resp.status_code)
        return {
            "key_id": key_id,
            "ok": ok,
            "status": resp.status_code,
            "latency_ms": latency,
            "url": url,
            "model": upstream_model,
            "error": err_msg,
        }
    except httpx.TimeoutException:
        return {
            "key_id": key_id, "ok": False, "status": 0,
            "latency_ms": int((time.time() - t0) * 1000),
            "url": url, "model": upstream_model,
            "error": "timeout (30s)",
        }
    except Exception as e:
        return {
            "key_id": key_id, "ok": False, "status": 0,
            "latency_ms": int((time.time() - t0) * 1000),
            "url": url, "model": upstream_model,
            "error": f"{type(e).__name__}: {e}",
        }


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        groups = await list_groups(ctx.store)
        # 拉取 proxy 内存健康状态（2026-08-15 v1：分组页标红失效成员）
        try:
            health = await fetch_proxy_key_health()
            health_map = {h["key_id"]: h for h in health}
        except Exception:
            health_map = {}
        # 成员 key 的失效标记（model_id → set(失效 key_id)）
        bad_by_model: dict[int, list[int]] = {}
        for g in groups:
            for m in (g.get("members") or []):
                mid = m["model_id"]
                keys = await ctx.store.fetchall(
                    "SELECT id FROM api_keys WHERE model_id=? AND enabled=1",
                    (mid,),
                )
                bad = []
                for (kid,) in keys:
                    h = health_map.get(kid)
                    if h and h.get("status") in ("invalid", "error", "rate_limited"):
                        bad.append(kid)
                if bad:
                    bad_by_model.setdefault(mid, []).extend(bad)
        out = []
        for g in groups:
            g = dict(g)
            for m in (g.get("members") or []):
                m = dict(m)
                m["bad_keys"] = bad_by_model.get(m["model_id"], [])
            g["members"] = g.get("members") or []
            out.append(g)
        return web.json_response({"data": out})

    async def create(request):
        data = await request.json()
        g = GroupData(
            name=data["name"],
            strategy=data["strategy"],
            fallback_enabled=bool(data.get("fallback_enabled", True)),
            exposed=bool(data.get("exposed", True)),
            fallback_group=data.get("fallback_group", "") or "",
        )
        g = await create_group(ctx.store, g)
        members = data.get("members", [])
        if members:
            await set_group_members(
                ctx.store,
                g.id,
                [
                    GroupMemberData(
                        group_id=g.id,
                        model_id=m["model_id"],
                        weight=m.get("weight", 1),
                        ord=m.get("ord", i),
                    )
                    for i, m in enumerate(members)
                ],
            )
        await notify_proxy_reload()
        return web.json_response(await get_group(ctx.store, g.name), status=201)

    async def update(request):
        group_id = int(request.match_info["id"])
        data = await request.json()
        g = GroupData(
            name=data["name"],
            strategy=data["strategy"],
            fallback_enabled=bool(data.get("fallback_enabled", True)),
            exposed=bool(data.get("exposed", True)),
            fallback_group=data.get("fallback_group", "") or "",
        )
        await update_group(ctx.store, group_id, g)
        # Only touch members when the field is explicitly provided (list, possibly empty).
        # None = leave members untouched; [] = clear all members.
        members = data.get("members")
        if members is not None:
            await set_group_members(
                ctx.store,
                group_id,
                [
                    GroupMemberData(
                        group_id=group_id,
                        model_id=m["model_id"],
                        weight=m.get("weight", 1),
                        ord=m.get("ord", i),
                    )
                    for i, m in enumerate(members)
                ],
            )
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def delete(request):
        group_id = int(request.match_info["id"])
        await delete_group(ctx.store, group_id)
        await notify_proxy_reload()
        return web.json_response({"ok": True})

    async def test_group(request):
        """测试分组内所有模型的连通性：对每个成员的每个启用 key 做极简 ping。

        返回按模型聚合的结果矩阵（含每个 key 的 HTTP 状态 / 延迟 / 错误）。
        纯 ping（不触发 M013 探测、不改写模型配置），与单 key 测试共用 URL
        规范化和指纹头，保证与代理主流程打到同一个上游 URL。
        """
        group_id = int(request.match_info["id"])
        rows = await ctx.store.fetchall(
            "SELECT id, name, strategy, fallback_enabled, exposed, fallback_group FROM model_groups WHERE id=?",
            (group_id,),
        )
        if not rows:
            return web.json_response({"ok": False, "error": "group not found"}, status=404)
        g = rows[0]
        members = await ctx.store.fetchall(
            "SELECT model_id, weight, ord FROM group_models WHERE group_id=? ORDER BY ord",
            (group_id,),
        )
        if not members:
            return web.json_response(
                {"ok": True, "group": g[1], "models": [], "summary": {"total_keys": 0, "ok": 0, "fail": 0}}
            )

        # 取成员模型信息 + 各自的启用 key
        meta = []  # (model_id, model_obj, [key_id...], ord)
        tasks = []  # (key_id, coro)
        for model_id, _weight, ord_ in members:
            model = await get_model_by_id(ctx.store, model_id)
            if not model:
                continue
            keys = await ctx.store.fetchall(
                "SELECT id FROM api_keys WHERE model_id=? AND enabled=1 ORDER BY id",
                (model_id,),
            )
            key_ids = [k[0] for k in keys]
            meta.append((model_id, model, key_ids, ord_))
            for kid in key_ids:
                tasks.append((kid, _test_group_key(ctx, kid, model)))

        # 并发执行（限制并发避免打爆上游），失败隔离不波及同组其他 key
        key_results: dict[int, dict] = {}
        if tasks:
            outcomes = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
            for (kid, _coro), out in zip(tasks, outcomes):
                if isinstance(out, Exception):
                    key_results[kid] = {"key_id": kid, "ok": False, "status": 0,
                                        "error": f"{type(out).__name__}: {out}", "url": ""}
                elif out:
                    key_results[kid] = out

        models_out = []
        total_ok = total_fail = total_keys = 0
        for model_id, model, key_ids, ord_ in meta:
            per_key = [key_results.get(kid) or {
                "key_id": kid, "ok": False, "status": 0, "error": "no result", "url": ""
            } for kid in key_ids]
            ok_n = sum(1 for k in per_key if k.get("ok"))
            models_out.append({
                "model_id": model_id,
                "name": model.name or "",
                "ord": ord_,
                "keys": per_key,
                "ok_count": ok_n,
                "total": len(per_key),
            })
            total_keys += len(per_key)
            total_ok += ok_n
            total_fail += len(per_key) - ok_n

        return web.json_response({
            "ok": True,
            "group": g[1],
            "models": models_out,
            "summary": {"total_keys": total_keys, "ok": total_ok, "fail": total_fail},
        })

    app.router.add_get("/api/groups", list_)
    app.router.add_post("/api/groups", create)
    app.router.add_put("/api/groups/{id}", update)
    app.router.add_delete("/api/groups/{id}", delete)
    app.router.add_post("/api/groups/{id}/test", test_group)
