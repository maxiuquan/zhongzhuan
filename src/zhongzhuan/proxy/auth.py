"""Proxy access token authentication middleware."""
from __future__ import annotations

import os

from aiohttp import web

from ..store.access_tokens import verify_token as db_verify_token


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

        # Check Bearer token
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token or not await db_verify_token(store, token):
            return web.json_response(
                {"error": {"message": "invalid or missing access token", "type": "unauthorized"}},
                status=401,
            )

        return await handler(request)

    return middleware