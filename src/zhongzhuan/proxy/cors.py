"""CORS 中间件：允许浏览器端跨域调用 API。

处理：
1. OPTIONS 预检请求 → 返回 204 + CORS 头
2. 所有响应添加 Access-Control-Allow-* 头
"""
from __future__ import annotations

from aiohttp import web


def make_cors_middleware(allow_origin: str = "*") -> web.middleware:
    """创建 CORS 中间件。

    Args:
        allow_origin: 允许的来源，默认 "*"（任何来源）。
    """

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        # OPTIONS 预检请求直接返回 204
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            resp = await handler(request)

        # 给所有响应添加 CORS 头
        resp.headers["Access-Control-Allow-Origin"] = allow_origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        resp.headers["Access-Control-Allow-Headers"] = (
            "Authorization, Content-Type, x-api-key, anthropic-version, "
            "x-session-id, x-zhongzhuan-session, x-request-id"
        )
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    return middleware
