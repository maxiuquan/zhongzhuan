"""CORS 中间件：允许浏览器端跨域调用 API（R-P2-01）。

R-P2-01 将默认 ``*`` 改为可配置 allowlist：

- allowlist 来源（优先级从高到低）：
  1. 构造时显式传入的 ``allow_origins``
  2. ``ZHONGZHUAN_CORS_ALLOW_ORIGINS`` 环境变量（逗号分隔）
  3. 空列表
- 行为：
  - 请求带 ``Origin`` 且命中 allowlist（或 allowlist 含 ``*``）→ 回显该 Origin
  - 请求带 ``Origin`` 但不在 allowlist → **不返回** ``Access-Control-Allow-Origin``，
    浏览器拒绝跨域（生产模式空 allowlist 即拒绝一切跨域）
  - 无 ``Origin`` 头（非浏览器）→ 不加 CORS 头
- 预检 OPTIONS：命中才回 204 + CORS 头，未命中返回 403。

判据③：allowlist 内/外来源各 1 例。
"""

from __future__ import annotations

import os

from aiohttp import web

_ALLOW_METHODS = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
_ALLOW_HEADERS = (
    "Authorization, Content-Type, x-api-key, anthropic-version, "
    "x-session-id, x-zhongzhuan-session, x-request-id, X-CSRF-Token"
)
_MAX_AGE = "86400"


def _origin_in_allowlist(origin: str, allow_origins: list[str]) -> bool:
    """Allowlist 匹配：精确字符串匹配，``*`` 代表任意来源。"""
    for allowed in allow_origins:
        if allowed == "*":
            return True
        if allowed == origin:
            return True
    return False


def _resolve_allow_origins(allow_origins: list[str] | None) -> list[str]:
    if allow_origins is not None:
        return list(allow_origins)
    raw = os.getenv("ZHONGZHUAN_CORS_ALLOW_ORIGINS", "")
    if raw.strip():
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def _echo_origin(origin: str, origins: list[str]) -> str:
    """Allowlist 含 ``*`` 时回 ``*``，否则回显具体 Origin。"""
    if "*" in origins:
        return "*"
    return origin


def make_cors_middleware(allow_origins: list[str] | None = None) -> web.middleware:
    """创建 CORS 中间件。

    Args:
        allow_origins: 允许的来源列表。``None`` 时读取
            ``ZHONGZHUAN_CORS_ALLOW_ORIGINS``；仍为空则不允许任何跨域来源。
    """
    origins = _resolve_allow_origins(allow_origins)

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
        allowed = bool(origin) and _origin_in_allowlist(origin, origins)
        echo = _echo_origin(origin, origins)

        # OPTIONS 预检：命中 allowlist 才放行。
        if request.method == "OPTIONS":
            if not allowed:
                return web.Response(status=403, text="cors origin not allowed")
            resp = web.Response(status=204)
            _add_cors_headers(resp, echo)
            return resp

        resp = await handler(request)

        if allowed:
            _add_cors_headers(resp, echo)
        return resp

    return middleware


def _add_cors_headers(resp: web.StreamResponse, origin: str) -> None:
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = _ALLOW_METHODS
    resp.headers["Access-Control-Allow-Headers"] = _ALLOW_HEADERS
    resp.headers["Access-Control-Max-Age"] = _MAX_AGE
    # 允许携带凭据（cookie/CSRF token）时不允许 ``*``，这里只在明确 Origin 时开启。
    if origin and origin != "*":
        resp.headers["Access-Control-Allow-Credentials"] = "true"
