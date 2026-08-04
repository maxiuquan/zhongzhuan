"""Proxy access token authentication middleware — 支持配额校验。

校验流程：
1. 验证 token 存在且 enabled
2. 检查 expires_at 是否过期
3. 检查 model_whitelist 是否允许请求的模型
4. 检查 used_tokens < quota_tokens

通过校验后，将 token_id 注入 request["token_id"] 供 handler 后续扣减配额。
"""

from __future__ import annotations

import json
import os

from aiohttp import web

from ..store.access_tokens import get_token_by_value


def proxy_auth_enabled() -> bool:
    """Check if proxy access token authentication is enabled."""
    return os.getenv("ZHONGZHUAN_PROXY_AUTH", "").lower() == "true"


def make_proxy_auth_middleware(store) -> web.middleware:
    """Create middleware that validates access tokens for /v1/* endpoints."""

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        if not proxy_auth_enabled():
            return await handler(request)

        # Only protect /v1/* endpoints
        if not request.path.startswith("/v1/"):
            return await handler(request)

        # Allow /v1/models without auth (used for model discovery)
        if request.path == "/v1/models" and request.method == "GET":
            return await handler(request)

        # Check token: prefer x-api-key (Anthropic clients), fallback Authorization: Bearer (OpenAI clients)
        token = request.headers.get("x-api-key", "").strip()
        if not token:
            auth = request.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip()
        if not token:
            return web.json_response(
                {"error": {"message": "invalid or missing access token", "type": "unauthorized"}},
                status=401,
            )

        # 查询完整 token 对象（含配额字段）
        at = await get_token_by_value(store, token)
        if at is None:
            return web.json_response(
                {"error": {"message": "invalid or missing access token", "type": "unauthorized"}},
                status=401,
            )

        # 从 body 提取请求的模型名（用于白名单校验）
        requested_model = ""
        try:
            body = await request.read()
            if body:
                obj = json.loads(body)
                requested_model = (obj.get("model") or "").strip()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            pass

        # 配额校验
        ok, reason = at.check_quota(requested_model)
        if not ok:
            status = 403 if "disabled" in reason or "expired" in reason or "whitelist" in reason else 429
            return web.json_response(
                {"error": {"message": reason, "type": "quota_exceeded" if status == 429 else "forbidden"}},
                status=status,
                headers={"X-Zhongzhuan-Reason": reason.replace(" ", "_")},
            )

        # 注入 token_id 供 handler 扣减配额
        request["token_id"] = at.id or 0
        request["token_quota_tokens"] = at.quota_tokens
        return await handler(request)

    return middleware
