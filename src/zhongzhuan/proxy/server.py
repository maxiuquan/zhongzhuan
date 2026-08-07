"""Proxy HTTP server."""

from __future__ import annotations

from aiohttp import web
from aiohttp.payload import Payload

from .auth import make_proxy_auth_middleware
from .cors import make_cors_middleware
from .handler import make_handler
from ..store import Store
from ..upstream import UpstreamClient

#: Codex (desktop) model-discovery endpoint — degraded-mode fallback model
#: slugs used only when the store is unavailable.  In normal operation the
#: Real list comes from the store's *official* models (``is_fallback=False``,
#: ``enabled=True``) — that's what Codex should see in production. This static
#: list is only a last-resort safety net used when the store can't be reached
#: (e.g. DB down), so Codex still gets a usable catalog instead of an empty one.
_CODEX_OFFICIAL_MODEL_SLUGS = [
    "gpt-5.6-sol",
    "agnes-2.5-flash",
    "glm-5.2",
]


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
        *,
        responses_bridge=None,
        feature_flags=None,
    ) -> None:
        self.upstream_clients = upstream_clients
        self.api_key = api_key
        self.keys = keys or []
        self._effective_keys = self.keys
        self.proxy_timeout = proxy_timeout
        self.models = models or []
        self.groups = groups or []
        self.store = store
        self.load_keys_fn = load_keys_fn
        self.sticky_ttl = sticky_ttl
        # T22: Responses v3 bridge wiring.  ``responses_bridge`` is the config
        # object (``enabled`` + ``rollout``); when omitted the bridge stays
        # disabled so existing callers (and the store-less setup) are
        # unaffected.  ``feature_flags`` may be injected directly for tests.
        self.responses_bridge = responses_bridge
        if feature_flags is None:
            from .feature_flags import ResponsesFeatureFlags

            feature_flags = ResponsesFeatureFlags(responses_bridge)
        self._feature_flags = feature_flags
        #: T04 / P0-8: the record written by :meth:`_audit_startup`, kept so
        #: tests and health introspection can read back what was audited.
        self.startup_audit: dict[str, str] | None = None

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
            the_keys = [
                KeyHealth(
                    key_id=0,
                    api_key=self.api_key,
                    window=SlidingWindow(60, 1000),
                    upstream_base=fallback_base,
                )
            ]

        self._effective_keys = the_keys
        handler = make_handler(
            upstream_clients=self.upstream_clients,
            keys=the_keys,
            proxy_timeout=self.proxy_timeout,
            store=self.store,
            load_keys_fn=self.load_keys_fn,
            groups=self.groups,
            sticky_ttl=self.sticky_ttl,
            feature_flags=self._feature_flags,
            v3_handler=self._build_v3_handler(),
        )
        #: 供 readiness 检查 handler 的 background worker 生命周期（T33）。
        self._proxy_handler = handler

        # 注册后台任务钩子（优化点4+5：sticky 清理 + 健康状态快照）
        async def _on_startup(app: web.Application) -> None:
            # T04 / P0-8 (AC-8.1): the switch audit is the FIRST thing written
            # at startup — before any worker can serve traffic — so the log
            # always opens with "which implementation is in force".
            self._audit_startup()
            await handler.start_background_tasks()

        async def _on_cleanup(app: web.Application) -> None:
            await handler.stop_background_tasks()

        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)

        # T22 / R-P1-28: the six exact Responses routes MUST be registered
        # before the ``/v1/{tail:.*}`` catch-all — aiohttp matches routes in
        # registration order, so a catch-all registered first would swallow
        # every Responses request.  ``/v1/responses/compact`` must come before
        # the parameterised ``{response_id}`` routes or it would be captured as
        # a response id.  All six routes point at the SAME handler (which
        # contains the single v2/v3 fork point, R-P0-22).
        app.router.add_post("/v1/responses/compact", handler)
        app.router.add_post("/v1/responses", handler)
        app.router.add_get("/v1/responses/{response_id}", handler)
        app.router.add_delete("/v1/responses/{response_id}", handler)
        app.router.add_post("/v1/responses/{response_id}/cancel", handler)
        app.router.add_get("/v1/responses/{response_id}/input_items", handler)
        # Codex (desktop) model-discovery endpoint — must be registered before
        # the /v1/{tail:.*} catch-all so it is matched explicitly.  Both the
        # /v1/ and the alias /api/ form are handled by the same method, which
        # performs its own Bearer-token check (see proxy/auth.py exemption).
        app.router.add_get("/v1/api/codex/models", self._codex_models)
        app.router.add_get("/api/codex/models", self._codex_models)
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

    # ------------------------------------------------------------------
    # T04 / P0-8: startup switch audit
    # ------------------------------------------------------------------

    def _audit_startup(self, logger: object = None) -> dict[str, str] | None:
        """Write the single-line v3 switch audit banner (AC-8.1).

        Emits exactly ONE line carrying the five mandated fields
        (``operator`` / ``timestamp`` / ``reason`` / ``effective_version`` /
        ``source``).  The redacted effective-config dump is deliberately NOT
        triggered here: it renders one line per config leaf (hundreds of
        lines) and would flood a supervisor's stderr pipe before the proxy
        binds its port.  The v3 switch section is instead folded into
        :func:`~zhongzhuan.config.effective.format_effective_config`, so any
        caller that *chooses* to dump the effective config still sees it.

        Never raises: an unloggable banner must not stop the proxy from
        serving.  Returns the audit record (also stored on
        ``self.startup_audit``) or ``None`` when auditing failed.
        """
        if logger is None:  # pragma: no cover - trivial default wiring
            from loguru import logger as _logger

            logger = _logger

        record: dict[str, str] | None = None
        try:
            flags = self._feature_flags
            audit = getattr(flags, "log_audit_record", None)
            if callable(audit):
                record = audit(reason="boot", operator="startup", logger=logger)
                self.startup_audit = record
        except Exception:  # pragma: no cover - logging must never break startup
            record = None

        return record

    # ------------------------------------------------------------------
    # T22: Responses v3 handler construction (fail-safe for store-less setups)
    # ------------------------------------------------------------------

    def _build_v3_handler(self):
        """Build the store-backed v3 resource handler, or ``None``.

        v3 requires a store (it persists responses / input items / state
        chains).  When ``store is None`` the bridge is unavailable and every
        Responses request falls back to the legacy path — a deterministic,
        testable fail-safe (no half-wired v3 with a ``None`` store).
        """
        if self.store is None:
            return None
        from ..responses_v3.handler import ResponsesV3Handler
        from ..store.response_store import ResponseStore

        return ResponsesV3Handler(ResponseStore(self.store))

    def _available_route_count(self) -> int:
        return sum(1 for k in self._effective_keys if getattr(k, "is_available", lambda: True)())

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
            "ok" if worker_ok else "background worker not started" if handler is not None else "no proxy handler"
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
        deps.append(
            dependency_item(
                "upstream",
                up_ok,
                "ok" if up_ok else "no upstream clients configured",
            )
        )
        # 工具执行器（T25/26）：无可注入执行器时报告 optional_unavailable。
        executor = getattr(self, "tool_executor", None)
        deps.append(
            dependency_item(
                "tool_executor",
                executor is not None,
                "ok" if executor is not None else "no tool executor configured",
                optional=True,
            )
        )
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
        from loguru import logger

        n = await handler.reload_keys()
        # 刷新 /v1/models 快照（models + groups）：此前 reload 只更新 handler
        # 内部的 keys/groups，server 的 self.models/self.groups 仍是启动快照，
        # 导致管理端新建/修改模型后 /v1/models 不更新，必须重启服务才生效。
        if self.store is not None:
            try:
                from ..store.models import list_models as _list_models_db
                from ..store.groups import list_groups as _list_groups_db

                ms = await _list_models_db(self.store)
                self.models = [{"name": m.name} for m in ms]
                rows = await _list_groups_db(self.store)
                self.groups = [
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "strategy": r.get("strategy"),
                        "members": [m["model_id"] for m in (r.get("members") or [])],
                    }
                    for r in rows
                ]
            except Exception:
                logger.exception("reload models/groups snapshot failed")
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

    # ------------------------------------------------------------------
    # Codex (desktop) model-discovery endpoint
    #   GET /v1/api/codex/models   (alias: GET /api/codex/models)
    # Codex 0.146.x calls this with `?client_version=...` and
    # `Authorization: Bearer <key>` and expects `{"models": [ModelInfo...]}`.
    # The ModelInfo shape mirrors codex-rs' `ModelInfo` struct exactly — every
    # non-`#[serde(default)]` field must be present or Codex refuses to start.
    # ------------------------------------------------------------------

    async def _codex_models(self, request: web.Request) -> web.Response:
        # Auth: same token table as /v1/responses (proxy/auth.py exempts this
        # path so the check lives here and is identical for both URL forms).
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token:
            token = request.headers.get("x-api-key", "").strip()
        if not token or self.store is None:
            return self._codex_unauthorized()
        from ..store.access_tokens import get_token_by_value

        at = await get_token_by_value(self.store, token)
        # Reuse the same validity rules as the proxy auth middleware
        # (enabled / not revoked / not expired / quota), but always answer 401
        # for this discovery endpoint regardless of the failure reason.
        ok, _ = at.check_quota("") if at is not None else (False, "no token")
        if not ok:
            return self._codex_unauthorized()

        names = await self._codex_model_slugs()
        models = [self._build_codex_model_info(n) for n in names]
        # `?client_version=...` is informational (Codex 0.146.x sends it); the
        # response shape is exactly `{"models": [...]}`.
        return web.json_response({"models": models})

    @staticmethod
    def _codex_unauthorized() -> web.Response:
        return web.json_response(
            {"error": {"message": "invalid or missing access token", "type": "unauthorized"}},
            status=401,
        )

    async def _codex_model_slugs(self) -> list[str]:
        """Return the model slugs Codex should see — our *official* models.

        Returns every enabled, non-fallback model from the store (production
        reality); falls back to a static official-model list only when the
        store is unreachable.
        """
        if self.store is not None:
            try:
                from ..store.models import list_models as _list_models_db

                rows = await _list_models_db(self.store)
                names = [
                    m.name
                    for m in rows
                    if m.enabled and not getattr(m, "is_fallback", False)
                ]
                if names:
                    return names
            except Exception:
                pass
        return list(_CODEX_OFFICIAL_MODEL_SLUGS)

    @staticmethod
    def _build_codex_model_info(slug: str) -> dict:
        """Build a codex-rs ``ModelInfo`` dict for *slug*.

        Every field below is required by codex-rs' deserializer (any missing
        non-``#[serde(default)]`` field makes Codex refuse to start).  Values
        are chosen to be safe for the OpenCode Free fallback models we serve:
        no reasoning advertised (upstreams may not support it), parallel tool
        calls on (needed for the MCP sub-agent bridge), and
        ``use_responses_lite: false`` so Codex stays on the legacy request path.
        """
        display = slug[len("oc-"):] if slug.startswith("oc-") else slug
        return {
            "slug": slug,
            "display_name": display,
            "description": None,
            "supported_reasoning_levels": [],
            "shell_type": "shell_command",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1,
            "availability_nux": None,
            "upgrade": None,
            "base_instructions": "You are a helpful coding agent.",
            "supports_reasoning_summaries": False,
            "support_verbosity": False,
            "default_verbosity": None,
            "apply_patch_tool_type": None,
            "truncation_policy": {"mode": "bytes", "limit": 200000},
            "supports_parallel_tool_calls": True,
            "experimental_supported_tools": [],
            "use_responses_lite": False,
        }


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
        if body is None or isinstance(body, Payload) or len(body) < min_size:
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
