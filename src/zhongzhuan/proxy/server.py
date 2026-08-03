"""Proxy HTTP server."""
from __future__ import annotations

from aiohttp import web

from .auth import make_proxy_auth_middleware
from .cors import make_cors_middleware
from .handler import make_handler
from ..store import Store
from ..upstream import UpstreamClient


class ProxyServer:
    def __init__(
        self,
        upstream_clients: dict[str, UpstreamClient],
        api_key: str = "",
        keys: list | None = None,
        proxy_timeout: float = 30.0,
        models: list[dict] | None = None,
        groups: list[dict] | None = None,
        store: Store | None = None,
        load_keys_fn=None,
        sticky_ttl: float = 1800.0,
    ) -> None:
        self.upstream_clients = upstream_clients
        self.api_key = api_key
        self.keys = keys or []
        self.proxy_timeout = proxy_timeout
        self.models = models or []
        self.groups = groups or []
        self.store = store
        self.load_keys_fn = load_keys_fn
        self.sticky_ttl = sticky_ttl

    def app(self) -> web.Application:
        # CORS 中间件在最外层，Gzip 在内层（对非流式 JSON 响应压缩）
        app = web.Application(
            client_max_size=64 * 1024 * 1024,
            middlewares=[
                make_cors_middleware(),  # CORS 必须在最外层
                _make_gzip_middleware(min_size=1024),  # >1KB 才压缩
            ],
        )

        # Proxy access token auth middleware (VPS mode)
        if self.store is not None:
            app.middlewares.append(make_proxy_auth_middleware(self.store))

        # Build keys list
        the_keys = list(self.keys)
        if not the_keys and self.api_key:
            from .ratelimit import KeyHealth, SlidingWindow
            fallback_base = next(iter(self.upstream_clients)) if self.upstream_clients else ""
            the_keys = [KeyHealth(
                key_id=0, api_key=self.api_key,
                window=SlidingWindow(60, 1000),
                upstream_base=fallback_base,
            )]

        handler = make_handler(
            upstream_clients=self.upstream_clients, keys=the_keys,
            proxy_timeout=self.proxy_timeout, store=self.store,
            load_keys_fn=self.load_keys_fn, groups=self.groups,
            sticky_ttl=self.sticky_ttl,
        )
        # 注册后台任务钩子（优化点4+5：sticky 清理 + 健康状态快照）
        async def _on_startup(app: web.Application) -> None:
            await handler.start_background_tasks()
        async def _on_cleanup(app: web.Application) -> None:
            await handler.stop_background_tasks()
        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)

        app.router.add_route("*", "/v1/{tail:.*}", handler)
        app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
        app.router.add_get("/version", self._version)
        app.router.add_get("/v1/models", self._list_models)
        app.router.add_post("/api/reload", lambda r: self._reload(r, handler))
        return app

    async def _reload(self, _request: web.Request, handler) -> web.Response:
        n = await handler.reload_keys()
        from loguru import logger
        logger.info(f"reloaded {n} keys from store")
        return web.json_response({"ok": True, "keys": n})

    async def _version(self, _request: web.Request) -> web.Response:
        from zhongzhuan import __version__
        return web.json_response({"name": "zhongzhuan", "version": __version__})

    async def _list_models(self, _request: web.Request) -> web.Response:
        items: list[dict] = []
        for m in self.models:
            items.append({"id": m.get("name", ""), "object": "model"})
        for g in self.groups:
            items.append({"id": g.get("name", ""), "object": "model"})
        return web.json_response({"object": "list", "data": items})


def _make_gzip_middleware(min_size: int = 1024):
    """Gzip 响应压缩中间件。

    仅对满足以下条件的响应压缩：
    - 响应体 >= min_size 字节
    - Content-Type 为 application/json 或 text/*
    - 客户端发送了 Accept-Encoding: gzip
    - 响应未设置 Content-Encoding（避免重复压缩）
    - 非 StreamResponse（流式响应不压缩）
    """
    import gzip

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        resp = await handler(request)

        # 流式响应不压缩
        if isinstance(resp, web.StreamResponse) and not isinstance(resp, web.Response):
            return resp

        # 只压缩 web.Response
        if not isinstance(resp, web.Response):
            return resp

        # 检查客户端是否接受 gzip
        accept_encoding = request.headers.get("Accept-Encoding", "").lower()
        if "gzip" not in accept_encoding:
            return resp

        # 已有 Content-Encoding 则跳过
        if resp.headers.get("Content-Encoding"):
            return resp

        body = resp.body
        if body is None or len(body) < min_size:
            return resp

        # 只压缩 JSON / text 类响应
        content_type = resp.headers.get("Content-Type", "").lower()
        if not (content_type.startswith("application/json") or content_type.startswith("text/")):
            return resp

        # 压缩
        compressed = gzip.compress(body)
        if len(compressed) >= len(body):
            return resp  # 压缩后更大则不压缩

        resp.body = compressed
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(compressed))
        resp.headers["Vary"] = "Accept-Encoding"
        return resp

    return middleware