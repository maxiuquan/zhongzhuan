"""Admin HTTP server."""

from __future__ import annotations

from aiohttp import web

from ..store import Store
from .api_models import register_routes as register_models
from .api_keys import register_routes as register_keys
from .api_groups import register_routes as register_groups
from .api_stats import register_routes as register_stats
from .api_logs import register_routes as register_logs
from .api_service import register_routes as register_service
from .api_export_import import register_routes as register_export
from .api_auth import register_routes as register_auth
from .api_tokens import register_routes as register_tokens
from .api_fallback import register_routes as register_fallback
from .auth import make_auth_middleware, init_jwt_secret
from .notify import configure_reload_target
from .ui import mount_ui


class AdminServer:
    def __init__(self, store: Store, version: str = "0.1.0", config=None) -> None:
        self.store = store
        self.version = version
        self.config = config

    def app(self) -> web.Application:
        from ..proxy.cors import make_cors_middleware

        app = web.Application(
            client_max_size=64 * 1024 * 1024,
            middlewares=[make_cors_middleware()],  # admin 也启用 CORS
        )

        # Configure proxy reload target so admin edits hot-reload the proxy
        # without a restart. Falls back to defaults if config is unavailable.
        try:
            cfg = self.config
            port = cfg.server.proxy.port if cfg else 8443
            use_tls = bool(getattr(cfg.server.tls, "enabled", True)) if cfg else True
            configure_reload_target(port, use_tls)
        except Exception:
            configure_reload_target(8443, True)

        @web.middleware
        async def error_middleware(request, handler):
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as e:
                return web.json_response(
                    {"error": {"message": str(e), "type": "internal_error"}},
                    status=500,
                )

        app.middlewares.append(error_middleware)

        # JWT auth middleware
        init_jwt_secret()
        app.middlewares.append(make_auth_middleware())

        # API routes
        register_auth(app, self)
        register_models(app, self)
        register_keys(app, self)
        register_groups(app, self)
        register_stats(app, self)
        register_logs(app, self)
        register_service(app, self)
        register_export(app, self)
        register_tokens(app, self)
        register_fallback(app, self)

        # UI
        mount_ui(app, self)

        # T33 (R-P2-07/08)：admin 控制面也暴露分层健康检查（复用 observability.health）。
        app.router.add_get("/healthz/live", self._health_liveness)
        app.router.add_get("/healthz/ready", self._health_readiness)
        app.router.add_get("/healthz/deps", self._health_dependencies)
        return app

    # ------------------------------------------------------------------
    # T33 分层健康检查（admin 侧：迁移完成 + store 就绪）
    # ------------------------------------------------------------------

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
        payload, status = build_readiness(
            migration_ok=migration_ok,
            migration_detail=migration_detail,
            routes_ok=True,
            routes_detail="admin control plane",
            worker_ok=True,
            worker_detail="admin has no async worker",
        )
        return web.json_response(sanitize_health_payload(payload), status=status)

    async def _health_dependencies(self, _request: web.Request) -> web.Response:
        from ..observability.health import (
            build_dependency_status,
            dependency_item,
            migration_status,
            sanitize_health_payload,
        )

        mig_ok, mig_detail = await migration_status(self.store)
        deps = [dependency_item("store", mig_ok, mig_detail)]
        return web.json_response(
            sanitize_health_payload(build_dependency_status(deps)),
        )
