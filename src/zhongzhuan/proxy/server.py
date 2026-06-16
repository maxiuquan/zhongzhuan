"""Proxy HTTP server."""
from __future__ import annotations

from aiohttp import web

from .auth import make_proxy_auth_middleware
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
    ) -> None:
        self.upstream_clients = upstream_clients
        self.api_key = api_key
        self.keys = keys or []
        self.proxy_timeout = proxy_timeout
        self.models = models or []
        self.groups = groups or []
        self.store = store
        self.load_keys_fn = load_keys_fn

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)

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
            load_keys_fn=self.load_keys_fn,
        )
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