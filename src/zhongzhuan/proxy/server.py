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
        #: 供 readiness 检查 handler 的 background worker 生命周期（T33）。
        self._proxy_handler = handler
        # 注册后台任务钩子（优化点4+5：sticky 清理 + 健康状态快照）
        async def _on_startup(app: web.Application) -> None:
            await handler.start_background_tasks()
        async def _on_cleanup(app: web.Application) -> None:
            await handler.stop_background_tasks()
        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)

        app.router.add_route("*", "/v1/{tail:.*}", handler)
        # T33 (R-P2-07/08/09)：分层健康检查 + Prometheus /metrics 导出。
        app.router.add_get("/healthz", self._health_liveness)
        app.router.add_get("/healthz/live", self._health_liveness)
        app.router.add_get("/healthz/ready", self._health_readiness)
        app.router.add_get("/healthz/deps", self._health_dependencies)
        app.router.add_get("/metrics", self._metrics)
        app.router.add_get("/version", self._version)
        app.router.add_get("/v1/models", self._list_models)
        app.router.add_post("/api/reload", lambda r: self._reload(r, handler))
        return app

    # ------------------------------------------------------------------
    # T33 分层健康检查（R-P2-07/08）
    # ------------------------------------------------------------------

    def _available_route_count(self) -> int:
        return sum(1 for k in self.keys if getattr(k, "is_available", lambda: True)())

    async def _health_liveness(self, _request: web.Request) -> web.Response:
        from ..observability.health import build_liveness, sanitize_health_payload
        return web.json_response(sanitize_health_payload(build_liveness()))

    async def _health_readiness(self, _request: web.Request) -> web.Response:
        from ..observability.health import (
            build_readiness,
            migration_status,
            sanitize_health_payload,
        )
        migration_ok, migration_detail = await migration_status(self.store)
        routes_ok = self._available_route_count() > 0
        routes_detail = "ok" if routes_ok else "no available upstream route"
        handler = getattr(self, "_proxy_handler", None)
        worker_ok = bool(handler is not None and getattr(handler, "_bg_running", False))
        worker_detail = (
            "ok" if worker_ok
            else "background worker not started" if handler is not None
            else "no proxy handler"
        )
        payload, status = build_readiness(
            migration_ok=migration_ok,
            migration_detail=migration_detail,
            routes_ok=routes_ok,
            routes_detail=routes_detail,
            worker_ok=worker_ok,
            worker_detail=worker_detail,
        )
        return web.json_response(sanitize_health_payload(payload), status=status)

    async def _health_dependencies(self, _request: web.Request) -> web.Response:
        from ..observability.health import (
            build_dependency_status,
            dependency_item,
            migration_status,
            sanitize_health_payload,
        )
        deps: list[dict] = []
        mig_ok, mig_detail = await migration_status(self.store)
        deps.append(dependency_item("store", mig_ok, mig_detail))
        up_ok = bool(self.upstream_clients)
        deps.append(dependency_item(
            "upstream",
            up_ok,
            "ok" if up_ok else "no upstream clients configured",
        ))
        # 工具执行器（T25/26）：无可注入执行器时报告 optional_unavailable。
        executor = getattr(self, "tool_executor", None)
        deps.append(dependency_item(
            "tool_executor",
            executor is not None,
            "ok" if executor is not None else "no tool executor configured",
            optional=True,
        ))
        return web.json_response(
            sanitize_health_payload(build_dependency_status(deps)),
        )

    async def _metrics(self, _request: web.Request) -> web.Response:
        from ..observability.metrics import render_metrics
        # Prometheus 标准 content-type 带 version/charset 参数；
        # aiohttp 的 content_type 参数不接受 charset，故直接设头。
        resp = web.Response(text=render_metrics(), content_type="text/plain")
        resp.headers["Content-Type"] = "text/plain; version=0.0.4; charset=utf-8"
        return resp

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

        content_type = resp.headers.get("Content-Type", "").lower()

        # SSE 响应禁止压缩（R-P1-27）：gzip 会缓冲输出，event 边界被延迟，
        # heartbeat 永远到不了客户端。按 content-type 显式跳过，不能依赖
        # Content-Encoding（SSE 响应不会自己设置该头）。
        if content_type.startswith("text/event-stream"):
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