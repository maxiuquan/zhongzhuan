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
from .auth import make_auth_middleware, init_jwt_secret, auth_enabled
from .notify import configure_reload_target
from .ui import mount_ui


class AdminServer:
    def __init__(self, store: Store, version: str = "0.1.0", config=None) -> None:
        self.store = store
        self.version = version
        self.config = config

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)

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
        return app