"""Admin JWT authentication middleware."""
from __future__ import annotations

import os
import time

import jwt
from aiohttp import web

# JWT secret - auto-generated if not set
_SECRET: str = ""
_TOKEN_EXPIRY = 86400  # 24 hours


def init_jwt_secret() -> None:
    global _SECRET
    if not _SECRET:
        _SECRET = os.getenv("ZHONGZHUAN_JWT_SECRET", "")
        if not _SECRET:
            import secrets
            _SECRET = secrets.token_hex(32)


def create_token(username: str) -> str:
    """Create a JWT token for admin user."""
    now = int(time.time())
    return jwt.encode(
        {"sub": username, "iat": now, "exp": now + _TOKEN_EXPIRY},
        _SECRET,
        algorithm="HS256",
    )


def verify_token(token: str) -> str | None:
    """Verify JWT token, return username if valid."""
    try:
        payload = jwt.decode(token, _SECRET, algorithms=["HS256"])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None


def auth_enabled() -> bool:
    return os.getenv("ZHONGZHUAN_ADMIN_AUTH", "").lower() == "true"


def make_auth_middleware() -> web.middleware:
    """Create admin auth middleware (only active when ZHONGZHUAN_ADMIN_AUTH=true)."""

    _whitelist = {"/api/auth/login", "/api/auth/status"}

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        if not auth_enabled():
            return await handler(request)

        # Allow whitelist paths
        if request.path in _whitelist:
            return await handler(request)

        # Allow static/UI pages
        if request.path == "/" or not request.path.startswith("/api/"):
            return await handler(request)

        # Check JWT
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token or not verify_token(token):
            return web.json_response({"error": "unauthorized"}, status=401)

        return await handler(request)

    return middleware