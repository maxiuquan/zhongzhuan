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
from .api_exposure import register_routes as register_exposure
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
            except Exception:
                from loguru import logger

                logger.exception(f"[admin] internal error: {request.method} {request.path}")
                return web.json_response(
                    {"error": {"message": "internal server error", "type": "internal_error"}},
                    status=500,
                )

        app.middlewares.append(error_middleware)

        # JWT auth middleware（传 store 以启用 JWT 可吊销校验）
        init_jwt_secret()
        app.middlewares.append(make_auth_middleware(self.store))

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
        register_exposure(app, self)

        # UI
        mount_ui(app, self)

        # T33 (R-P2-07/08)：admin 控制面也暴露分层健康检查（复用 observability.health）。
        app.router.add_get("/healthz", self._health_store)
        app.router.add_get("/healthz/live", self._health_liveness)
        app.router.add_get("/healthz/ready", self._health_readiness)
        app.router.add_get("/healthz/deps", self._health_dependencies)
        return app

    async def _health_store(self, _request: web.Request) -> web.Response:
        """存储/密钥降级可见性（2026-09-28 静默降级事故的补救）。

        **零 SQL**：``store.status()`` 是纯内存快照（SELECT 1 会唤醒休眠
        集群、烧在线税，健康检查轮询绝不能碰库）。“库真的通不通”由
        ``pool_alive`` + ``crypto_ready`` + 请求路径的真实表现间接反映。
        非 ``/api/`` 路径天然绕过 JWT（auth middleware 白名单），无需登录。

        2026-10-01 补丁（Aiven 关停 20h 而 healthz 报 ok 的事故）：新增
        **数据陈旧检测**——常驻池模式（``idle_release_seconds == 0``）下，
        worker 安全网每 30s 必有一次成功查询，``idle_seconds`` 超过阈值
        即说明“所有到库的访问都在失败”，必须报 degraded（pool_alive /
        crypto_ready 是内存态，库被平台关停时它们**不会**变化，唯独
        ``idle_seconds`` 不会撒谎）。
        """
        import os

        from ..crypto import ready as crypto_ready

        store_status = self.store.status()
        payload = {
            "status": "ok",
            "backend": self.store.dialect,
            "store": store_status,
            "crypto_ready": crypto_ready(),
        }
        stale_after = float(os.getenv("ZHONGZHUAN_HEALTHZ_STALE_SECONDS", "900"))
        idle = store_status.get("idle_seconds")
        permanent_pool = store_status.get("idle_release_seconds") == 0
        stale = (
            permanent_pool
            and stale_after > 0
            and isinstance(idle, (int, float))
            and idle > stale_after
        )
        # 降级态仍然 200（本端点只做可见性，不做拨测判定）：
        # - crypto 未就绪 = key 池残缺，代理请求会 fail-closed；
        # - db_stale = 常驻池模式下太久没有成功查询，库大概率已不可达。
        if not payload["crypto_ready"] and not store_status.get("pool_alive", True):
            payload["status"] = "degraded"
            payload["reason"] = "crypto_not_ready_and_pool_down"
        elif stale:
            payload["status"] = "degraded"
            payload["reason"] = f"db_no_successful_query_for_{idle}s"
        return web.json_response(payload)

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
