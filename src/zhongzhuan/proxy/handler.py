"""/v1/* route handler: pass-through with multi-key retry + protocol translation."""

from __future__ import annotations

import json
import time
from typing import Any

from aiohttp import web

import asyncio

from ..store import Store
from ..store.logs import log_request
from ..upstream import UpstreamClient
from ..config.timeouts import DEFAULT_TIMEOUT_POLICY, TimeoutPolicy
from ..observability.metrics import record_v3_fallback
from ..responses_v3.background import BackgroundWorker
from ..responses_v3.capability import CapabilityRouter, StaticRouteRegistry
from ..responses_v3.chain import build_upstream_input, chain_error_response
from ..responses_v3.pipeline import PipelineConfig, ResponsePipeline
from ..responses_v3.request_sanitizer import RequestSanitizer, capability_values
from ..responses_v3.upstream_chunk_adapter import UpstreamSSEChunkAdapter

#: 需要做外来客户端标识中性化的指令类角色。OpenAI 如今把系统提示词放在
#: ``developer`` 角色（官方 Codex CLI 的 "You are Codex" 即在此），我们只认
#: ``system`` 会漏掉，导致该标识原样透传上游而 403（2026-08-06 实测）。
_INSTRUCTION_ROLES = ("system", "developer")

from .context import RequestContextBuilder
from .ratelimit import KeyHealth, STATE_HEALTHY
from .retry import (
    mark_network_failure,
    mark_success,
    learn_rate_limits,
    classify_failure,
    reason_for_exhaustion,
)
from .scheduler import pick_key
from .protocol.translate_a2o import translate_request_a2o, translate_response_o2a
from .protocol.translate_o2a import translate_request_o2a, translate_response_a2o
from .protocol.errors import translate_error_a2o, translate_error_o2a
from .protocol.stream_o2a import StreamO2A
from .protocol.stream_a2o import StreamA2O
from .protocol.responses import (
    convert_responses_request_to_chatcompletions,
    chatcompletions_to_responses,
    ResponsesStreamTranslator,
    CompositeStreamTranslator,
)
from .protocol.responses_models import TerminalReason
from .protocol.translator_base import finish_translator

from loguru import logger as _lg


def make_handler(
    upstream_clients: dict[str, UpstreamClient],
    keys: list[KeyHealth],
    proxy_timeout: float | None = None,
    store: Store | None = None,
    load_keys_fn=None,
    groups: list[dict] | None = None,
    sticky_ttl: float = 1800.0,
    timeouts: TimeoutPolicy | None = None,
    *,
    feature_flags=None,
    v3_handler=None,
) -> ProxyHandler:
    """Factory: create a ProxyHandler for the aiohttp route.

    ``proxy_timeout`` (single float) is the deprecated shape kept for existing
    callers/tests; ``timeouts`` is the six-layer policy introduced by T01.

    ``feature_flags`` / ``v3_handler`` are the T22 Responses v3 wiring: when
    both are supplied, Responses requests that pass the feature flag are served
    by the v3 resource handler at the single fork point; otherwise the legacy
    path is used (R-P0-22).
    """
    return ProxyHandler(
        clients=upstream_clients,
        keys=keys,
        store=store,
        proxy_timeout=proxy_timeout,
        load_keys_fn=load_keys_fn,
        groups=groups,
        sticky_ttl=sticky_ttl,
        timeouts=timeouts,
        feature_flags=feature_flags,
        v3_handler=v3_handler,
    )


def _swap_model_name(raw_body: bytes, old_name: str, new_name: str) -> bytes:
    """Replace model name at byte level to avoid expensive json.loads/dumps.

    Only falls back to full JSON parse when the simple byte replacement
    would produce incorrect results (e.g. the old substring appears in
    unexpected places like values for other fields).
    """
    if not raw_body or not old_name or not new_name:
        return raw_body

    # Fast path: simple byte substitution.
    # The pattern is '"model": "OLD"' → '"model": "NEW"'.
    search = b'"model": "%s"' % old_name.encode()
    replacement = b'"model": "%s"' % new_name.encode()
    if search in raw_body:
        return raw_body.replace(search, replacement)

    # Slow path: full JSON parse + model-only swap.
    try:
        obj = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return raw_body
    if "model" in obj and obj["model"] == old_name:
        obj["model"] = new_name
    return json.dumps(obj, ensure_ascii=False).encode()


class ProxyHandler:
    """Handles /v1/* (chat/completions, messages, models) with retry + protocol translation."""

    def __init__(
        self,
        clients: dict[str, UpstreamClient],
        keys: list[KeyHealth],
        store: Store | None = None,
        proxy_timeout: float | None = None,
        load_keys_fn=None,
        groups: list[dict] | None = None,
        sticky_ttl: float = 1800.0,
        timeouts: TimeoutPolicy | None = None,
        *,
        feature_flags=None,
        v3_handler=None,
    ) -> None:
        self._clients = clients
        self._keys = keys
        self.store = store
        # T22: Responses v3 wiring.  ``_feature_flags`` decides whether a
        # Responses request is served by ``_v3`` at the single fork point; a
        # ``None`` v3 handler means v3 is unavailable (e.g. store-less setup)
        # and every Responses request falls back to the legacy path.
        self._feature_flags = feature_flags
        self._v3 = v3_handler
        # Timeout wiring (T01): the six-layer policy wins; the legacy single
        # float is only used when no policy was supplied.
        self._timeouts: TimeoutPolicy | None = timeouts
        self._timeout = proxy_timeout
        if self._timeouts is None and proxy_timeout is None:
            self._timeouts = DEFAULT_TIMEOUT_POLICY
        self._load_keys_fn = load_keys_fn
        # RequestContext building (T02): single-parse preamble.
        self._ctx_builder = RequestContextBuilder()
        # One lossless v3 fact model per request. Capability routing and sticky
        # binding consume this same object instead of re-scanning the payload.
        self._request_sanitizer = RequestSanitizer()
        # Lazy client cache (upstream_base → UpstreamClient)
        self._client_cache: dict[str, UpstreamClient] = dict(clients)
        self._lock = asyncio.Lock()
        # Group routing map: name → {"id": int, "strategy": str, "members": set[model_id]}
        self._groups: dict[str, dict] = {}
        self._set_groups(groups or [])
        # Sticky session: session_key → (key_id, expire_at). Keeps multi-turn
        # conversations on the same upstream key to avoid mid-conversation
        # model switches that cause hallucination spikes.
        self._sticky: dict[str, tuple[int, float]] = {}
        #: session_key → capabilities snapshot at bind time (T35 / R-P1-61).
        #: Kept separate from ``_sticky`` so the existing two-tuple layout stays
        #: intact (existing tests write ``_sticky[s] = (key_id, expire_at)``).
        self._sticky_caps: dict[str, frozenset[str]] = {}
        #: session_key → failover reason, flushed to the store by the caller
        #: (T35 / R-P1-61 故障迁移记录). Populated when a sticky key is
        #: rejected for health / capability mismatch.
        self._binding_failover_reasons: dict[str, str] = {}
        self._sticky_ttl: float = sticky_ttl
        #: Lazy ResponseStore for session→route binding persistence.
        self._rs = None
        #: Injectable clock (T35): tests swap in a FakeClock to avoid real
        #: waits when exercising the sticky TTL.
        self._now = time.time
        # 后台任务引用（优化点4+5：sticky 清理 + 健康状态快照）
        self._bg_tasks: list[asyncio.Task] = []
        self._bg_running = False
        #: P0-6: the v3 ``background=true`` worker, owned by this handler's
        #: background-task lifecycle.  ``None`` until ``start_background_tasks``
        #: finds a store-backed v3 setup (a store-less proxy never has one).
        self._v3_worker: BackgroundWorker | None = None
        #: AC-7.4: lazily built once from ``responses_bridge.timeout.*``.
        self._v3_pipeline_cfg: PipelineConfig | None = None
        # R-P1-35: let ``POST /v1/responses/{id}/cancel`` reach the worker that
        # is actually running the job.  A *provider* is handed over instead of
        # the worker itself because the worker above is built lazily on first
        # use -- binding the value now would bind ``None`` for the life of the
        # process and a cancel would silently degrade to "set a flag and hope".
        binder = getattr(self._v3, "set_worker_provider", None)
        if binder is not None:
            binder(self._v3_background_worker)

    def _set_groups(self, groups: list[dict]) -> None:
        """Rebuild the group routing map from a list of group dicts."""
        gm: dict[str, dict] = {}
        for g in groups:
            name = (g.get("name") or "").strip()
            if not name:
                continue
            gm[name] = {
                "id": g.get("id"),
                "strategy": g.get("strategy", "round_robin"),
                "members": set(g.get("members") or []),
            }
        self._groups = gm

    def _resolve_candidates(self, requested_model: str) -> list[KeyHealth]:
        """Pick candidate keys based on the requested model name.

        - requested_model matches a *group* name → all available keys whose
          model belongs to that group's members (group-level load balancing
          via key health scoring; member order/strategy is best-effort).
        - requested_model matches a *model* name OR its aliases → keys bound to that model.
        - requested_model was specified but matches nothing (or every matched
          key is unavailable) → **empty list**.  Never fall back to keys of a
          different model: a client that named a specific model must not be
          silently served by another model (previously this fell through to
          ``all available keys``, which routed e.g. gpt-5.6-sol requests to
          agnes/oc-* after every matching key hit 403).
        - requested_model is empty → all available keys (no model named:
          serve from whatever is healthy).
        - **例外（向后兼容）**：所有可用 key 的 ``model_name`` 都为空（单 key
          透传 / ``ProxyServer(api_key=...)`` 简写模式，未绑定任何模型）时，
          不构成"跨模型路由"风险，仍回退到所有可用 key。
        """
        available = [k for k in self._keys if k.is_available()]

        if requested_model:
            # 1. Group name match
            grp = self._groups.get(requested_model)
            if grp and grp["members"]:
                member_ids = grp["members"]
                matched = [k for k in available if k.model_id in member_ids]
                if matched:
                    return matched
            # 2. Model name match (直接匹配 model_name)
            matched = [k for k in available if k.model_name == requested_model]
            if matched:
                return matched
            # 3. 别名匹配：检查 KeyHealth 是否有 aliases 属性（从 DB 加载时设置）
            matched = [
                k
                for k in available
                if getattr(k, "aliases", "")
                and requested_model in [a.strip() for a in str(getattr(k, "aliases", "")).split(",") if a.strip()]
            ]
            if matched:
                return matched
            # 指定了模型但组/模型/别名都匹配不到（或全不可用）→ 不跨模型兜底。
            # 唯一例外：所有可用 key 都未绑定模型名（透传模式）→ 无跨模型风险，
            # 保持宽松行为（向后兼容 ProxyServer(api_key=...) 简写与旧测试）。
            if all(not k.model_name for k in available):
                return available
            return []

        return available

    # ------------------------------------------------------------------
    # T22: Responses v3 HTTP adapter (the only v3 entry point in production).
    # ------------------------------------------------------------------

    async def _dispatch_v3(self, request: web.Request, ctx) -> web.Response:
        """Serve a Responses request through the v3 resource handler.

        This is the production adapter that turns a ``web.Request`` into the
        ``dispatch(method, path, *, workspace_id, body)`` resource call and
        maps the ``(status, body)`` result back to a ``web.Response``
        (R-P1-28: six endpoints must be reachable over HTTP, not just from a
        unit test).
        """
        body_obj = ctx.body or {}
        # GET query params (input_items pagination) are merged into the body so
        # the resource handler's ``after`` / ``limit`` parsing sees them; the
        # resource layer rejects invalid values with a standard 400 (T22).
        query = request.query
        if query:
            body_obj = dict(body_obj)
            for key in ("after", "limit"):
                if key in query:
                    body_obj[key] = query[key]
        # The access-token row is the current tenant boundary.  Anonymous/dev
        # traffic keeps the historical default workspace; authenticated tokens
        # are isolated from one another in ResponseStore queries.
        token_id = int(request.get("token_id", 0) or 0)
        workspace_id = f"token:{token_id}" if token_id > 0 else ""
        try:
            status, payload = await self._v3.dispatch(
                ctx.method,
                ctx.path,
                workspace_id=workspace_id,
                body=body_obj,
            )
        except Exception as exc:  # never leak a traceback to the client
            _lg.exception(f"[{id(request):x}] v3 dispatch failed: {type(exc).__name__}: {exc}")
            status, payload = (
                500,
                {
                    "error": {
                        "message": "internal server error",
                        "type": "internal_server_error",
                        "code": "internal_server_error",
                    }
                },
            )
        return web.json_response(payload, status=status)

    # ------------------------------------------------------------------
    # T26: v3 create over the real production upstream chain.
    #
    # The legacy non-stream path (below) already owns the multi-key
    # scheduler, sticky binding, health state, retry/failure classification
    # and the Responses -> Chat/Anthropic translators.  v3 create reuses that
    # chain instead of building a second transport:
    #
    #   1. capability route  -> ExecutionMode + upstream_path (NATIVE/EMULATE/
    #      TRANSLATE, or a standard 400/503 error);
    #   2. response_id is minted ONCE here and used by the returned object,
    #      the persisted row and (in T26 stream) the SSE lifecycle;
    #   3. the sanitizer's payload becomes the single outbound body source;
    #   4. on success the row is moved to a terminal state with output/usage
    #      so retrieve() returns the completed resource (store=true only).
    # ------------------------------------------------------------------

    def _new_response_id(self) -> str:
        import uuid

        return "resp_" + uuid.uuid4().hex

    def _v3_workspace_id(self, request: web.Request) -> str:
        """The access-token row is the current tenant boundary (T22)."""
        token_id = int(request.get("token_id", 0) or 0)
        return f"token:{token_id}" if token_id > 0 else ""

    def _capability_router(self, candidates: list[KeyHealth]) -> CapabilityRouter:
        """One router per create, over the *filtered* candidate pool."""
        return CapabilityRouter(StaticRouteRegistry(candidates))

    def _v3_response_store(self):
        rs = self._response_store()
        if rs is not None:
            return rs
        # A store-less setup never reaches here: ``_v3 is None`` short-circuits
        # the fork before create dispatch.  Defensive fallback to the handler
        # wiring used by the v3 resource endpoints.
        if self._v3 is not None and hasattr(self._v3, "_store"):
            return self._v3._store
        return None

    async def _persist_v3_terminal(
        self,
        *,
        response_id: str,
        workspace_id: str,
        status: str,
        usage: dict[str, Any] | None = None,
        output: list[Any] | None = None,
        terminal_reason: str = "",
        error: str = "",
    ) -> None:
        rs = self._v3_response_store()
        if rs is None:
            return
        try:
            await rs.update_status(
                response_id,
                status,
                workspace_id=workspace_id,
                terminal_reason=terminal_reason,
                error=error,
                usage=usage or {},
                output=output or [],
            )
            if output:
                await rs.save_output_items(response_id, output)
        except Exception:
            _lg.exception(f"[v3] persist terminal {status} for {response_id} failed (workspace={workspace_id!r})")

    async def _prepare_v3_create(
        self,
        request: web.Request,
        ctx,
        candidates: list[KeyHealth],
        *,
        persist_skeleton: bool = True,
    ) -> tuple["_V3CreateContext | None", web.Response | None]:
        """Phase A of a v3 create: every decision that is still allowed to fail.

        The non-stream, stream and background create paths share this verbatim,
        which is what keeps them from drifting apart.  The step order is the
        one §9.7 declares non-commutative:

        ``resolve_chain`` → ``sanitize`` → ``build_upstream_input`` injection
        → capability route → skeleton persist → outbound body.

        The injection (P0-5) **must** precede the protocol translation done by
        :meth:`_prepare_v3_upstream_call`, because the translator reads
        ``body["input"]``; injecting one step later silently drops the parent
        turns.  The chain guard **must** precede every network call, so a
        broken ``previous_response_id`` costs zero upstream requests.

        Args:
            persist_skeleton: When ``False``, skip step A5.  The background
                path passes ``False`` because ``BackgroundWorker.enqueue``
                writes the ``queued`` row, its ``response.queued`` event and
                the job row as one unit — two writers for one ``response_id``
                is a UNIQUE-constraint violation, and "whoever runs the job
                owns the row" is the rule that keeps recovery unambiguous.

        Returns ``(context, None)`` or ``(None, error_response)``.  No network
        I/O happens here, so an error means the client never saw a byte and a
        standard JSON error is always still legal (§9.2).
        """
        body_obj = dict(ctx.body or {})
        workspace_id = self._v3_workspace_id(request)
        response_id = self._new_response_id()

        # A1. R-P0-29: a chain that cannot be resolved is a standard error,
        # never a silent stateless turn.  The resource layer owns the guard; we
        # only let it fail fast before any network I/O.
        previous_response_id = str(body_obj.get("previous_response_id") or "")
        resolution = None
        if previous_response_id and self._v3 is not None:
            resolution = await self._v3.resolve_chain(
                previous_response_id,
                workspace_id=workspace_id,
            )
            if not resolution.ok:
                status, payload = chain_error_response(resolution)
                return None, web.json_response(payload, status=status)

        # A2. One lossless fact model per create; capability routing and sticky
        # binding both read it instead of re-scanning the payload.
        sanitized = self._request_sanitizer.sanitize(body_obj)

        # A3. P0-5: the resolved chain becomes the upstream ``input``.  It is a
        # *separate* dict from ``body_obj`` on purpose -- what we persist is
        # this turn's input (the parent turns are already stored under their own
        # response ids), what we send upstream is the flattened history.
        upstream_body = dict(body_obj)
        if resolution is not None:
            upstream_body["input"] = build_upstream_input(resolution, body_obj.get("input"))
        _lg.debug("[v3-debug] model=%s prev=%r input_len=%d tools=%s", body_obj.get("model"), previous_response_id, len(upstream_body.get("input") or []), upstream_body.get("tools"))

        # A4. Capability route over the *filtered* candidate pool.  A hosted
        # tool with no executor is a standard 400; a declared-but-down route is
        # 503.  Both must be answered BEFORE any streaming response is prepared
        # (T26: never a fake 200 over a missing executor).
        decision = self._capability_router(candidates).route(sanitized, candidates)
        if hasattr(decision, "to_response"):
            # CapabilityError: standard 400 (no executor) or 503 (route down).
            status, payload = decision.to_response()
            return None, web.json_response(payload, status=status)

        store_enabled = bool(body_obj.get("store", True))

        # A5. Persist the skeleton row (store=true only).  The request is
        # redacted by the resource layer on the write path; here we store the
        # raw sanitized payload because reasoning text never reached the store
        # in this path (the sanitizer does not materialise it either).  A store
        # hiccup degrades the request to store-less, it never fails it.
        chain_depth = resolution.depth + 1 if resolution is not None and resolution.ok else 0

        rs = self._v3_response_store()
        if store_enabled and persist_skeleton and rs is not None:
            from ..proxy.protocol.item_registry import parse_input_items, serialize_item

            input_items = [serialize_item(it) for it in parse_input_items(body_obj.get("input"))]
            stored_request = dict(body_obj)
            if "input" in stored_request:
                stored_request["input"] = input_items
            try:
                await rs.create_response(
                    response_id=response_id,
                    workspace_id=workspace_id,
                    model=str(body_obj.get("model", "") or ""),
                    status="in_progress",
                    previous_response_id=previous_response_id,
                    background=bool(body_obj.get("background", False)),
                    request=stored_request,
                )
            except Exception:
                _lg.exception(f"[v3] skeleton persist failed for {response_id}")
                rs = None  # do not fatal the request over a store hiccup

        prep = _V3CreateContext(
            body_obj=body_obj,
            upstream_body=upstream_body,
            final_body=json.dumps(upstream_body, ensure_ascii=False).encode(),
            sanitized=sanitized,
            decision=decision,
            response_id=response_id,
            workspace_id=workspace_id,
            previous_response_id=previous_response_id,
            chain_depth=chain_depth,
            store_enabled=store_enabled,
            rs=rs,
        )
        if persist_skeleton:
            await self._persist_v3_create_side_records(prep)
        return prep, None

    async def _persist_v3_create_side_records(self, prep: "_V3CreateContext") -> None:
        """Write this turn's input items and state-chain row.

        Split out of the skeleton write because the *row* has two possible
        owners (this handler for sync creates, ``BackgroundWorker.enqueue`` for
        background ones) while these two side records always belong to the same
        writer -- the one that knows the resolved chain depth.  They must run
        **after** the row exists, hence the separate call site.

        A store hiccup degrades the request (no ``/input_items`` listing) but
        never fails it: the client already has a valid response either way.
        """
        rs = prep.rs
        if not prep.store_enabled or rs is None:
            return
        from ..proxy.protocol.item_registry import parse_input_items, serialize_item

        try:
            input_items = [serialize_item(it) for it in parse_input_items(prep.body_obj.get("input"))]
            if input_items:
                await rs.save_input_items(prep.response_id, input_items)
            if prep.chain_depth > 0:
                # Reuse the walk done by A1: resolving the same chain twice
                # doubles the store reads and can only ever agree with itself.
                await rs.save_state_chain(
                    prep.response_id,
                    prep.previous_response_id,
                    prep.chain_depth,
                    workspace_id=prep.workspace_id,
                )
        except Exception:
            _lg.exception(f"[v3] input/chain persist failed for {prep.response_id}")

    async def _dispatch_v3_create(
        self,
        request: web.Request,
        ctx,
        candidates: list[KeyHealth],
    ) -> web.Response:
        """Execute one real non-stream v3 create against an upstream (T26)."""
        prep, error = await self._prepare_v3_create(request, ctx, candidates)
        if error is not None:
            return error
        assert prep is not None  # narrow for type checkers: error is None

        resp, payload_bytes = await self._run_v3_nonstream(
            request=request,
            body_obj=prep.upstream_body,
            final_body=prep.final_body,
            decision=prep.decision,
            requested_model=ctx.requested_model or "",
            inbound_protocol="responses",
            session_key=self._session_key(request, prep.body_obj),
            required_caps=capability_values(prep.sanitized),
        )
        if resp.status_code >= 400:
            return web.Response(
                status=resp.status_code,
                body=payload_bytes,
                content_type="application/json",
            )
        # Unify the response id: upstream (native or translated) may return
        # its own; the stored resource must be retrievable under the id we
        # minted at the fork.
        try:
            resp_obj = json.loads(payload_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            resp_obj = {}
        if not isinstance(resp_obj, dict):
            # An upstream that answered 200 with a non-object body is passed
            # through untouched rather than laundered into a fabricated
            # response object (R-P0-32: never invent success shape).
            return web.Response(status=200, body=payload_bytes, content_type="application/json")

        resp_obj["id"] = prep.response_id
        # The translated body is a plain Chat->Responses conversion; it
        # carries no chain/background state.  Echo the official fields
        # the client sent so the returned object round-trips with a
        # later retrieve() (T37 criterion ②).
        if prep.previous_response_id:
            resp_obj["previous_response_id"] = prep.previous_response_id
        if prep.body_obj.get("background"):
            resp_obj["background"] = True
        if "store" not in resp_obj:
            resp_obj["store"] = prep.store_enabled
        if prep.store_enabled and prep.rs is not None:
            usage = resp_obj.get("usage") if isinstance(resp_obj.get("usage"), dict) else {}
            output = resp_obj.get("output") if isinstance(resp_obj.get("output"), list) else []
            await self._persist_v3_terminal(
                response_id=prep.response_id,
                workspace_id=prep.workspace_id,
                status="completed",
                usage=usage,
                output=output,
            )
        return web.json_response(resp_obj, status=200)

    # ------------------------------------------------------------------
    # P0-1: the real streaming create.
    #
    # Two-phase commit (§9.2) is the whole design:
    #
    #   Phase A -- 0 bytes written, any failure is a normal JSON error;
    #   Phase B -- after ``prepare()`` the status code is locked to 200 and
    #              the ONLY legal way to end is an SSE terminal event +
    #              ``[DONE]``, both produced by ``ResponsePipeline`` (§9.1).
    #
    # This handler never writes an ``event:`` or ``data:`` line of its own.
    # That is not a style preference: single ownership is the structural
    # reason a lifecycle event cannot appear twice (AC-1.4).
    # ------------------------------------------------------------------

    async def _dispatch_v3_create_stream(
        self,
        request: web.Request,
        ctx,
        candidates: list[KeyHealth],
    ) -> web.StreamResponse:
        """Serve ``POST /v1/responses`` with ``stream=true`` as a real SSE stream."""
        prep, error = await self._prepare_v3_create(request, ctx, candidates)
        if error is not None:
            return error
        assert prep is not None

        # A6. Key / translation / headers / body -- still no network I/O.
        call, call_error = await self._prepare_v3_upstream_call(
            request=request,
            body_obj=prep.upstream_body,
            final_body=prep.final_body,
            decision=prep.decision,
            requested_model=ctx.requested_model or "",
            inbound_protocol="responses",
            stream=True,
        )
        if call_error is not None:
            await self._persist_v3_stream_terminal(prep, "failed", TerminalReason.UPSTREAM_ERROR.value)
            return web.Response(
                status=call_error.status_code,
                body=call_error.body,
                content_type="application/json",
            )
        assert call is not None
        key = call.key

        # A7. Open the upstream and read its response header.  ``UpstreamClient
        # .stream`` is an async generator that yields exactly one response
        # inside ``async with client.stream(...)``, so pulling the first item
        # by hand is what lets us inspect the status *before* committing to a
        # 200 -- and ``aclose()`` is what later exits that context manager.
        upstream_gen = call.client.stream(
            call.method,
            call.path,
            headers=call.headers,
            content=call.body,
        )
        try:
            upstream_resp = await upstream_gen.__anext__()
        except StopAsyncIteration:
            await self._aclose_quietly(upstream_gen)
            mark_network_failure(key)
            await self._persist_v3_stream_terminal(prep, "failed", TerminalReason.UPSTREAM_CONNECT.value)
            return web.json_response({"error": "upstream connection failed"}, status=502)
        except (ConnectionResetError, ConnectionError, OSError) as exc:
            await self._aclose_quietly(upstream_gen)
            transport = request.transport
            if transport is not None and transport.is_closing():
                # The client hung up while we were connecting: not the key's
                # fault, so its health is left untouched (R-P1-25).
                return web.Response(status=499, text="Client Closed Request")
            _lg.error(f"[v3-stream] key_id={key.key_id} connection error: {type(exc).__name__}: {exc}")
            mark_network_failure(key)
            await self._persist_v3_stream_terminal(prep, "failed", TerminalReason.UPSTREAM_CONNECT.value)
            return web.json_response({"error": "upstream connection failed"}, status=502)
        except Exception as exc:  # noqa: BLE001 - attribute, never leak a traceback
            await self._aclose_quietly(upstream_gen)
            _lg.error(f"[v3-stream] key_id={key.key_id} exception: {type(exc).__name__}: {exc}")
            mark_network_failure(key)
            await self._persist_v3_stream_terminal(prep, "failed", TerminalReason.UPSTREAM_ERROR.value)
            return web.json_response({"error": "upstream request failed"}, status=502)

        upstream_headers = dict(upstream_resp.headers)
        if upstream_resp.status_code >= 400:
            # An upstream error before the first byte is a normal HTTP error --
            # turning it into "200 + response.failed" would blind every SDK's
            # error path (the rejected alternative in §1.4).
            try:
                error_body = await upstream_resp.aread()
            except Exception:  # noqa: BLE001 - the status is the useful part
                error_body = b""
            await self._aclose_quietly(upstream_gen)
            if classify_failure(key, upstream_resp.status_code, upstream_headers):
                # Retryable upstream states must not park the key: the next
                # request gets to try it again (same rule as the non-stream path).
                mark_success(key)
            _lg.info(
                f"[v3-stream] key_id={key.key_id} upstream status={upstream_resp.status_code} body={error_body[:300]!r}"
            )
            await self._persist_v3_stream_terminal(prep, "failed", TerminalReason.UPSTREAM_ERROR.value)
            return web.Response(
                status=upstream_resp.status_code,
                body=error_body,
                content_type="application/json",
            )

        mark_success(key)
        learn_rate_limits(key, upstream_headers, upstream_resp.status_code)
        session_key = self._session_key(request, prep.body_obj)
        if session_key:
            required_caps = capability_values(prep.sanitized)
            self._set_sticky(session_key, key.key_id, required_caps)
            asyncio.create_task(self._persist_sticky_binding(session_key, key.key_id, required_caps))

        # A8. ---- Phase A is over: from here the status code is 200. ----
        resp = web.StreamResponse(status=200)
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        adapter = UpstreamSSEChunkAdapter.for_protocol(
            call.outbound_protocol,
            native=not call.need_translation,
        )
        pipeline = ResponsePipeline(
            prep.response_id,
            workspace_id=prep.workspace_id,
            store=prep.rs if prep.store_enabled else None,
        )
        cancelled = asyncio.Event()
        client_gone = False

        # §9.6: hold the generator so ``aclose()`` can be awaited.  Iterating
        # ``pipeline.run(...)`` anonymously leaves the cleanup to the garbage
        # collector, which means the producer tasks and the httpx connection
        # are released at an unpredictable time -- i.e. leaked under load.
        frames = pipeline.run(
            adapter.iter_chunks(upstream_resp.aiter_bytes()),
            client_cancelled=cancelled,
            key_health=key,  # read-only: a disconnect is not a key failure
            config=self._v3_pipeline_config(),  # AC-7.4
        )
        try:
            async for frame in frames:
                try:
                    await resp.write(frame)
                except (ConnectionResetError, ConnectionError, OSError):
                    # The client left.  That is not an error and above all not
                    # the key's fault -- no ``mark_network_failure`` (R-P1-25).
                    client_gone = True
                    cancelled.set()
                    break
        finally:
            await self._aclose_quietly(frames)
            await self._aclose_quietly(upstream_gen)

        if client_gone:
            pipeline.stats.client_disconnects += 1
            await self._persist_v3_stream_terminal(
                prep,
                "incomplete",
                TerminalReason.CANCELLED_BY_CLIENT.value,
                output=pipeline.output_items(),
            )
            return resp

        status = pipeline.state if pipeline.state in ("completed", "failed", "incomplete") else "incomplete"
        await self._persist_v3_stream_terminal(
            prep,
            status,
            pipeline.stats.terminal_reason or TerminalReason.NORMAL_FINISH.value,
            output=pipeline.output_items(),
        )
        try:
            await resp.write_eof()
        except (ConnectionResetError, ConnectionError, OSError):
            pass
        return resp

    async def _dispatch_v3_create_background(
        self,
        request: web.Request,
        ctx,
        candidates: list[KeyHealth],
    ) -> web.Response:
        """Serve ``POST /v1/responses`` with ``background=true`` (P0-6).

        The response is enqueued and returned as ``202 queued`` without a single
        upstream byte, which is what makes R-P1-34 ① ("an id in under a second")
        a property of the code rather than a hope.  Execution happens later in
        :class:`~zhongzhuan.responses_v3.background.BackgroundWorker`.
        """
        body_obj = dict(ctx.body or {})
        if body_obj.get("stream"):
            # U2 / GA v1: a detached job has no socket to stream over, and
            # pretending otherwise (opening an SSE stream that ends at the
            # first heartbeat) is worse than a clear rejection.
            return web.json_response(
                {
                    "error": {
                        "message": "background=true cannot be combined with stream=true",
                        "type": "invalid_request_error",
                        "param": "stream",
                        "code": "unsupported_parameter",
                    }
                },
                status=400,
            )

        worker = self._v3_background_worker()
        if worker is None:
            return web.json_response(
                {
                    "error": {
                        "message": "background responses are not available on this deployment",
                        "type": "invalid_request_error",
                        "code": "background_unavailable",
                    }
                },
                status=503,
            )

        # Phase A of a create still applies: a broken chain or a missing tool
        # executor must be rejected now, not discovered by a worker minutes
        # later with nobody listening.  The skeleton row is NOT written here --
        # ``worker.enqueue`` owns it (see ``persist_skeleton``).
        prep, error = await self._prepare_v3_create(
            request,
            ctx,
            candidates,
            persist_skeleton=False,
        )
        if error is not None:
            return error
        assert prep is not None

        try:
            record = await worker.enqueue(
                response_id=prep.response_id,
                workspace_id=prep.workspace_id,
                model=str(prep.body_obj.get("model", "") or ""),
                request=prep.upstream_body,
                previous_response_id=prep.previous_response_id,
            )
        except Exception:
            _lg.exception(f"[v3] background enqueue failed for {prep.response_id}")
            return web.json_response(
                {
                    "error": {
                        "message": "failed to enqueue background response",
                        "type": "server_error",
                        "code": "internal_server_error",
                    }
                },
                status=500,
            )

        # The row exists now, so this turn's input items / chain row can be
        # attached to it (the worker only writes the row, event and job).
        await self._persist_v3_create_side_records(prep)

        from ..responses_v3.schema import to_response_object

        payload = to_response_object(record) if record is not None else {"id": prep.response_id, "status": "queued"}
        return web.json_response(payload, status=202)

    def _v3_pipeline_config(self) -> PipelineConfig:
        """AC-7.4 + P0-2: the effective pipeline policy, from the unified config.

        Carries both the timeout ceilings and ``strict_terminal``.  The latter
        matters because :class:`PipelineConfig` defaults it to ``False`` for
        backwards compatibility (R-P1-22), which would let a truncated upstream
        be reported as ``response.completed``.  GA states its own policy in
        ``responses_bridge.strict_terminal`` (default ``True``), so the wire
        never whitewashes a terminal state — 铁律 2.

        An **absent** ``responses_bridge`` section must not weaken the
        contract.  ``PipelineConfig.from_config(None)`` legitimately yields the
        library defaults, and the library default of ``strict_terminal`` is
        ``False`` — so falling back to it would mean "the operator wrote no
        config, therefore truncated streams may be reported as
        ``response.completed``".  Missing configuration is exactly the case
        where the safe reading has to win, so the fallback is a default
        :class:`ResponsesBridgeConfig` (GA policy: strict) rather than no
        config at all.  Explicitly configured values still win over it.

        Built once and cached — the values cannot change without a restart,
        and rebuilding it per request would put a dataclass construction on
        the hot path for no benefit.
        """
        if self._v3_pipeline_cfg is None:
            from ..config.config import ResponsesBridgeConfig

            bridge = getattr(self._feature_flags, "bridge_config", None)
            if bridge is None:
                bridge = ResponsesBridgeConfig()
            self._v3_pipeline_cfg = PipelineConfig.from_config(bridge)
        return self._v3_pipeline_cfg

    @staticmethod
    async def _aclose_quietly(agen: Any) -> None:
        """``aclose()`` an async generator without ever raising at teardown."""
        closer = getattr(agen, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
            pass

    async def _persist_v3_stream_terminal(
        self,
        prep: "_V3CreateContext",
        status: str,
        terminal_reason: str,
        *,
        output: list[Any] | None = None,
    ) -> None:
        """Move the streamed response's row to its terminal state (store=true)."""
        if not prep.store_enabled or prep.rs is None:
            return
        await self._persist_v3_terminal(
            response_id=prep.response_id,
            workspace_id=prep.workspace_id,
            status=status,
            output=output or [],
            terminal_reason=terminal_reason,
        )

    @staticmethod
    def _sanitize_system_content_value(content) -> Any:
        """清洗 system/developer 消息内容中的外来客户端标识（兼容两种形态）。

        OpenAI 的 ``content`` 有两种合法形态：
        * 纯字符串（简单请求 / 老客户端）；
        * 内容块数组（Codex 真实请求：``[{"type": "text", "text": ...}]``）。

        两种都必须清洗：上游（freemodel.dev 等 WorkBuddy-only）扫描的是整个
        请求体的 system/developer 消息，content 块数组里的 ``codex`` 同样会
        触发 403 unsupported_client（2026-08-06 实测 Codex 真实请求体 item[1]
        即此形态，且 role 为 developer 而非 system）。
        """
        from .client_presets import sanitize_system_content

        if isinstance(content, str):
            return sanitize_system_content(content)
        if isinstance(content, list):
            changed = False
            out: list = []
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    cleaned = sanitize_system_content(block["text"])
                    if cleaned != block["text"]:
                        nb = dict(block)
                        nb["text"] = cleaned
                        out.append(nb)
                        changed = True
                    else:
                        out.append(block)
                else:
                    out.append(block)
            return out if changed else content
        return content

    @staticmethod
    def _inject_system_message(final_body: bytes, key, upstream_model: str | None = None) -> bytes:
        """按预设的 ``require_system`` 标记，确保请求体第一条 system 消息是客户端特征内容。

        部分上游（如 freemodel.dev）通过请求体中的 system 消息识别客户端来源
        （WorkBuddy 请求必带系统提示词）。上游校验的是**特征内容**：
        * 请求体完全没有 system → 补一条（历史行为）；
        * 请求体有 system 但**第一条不是客户端特征**（如 Trae 自带
          "powered by TRAE"）→ 上游识别出不是目标客户端 → 403 unsupported_client。
          此时在**最前面插入**一条客户端特征 system，把原 system 挤到后面
          （原指令仍生效，但上游看到的第一条 system 是目标客户端特征）。
        * 第一条 system 已是特征内容 → 原样返回（零影响）。

        返回新 bytes；不满足条件时原样返回 ``final_body``（零影响）。
        """
        preset_name = getattr(key, "client_preset", "") or ""
        if not preset_name:
            return final_body
        try:
            from .client_presets import (
                needs_system_message,
                get_fingerprint_system_prefix,
            )

            if not needs_system_message(preset_name):
                return final_body
            body_obj = json.loads(final_body)
            if not isinstance(body_obj, dict):
                return final_body
            messages = body_obj.get("messages")
            if not isinstance(messages, list):
                return final_body
            model_name = upstream_model or body_obj.get("model") or ""
            prefix = get_fingerprint_system_prefix(preset_name, model_name)
            # 第一条消息是 system 且已是特征内容 → 不重复注入（零影响）
            first = messages[0] if messages else None
            if (
                isinstance(first, dict)
                and first.get("role") == "system"
                and prefix
                and str(first.get("content", "")).strip().startswith(prefix.strip())
            ):
                # 特征 system 已置顶，但后续 system（通常是客户端自带的长
                # instructions）里可能含外来客户端标识（如 codex），上游会
                # 据此判定为外来客户端 → 403。对后续所有 system 做中性化
                # 清洗，其余消息（user/assistant/tool）不受影响。
                # content 可能是纯字符串或内容块数组，两者都清洗。
                for msg in messages[1:]:
                    if isinstance(msg, dict) and msg.get("role") in _INSTRUCTION_ROLES:
                        cleaned = ProxyHandler._sanitize_system_content_value(msg.get("content"))
                        if cleaned != msg.get("content"):
                            msg["content"] = cleaned
                return json.dumps(body_obj, ensure_ascii=False).encode()
            # 缺 system 或第一条不是特征 system → 在最前面插入特征 system，
            # 同时把请求体里**所有** system（外来 instructions，可能排在 user
            # 后面，如 Codex 把 system prompt 放在 input 数组靠后位置）中性化，
            # 避免插入后仍有 system 携带外来标识而被上游拒绝。
            for msg in messages:
                if isinstance(msg, dict) and msg.get("role") in _INSTRUCTION_ROLES:
                    cleaned = ProxyHandler._sanitize_system_content_value(msg.get("content"))
                    if cleaned != msg.get("content"):
                        msg["content"] = cleaned
            messages.insert(0, {"role": "system", "content": prefix})
            return json.dumps(body_obj, ensure_ascii=False).encode()
        except (json.JSONDecodeError, TypeError, ValueError):
            return final_body

    def _apply_client_fingerprint(self, headers: dict, key) -> dict:
        """根据 ``key.client_preset`` 注入上游客户端指纹头（v009）。

        * ``client_preset == ""``   → 不模拟, 直接返回, headers 零修改（默认零影响）        * ``client_preset == "workbuddy"``（或其他内置预设）→ 注入 PRESETS 内置头
        * ``client_preset == "custom"`` → 注入 key.custom_headers（加载链已解析）

        在 Authorization 注入之后调用, 预设/自定义头若含 Authorization 会覆盖
        P0 的内置预设不含受控头; 自定义头的受控头黑名单在 API 层拦截, 加载链
        不重复校验以保性能。
        """
        preset_name = getattr(key, "client_preset", "") or ""
        if not preset_name:
            return headers

        if preset_name == "custom":
            headers_list = getattr(key, "custom_headers", None) or []
        else:
            from .client_presets import get_headers

            headers_list = get_headers(preset_name)

        if not headers_list:
            return headers

        from .header_templates import render

        for name, value_tpl in headers_list:
            if name:  # 防御：跳过空 name
                headers[name] = render(value_tpl)
        return headers

    async def _prepare_v3_upstream_call(
        self,
        *,
        request: web.Request | _BackgroundRequest,
        body_obj: dict,
        final_body: bytes,
        decision,
        requested_model: str,
        inbound_protocol: str,
        stream: bool,
    ) -> tuple["_V3UpstreamCall | None", "_V3UpstreamResult | None"]:
        """Resolve everything needed to *issue* one v3 upstream request.

        This is the two-phase-commit boundary of the streaming path (Q1): every
        decision that can still fail -- rate window, client construction,
        request translation, auth headers, path override -- happens here, i.e.
        **before** the first SSE byte is written.  Once this returns a call the
        stream handler may safely ``prepare()`` a ``200`` response, because the
        only remaining failure modes are mid-stream ones the pipeline already
        renders as terminal events.

        It performs no network I/O and mutates nothing but the key's request
        counter, so the non-stream and stream paths can share it verbatim.

        Returns:
            ``(call, None)`` on success, or ``(None, error)`` where ``error``
            carries the status + JSON body to return to the client.
        """
        key = decision.key
        # The capability router already picked a candidate; honour its
        # NATIVE/TRANSLATE path decision instead of re-deriving from the body.
        upstream_path = getattr(decision, "upstream_path", "") or ""
        if not upstream_path:
            upstream_path = "/v1/responses" if getattr(decision, "is_native", False) else "/v1/chat/completions"

        if key.window is not None and not key.window.allow(1):
            return None, _http_json(429, {"error": "all keys exhausted"})
        key.record_request()

        client = await self._ensure_client(key.upstream_base)
        if client is None:
            return None, _http_json(503, {"error": "no enabled keys"})

        outbound_protocol = key.upstream_protocol
        need_translation = inbound_protocol != outbound_protocol
        headers: dict[str, str] = {}
        for hk, hv in request.headers.items():
            kl = hk.lower()
            if kl not in (
                "host",
                "connection",
                "transfer-encoding",
                "content-length",
                "content-encoding",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailer",
                "upgrade",
                "x-forwarded-for",
                "x-forwarded-proto",
            ):
                headers[hk] = hv

        if need_translation:
            try:
                cc = convert_responses_request_to_chatcompletions(body_obj)
            except Exception as exc:
                _lg.exception(f"[v3] translate request failed: {exc}")
                return None, _http_json(500, {"error": "request translation failed"})
            if outbound_protocol == "anthropic":
                translated_req = translate_request_o2a(cc, key.anthropic_version)
                upstream_path = "/v1/messages"
            else:
                translated_req = cc
                upstream_path = "/v1/chat/completions"
            if isinstance(translated_req, dict):
                # The outbound stream flag is ours to set: a v3 stream must ask
                # the upstream to stream, a v3 non-stream must not.
                if stream:
                    translated_req["stream"] = True
                else:
                    translated_req.pop("stream", None)
                if key.upstream_model:
                    translated_req["model"] = key.upstream_model
            final_body = json.dumps(translated_req, ensure_ascii=False).encode()
            if outbound_protocol == "anthropic":
                headers["x-api-key"] = key.api_key
                headers["anthropic-version"] = key.anthropic_version
                headers.pop("Authorization", None)
            else:
                headers["Authorization"] = f"Bearer {key.api_key}"
                headers.pop("x-api-key", None)
                headers.pop("anthropic-version", None)
            headers["Content-Length"] = str(len(final_body))
        else:
            # NATIVE passthrough: only auth, model mapping and the stream flag
            # are touched.
            native_body = dict(body_obj)
            mutated = False
            if requested_model and key.upstream_model and requested_model != key.upstream_model:
                native_body["model"] = key.upstream_model
                mutated = True
            if bool(native_body.get("stream", False)) != stream:
                native_body["stream"] = stream
                mutated = True
            if mutated:
                final_body = json.dumps(native_body, ensure_ascii=False).encode()
            headers["Authorization"] = f"Bearer {key.api_key}"
            headers.pop("x-api-key", None)
            headers.pop("anthropic-version", None)
            headers["Content-Length"] = str(len(final_body))

        if stream:
            # Never let a transport-level content codec sit between the
            # upstream and the SSE framer.
            headers["Accept-Encoding"] = "identity"

        # 客户端指纹模拟（v009）：在 Authorization 注入后、path 处理前注入预设/自定义头
        self._apply_client_fingerprint(headers, key)
        # require_system 预设（如 workbuddy）：请求体缺 system 消息时补一条
        injected = self._inject_system_message(final_body, key, getattr(key, "upstream_model", None))
        if injected is not final_body:
            final_body = injected
            headers["Content-Length"] = str(len(final_body))

        if key.upstream_path_override:
            upstream_path = key.upstream_path_override

        return (
            _V3UpstreamCall(
                key=key,
                client=client,
                method=request.method,
                path=upstream_path,
                headers=headers,
                body=final_body,
                outbound_protocol=outbound_protocol,
                need_translation=need_translation,
            ),
            None,
        )

    async def _run_v3_nonstream(
        self,
        *,
        request: web.Request,
        body_obj: dict,
        final_body: bytes,
        decision,
        requested_model: str,
        inbound_protocol: str,
        session_key: str,
        required_caps: frozenset[str],
    ) -> tuple[Any, bytes]:
        """Execute one non-stream create against the production upstream chain.

        Reuses the scheduler pick (``decision.key``), health accounting, retry
        classification and the existing Responses -> Chat/Anthropic translators.
        Returns ``(httpx.Response, payload_bytes)`` so the caller can inspect
        the status and unify the response id before sending to the client.
        """
        call, error = await self._prepare_v3_upstream_call(
            request=request,
            body_obj=body_obj,
            final_body=final_body,
            decision=decision,
            requested_model=requested_model,
            inbound_protocol=inbound_protocol,
            stream=False,
        )
        if error is not None:
            return error, b""
        assert call is not None  # narrow for type checkers: error is None
        key = call.key
        client = call.client
        headers = call.headers
        final_body = call.body
        upstream_path = call.path
        outbound_protocol = call.outbound_protocol
        need_translation = call.need_translation

        try:
            resp = await client.request(
                request.method,
                upstream_path,
                headers=headers,
                content=final_body,
            )
        except (ConnectionResetError, ConnectionError, OSError) as exc:
            transport = request.transport
            if transport is not None and transport.is_closing():
                return _http_json(499, b"Client Closed Request"), b""
            _lg.error(f"[v3] key_id={key.key_id} connection error: {type(exc).__name__}: {exc}")
            mark_network_failure(key)
            return _http_json(502, {"error": "upstream connection failed"}), b""
        except Exception as exc:
            _lg.error(f"[v3] key_id={key.key_id} exception: {type(exc).__name__}: {exc}")
            mark_network_failure(key)
            return _http_json(502, {"error": "upstream request failed"}), b""

        data = await resp.aread()
        resp_headers = dict(resp.headers)
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers.pop("content-length", None)
        content_encoding = resp_headers.get("content-encoding", "").lower()
        if "gzip" in content_encoding:
            import gzip

            try:
                data = gzip.decompress(data)
            except Exception:
                pass
            resp_headers.pop("content-encoding", None)

        if resp.status_code >= 400:
            should_retry = classify_failure(key, resp.status_code, resp_headers)
            _lg.info(
                f"[v3] key_id={key.key_id} upstream status={resp.status_code} retry={should_retry} body={data[:300]!r}"
            )
            if should_retry:
                # Single-shot for now: the full scheduler retry loop is the
                # T28 pass.  Keep the key healthy for the next request.
                mark_success(key)
            result = _V3UpstreamResult(resp.status_code, data)
            return result, data

        mark_success(key)
        learn_rate_limits(key, resp_headers, resp.status_code)

        # Record token usage for TPM tracking + quota.
        try:
            _resp_obj = json.loads(data.decode("utf-8"))
            _usage = _resp_obj.get("usage") if isinstance(_resp_obj, dict) else None
            if isinstance(_usage, dict):
                _tokens_in = int(_usage.get("prompt_tokens") or _usage.get("input_tokens") or 0)
                _tokens_out = int(_usage.get("completion_tokens") or _usage.get("output_tokens") or 0)
                key.record_tokens(_tokens_in, _tokens_out)
        except (ValueError, TypeError):
            pass

        # Sticky binding: remember which key served this conversation.
        if session_key:
            self._set_sticky(session_key, key.key_id, required_caps)
            asyncio.create_task(
                self._persist_sticky_binding(
                    session_key,
                    key.key_id,
                    required_caps,
                )
            )

        # Translate the response body if needed (Chat/Anthropic -> Responses).
        if need_translation:
            try:
                resp_data = json.loads(data.decode("utf-8"))
                cc_resp = (
                    translate_response_a2o(resp_data, requested_model)
                    if outbound_protocol == "anthropic"
                    else resp_data
                )
                translated_resp = chatcompletions_to_responses(cc_resp, requested_model)
                data = json.dumps(translated_resp, ensure_ascii=False).encode()
            except (json.JSONDecodeError, ValueError) as exc:
                _lg.warning(f"[v3] failed to translate response: {exc}, returning raw")

        return _V3UpstreamResult(resp.status_code, data), data

    def _filter_v3_candidates(
        self,
        candidates: list[KeyHealth],
        ctx,
    ) -> list[KeyHealth]:
        """Apply the key rollout to candidate keys (R-P0-25).

        With no feature flags wired, every candidate passes (backwards
        compatible).  With flags, only keys that ``v3_key_allowed`` pass;
        an empty ``keys`` rollout means "allow all" (per the config schema).
        """
        if self._feature_flags is None or not candidates:
            return candidates
        allowed = []
        for k in candidates:
            if self._feature_flags.v3_key_allowed(k.key_id):
                allowed.append(k)
        return allowed

    # ------------------------------------------------------------------
    # Sticky session helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stable_fingerprint(body_obj: dict | None) -> str:
        """首轮稳定指纹（T35 / R-P1-61）。

        R-P0-14 / R-P1-61 要求：**不用滚动消息尾部**做指纹 —— 每一轮
        ``messages[-3:]`` 的内容都在变，同一会话每轮 hash 都不同，粘性路由
        永远落不到同一个 key。改用**会话第一条 user 消息**的归一化指纹
        （sha256 前 16 位）：只要会话的第一条 user 消息不变，后续无论追加多少
        轮次，指纹都恒定。

        **reasoning 内容绝不参与指纹计算**（R-P0-14）：消息对象里的
        ``reasoning`` 字段、以及 ``role=reasoning`` 的输入项一律跳过，只取
        首条 ``role=user`` 的文本内容。

        Returns:
            ``"fp:<hex>"`` 形式的指纹；无法提取到首条 user 消息时返回 ``""``。
        """
        msgs = body_obj.get("messages") if body_obj else None
        if msgs is None and body_obj:
            # Responses API (Codex) 把对话放在 `input`（OpenAI messages 数组形态）。
            msgs = body_obj.get("input")
        if not isinstance(msgs, (list, tuple)):
            return ""
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            item_type = str(msg.get("type") or "")
            if role == "reasoning" or item_type == "reasoning":
                # R-P0-14：reasoning 项绝不进入指纹（Responses 项可能只有
                # ``type: "reasoning"`` 而没有 ``role``）。
                continue
            if role != "user":
                continue
            content = msg.get("content")
            if content is None:
                continue
            text = (
                content
                if isinstance(content, str)
                else json.dumps(
                    content,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            text = text.strip()
            if not text:
                continue
            import hashlib

            return "fp:" + hashlib.sha256(text.encode()).hexdigest()[:16]
        return ""

    @staticmethod
    def _session_key(request: web.Request, body_obj: dict | None) -> str:
        """Extract a conversation fingerprint for sticky routing (T35).

        Priority (R-P1-61):
        1. Explicit header: x-session-id / x-zhongzhuan-session / x-request-id
        2. Explicit body id: ``conversation`` (Responses API) /
           ``previous_response_id`` (Responses chain)
        3. First-turn stable fingerprint (first user message, reasoning
           excluded) instead of the rolling message tail.

        Returns "" if no stable identifier can be derived.
        """
        for h in ("x-session-id", "x-zhongzhuan-session", "x-request-id"):
            v = request.headers.get(h)
            if v:
                return f"hdr:{v}"
        if body_obj:
            for field in ("conversation", "previous_response_id"):
                v = body_obj.get(field)
                if isinstance(v, str) and v.strip():
                    return f"id:{v.strip()}"
        return ProxyHandler._stable_fingerprint(body_obj)

    @staticmethod
    def _required_capabilities(body_obj: dict | None) -> frozenset[str]:
        """Compatibility adapter over the shared v3 request fact model.

        T35 callers persist string values, while capability routing consumes the
        enum values in :class:`SanitizedRequest`. Both now come from the same
        sanitizer, including ``previous_response_id`` as a stateful requirement.
        """
        return capability_values(RequestSanitizer().sanitize(body_obj))

    def _get_sticky_key(
        self,
        session_key: str,
        candidates: list[KeyHealth],
        body_obj: dict | None = None,
        required_caps: frozenset[str] | None = None,
    ) -> KeyHealth | None:
        """Return the sticky key for this session if still valid, healthy and
        capability-compatible (T35 / R-P1-60 判据⑥).

        Checks in order:
        1. TTL not expired (clock is injectable via ``self._now``).
        2. The bound key is still in ``candidates`` **and** ``is_available()``.
        3. **Capability compatibility** (新增，判据⑥): the capabilities
           recorded at bind time must be a superset of what this request needs.
           A mismatch invalidates the binding, records the failover reason and
           returns ``None`` so the scheduler routes elsewhere.
        """
        entry = self._sticky.get(session_key)
        if entry is None:
            return None
        key_id, expire_at = entry
        if self._now() > expire_at:
            self._sticky.pop(session_key, None)
            self._sticky_caps.pop(session_key, None)
            return None
        for k in candidates:
            if k.key_id == key_id and k.is_available():
                # 判据⑥：能力兼容校验 —— sticky 只在选定模型健康**且**能力兼容时生效。
                bound_caps = self._sticky_caps.get(session_key, frozenset())
                required = required_caps
                if required is None:
                    required = self._required_capabilities(body_obj)
                if required and bound_caps and not (required <= bound_caps):
                    reason = f"capability mismatch: required={sorted(required)} bound={sorted(bound_caps)}"
                    self._binding_failover_reasons[session_key] = reason
                    return None
                return k
        return None

    def _set_sticky(self, session_key: str, key_id: int, caps: frozenset[str] | None = None) -> None:
        """Record a successful key for this session."""
        if session_key:
            self._sticky[session_key] = (key_id, self._now() + self._sticky_ttl)
            if caps is not None:
                self._sticky_caps[session_key] = caps
            # Opportunistic cleanup: drop expired entries occasionally
            if len(self._sticky) > 256:
                now = self._now()
                self._sticky = {k: v for k, v in self._sticky.items() if v[1] > now}

    def _response_store(self):
        """Lazily build the ResponseStore used for session→route binding (T35)."""
        if self._rs is None and self.store is not None:
            from ..store.response_store import ResponseStore

            self._rs = ResponseStore(self.store)
        return self._rs

    async def _persist_sticky_failover(self, session_key: str) -> None:
        """Flush a recorded failover reason to the persisted binding (T35)."""
        reason = self._binding_failover_reasons.pop(session_key, "")
        rs = self._response_store()
        if rs is None or not reason:
            return
        try:
            await rs.record_binding_failover(session_key, reason=reason)
        except Exception:
            _lg.exception("record_binding_failover failed")

    async def _restore_sticky_from_store(self, session_key: str) -> None:
        """Restore an in-memory sticky entry from the persisted binding (T35).

        Called lazily on the first lookup of an unknown session when a store is
        present, so process restarts keep the sticky continuity promise of
        R-P1-61.  The binding's TTL is enforced by the store; an expired or
        foreign-workspace binding returns ``None`` and is simply ignored.
        """
        rs = self._response_store()
        if rs is None or not session_key or session_key in self._sticky:
            return
        try:
            rec = await rs.get_route_binding(session_key)
        except Exception:
            _lg.exception("get_route_binding failed")
            return
        if rec is None:
            return
        self._sticky[session_key] = (rec["key_id"], self._now() + self._sticky_ttl)
        caps = frozenset(str(c) for c in (rec.get("capabilities") or ()))
        if caps:
            self._sticky_caps[session_key] = caps

    async def _persist_sticky_binding(
        self,
        session_key: str,
        key_id: int,
        caps: frozenset[str],
    ) -> None:
        """Persist a session→route binding to the ResponseStore (T35 / R-P1-61)."""
        rs = self._response_store()
        if rs is None or not session_key:
            return
        try:
            await rs.upsert_route_binding(
                session_key=session_key,
                key_id=key_id,
                capabilities=caps,
                expires_at=int(self._now() + self._sticky_ttl),
            )
        except Exception:
            _lg.exception("upsert_route_binding failed")

    async def reload_keys(self) -> int:
        """Reload keys (and groups) from the store. Returns new key count.

        优化点3：reload 时重置 invalid 状态。从 DB 重新加载意味着 key 可能已修复，
        所以把 status 强制重置为 healthy（但保留学到的 rpm_limit/tpm_limit 限额）。
        """
        if self._load_keys_fn is not None:
            new_keys = await self._load_keys_fn()
            # 保留旧 keys 中学到的限额（learn_rate_limits 的成果）
            old_limits: dict[int, tuple[int, int]] = {}
            for ok in self._keys:
                if ok.key_id > 0:  # 跳过 env/dummy key (key_id=0)
                    old_limits[ok.key_id] = (ok.rpm_limit, ok.tpm_limit)
            # 对新加载的 keys：重置状态但恢复学到的限额
            for nk in new_keys:
                nk.status = STATE_HEALTHY
                nk.cooldown_until = 0.0
                nk.recent_429_count = 0
                if nk.key_id in old_limits:
                    old_rpm, old_tpm = old_limits[nk.key_id]
                    # 只保留更严格的限额（学到的比配置的更小才保留）
                    if old_rpm > 0 and (nk.rpm_limit == 0 or old_rpm < nk.rpm_limit):
                        nk.rpm_limit = old_rpm
                    if old_tpm > 0 and (nk.tpm_limit == 0 or old_tpm < nk.tpm_limit):
                        nk.tpm_limit = old_tpm
                        if nk.tpm_window is not None:
                            nk.tpm_window.limit = old_tpm
            self._keys = new_keys
        # Also reload groups so admin edits to groups take effect without restart
        if self.store is not None:
            try:
                from ..store.groups import list_groups as list_groups_db

                rows = await list_groups_db(self.store)
                self._set_groups(
                    [
                        {
                            "id": r.get("id"),
                            "name": r.get("name"),
                            "strategy": r.get("strategy"),
                            "members": [m["model_id"] for m in (r.get("members") or [])],
                        }
                        for r in rows
                    ]
                )
            except Exception:
                _lg.exception("reload groups failed")
        return len(self._keys)

    async def _ensure_client(self, upstream_base: str) -> UpstreamClient | None:
        if upstream_base in self._client_cache:
            return self._client_cache[upstream_base]
        # Lazy init under lock
        async with self._lock:
            if upstream_base in self._client_cache:
                return self._client_cache[upstream_base]
            client = UpstreamClient(base_url=upstream_base, timeout=self._timeout)
            try:
                await client.start()
            except Exception:
                _lg.exception(f"lazy-init upstream client failed for {upstream_base!r}")
                return None
            self._client_cache[upstream_base] = client
            return client

    # ---- 后台任务：sticky 清理 + 健康状态持久化（优化点4+5）----

    def _v3_background_worker(self) -> BackgroundWorker | None:
        """The process-wide v3 background worker, or ``None`` if unavailable.

        A worker needs a store (leases, jobs and the event log all live there)
        and a v3 handler; a store-less or v2-only deployment simply has no
        background support and says so with a 503 instead of accepting jobs it
        can never run.
        """
        if self._v3_worker is not None:
            return self._v3_worker
        if self._v3 is None:
            return None
        rs = self._v3_response_store()
        if rs is None:
            return None
        self._v3_worker = BackgroundWorker(rs)
        return self._v3_worker

    def _v3_background_upstream_factory(self, task_id: str) -> Any:
        """Build the execution source for one background job (P0-6).

        §9.8: this returns a **zero-argument callable**, never a started async
        generator.  ``BackgroundWorker._stream`` does ``source = upstream() if
        callable(upstream) else upstream`` precisely so a recovered attempt can
        obtain a *fresh* stream -- an async generator that already ran cannot
        be re-entered, and reusing one would make recovery a silent no-op.

        The stream it opens is the same one the live path uses: same key
        selection, same translation, same adapter vocabulary.  Only the
        consumer differs (budget ledger + event log instead of a socket).
        """

        async def _open() -> Any:
            worker = self._v3_background_worker()
            if worker is None:
                raise RuntimeError("background worker is unavailable")
            job = await worker.jobs.get_job_any_tenant(task_id) or {}
            response_id = str(job.get("response_id") or task_id)
            workspace_id = str(job.get("workspace_id") or "")
            record = await worker.store.get_response(response_id, workspace_id=workspace_id)
            if record is None:
                raise RuntimeError(f"background job {task_id} has no response row")

            body_obj = dict(record.request or {})
            # A detached job always streams from the upstream: it is the only
            # shape that lets the budget ledger charge per chunk and the cancel
            # flag be honoured mid-answer.
            body_obj["stream"] = True
            body_obj.pop("background", None)

            candidates = self._filter_v3_candidates(
                self._resolve_candidates(str(body_obj.get("model", "") or "")),
                None,
            )
            if not candidates:
                raise RuntimeError("no enabled keys for background job")
            sanitized = self._request_sanitizer.sanitize(body_obj)
            decision = self._capability_router(candidates).route(sanitized, candidates)
            if hasattr(decision, "to_response"):
                status, payload = decision.to_response()
                raise RuntimeError(f"capability route refused background job: {status} {payload}")

            call, error = await self._prepare_v3_upstream_call(
                request=_BackgroundRequest(),
                body_obj=body_obj,
                final_body=json.dumps(body_obj, ensure_ascii=False).encode(),
                decision=decision,
                requested_model=str(body_obj.get("model", "") or ""),
                inbound_protocol="responses",
                stream=True,
            )
            if error is not None:
                raise RuntimeError(f"background upstream unavailable: {error.status_code}")
            assert call is not None

            adapter = UpstreamSSEChunkAdapter.for_protocol(
                call.outbound_protocol,
                native=not call.need_translation,
            )
            async for upstream_resp in call.client.stream(
                call.method,
                call.path,
                headers=call.headers,
                content=call.body,
            ):
                if upstream_resp.status_code >= 400:
                    raise RuntimeError(f"background upstream status {upstream_resp.status_code}")
                async for chunk in adapter.iter_chunks(upstream_resp.aiter_bytes()):
                    yield chunk

        return _open

    async def start_background_tasks(self) -> None:
        """启动后台周期任务。应在 aiohttp app.on_startup 时调用。"""
        if self._bg_running:
            return
        self._bg_running = True
        self._bg_tasks.append(asyncio.create_task(self._sticky_cleanup_loop()))
        if self.store is not None:
            self._bg_tasks.append(asyncio.create_task(self._health_snapshot_loop()))
        # P0-6: the v3 background worker is part of this handler's lifecycle,
        # so a process that serves requests is by construction a process that
        # drains the background queue.
        worker = self._v3_background_worker()
        if worker is not None:
            self._bg_tasks.append(
                asyncio.create_task(worker.start(upstream_factory=self._v3_background_upstream_factory))
            )
            _lg.info("[v3] background worker started")
        _lg.info(f"started {len(self._bg_tasks)} background tasks")

    async def stop_background_tasks(self) -> None:
        """停止后台任务。应在 aiohttp app.on_cleanup 时调用。"""
        self._bg_running = False
        if self._v3_worker is not None:
            # Ask the poll loop to leave after the current job before the task
            # is cancelled: a cancel mid-``run_job`` would drop the lease
            # without a terminal state and leave the job for recovery.
            self._v3_worker.stop()
        for t in self._bg_tasks:
            t.cancel()
        for t in self._bg_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_tasks.clear()

    async def _sticky_cleanup_loop(self) -> None:
        """优化点5：每 5 分钟清理过期的 sticky session 条目。"""
        while self._bg_running:
            try:
                await asyncio.sleep(300)
                if not self._bg_running:
                    break
                now = time.time()
                before = len(self._sticky)
                self._sticky = {k: v for k, v in self._sticky.items() if v[1] > now}
                cleaned = before - len(self._sticky)
                if cleaned > 0:
                    _lg.debug(f"sticky cleanup: removed {cleaned} expired entries")
            except asyncio.CancelledError:
                break
            except Exception:
                _lg.exception("sticky cleanup loop error")
                await asyncio.sleep(60)

    async def _health_snapshot_loop(self) -> None:
        """优化点4：每 30 秒把 key 健康状态快照到 DB（重启后可恢复）。"""
        from ..store.key_health import save_health, KeyHealthRow

        while self._bg_running:
            try:
                await asyncio.sleep(30)
                if not self._bg_running or self.store is None:
                    break
                for k in self._keys:
                    if k.key_id <= 0:
                        continue  # 跳过 env/dummy key (key_id=0)
                    try:
                        await save_health(
                            self.store,
                            KeyHealthRow(
                                key_id=k.key_id,
                                status=k.status,
                                cooldown_until=k.cooldown_until,
                                rpm_limit=k.rpm_limit,
                                tpm_limit=k.tpm_limit,
                                success_count=k.success_count,
                                failure_count=k.total_failures,
                                recent_429_count=k.recent_429_count,
                            ),
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                _lg.exception("health snapshot loop error")
                await asyncio.sleep(60)

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        _request_start = time.time()
        store = self.store

        # -- Build request context once (T02): body is parsed exactly once here --
        ctx = await self._ctx_builder.build(request)
        path = ctx.path
        method = ctx.method
        body_obj = ctx.body
        sanitized_request = self._request_sanitizer.sanitize(body_obj)
        required_caps = capability_values(sanitized_request)
        inbound_protocol = ctx.inbound_protocol
        requested_model = ctx.requested_model
        remote = ctx.remote

        # Log the incoming request early (before processing)
        _lg.info(f"[REQ] {method} {path} remote={remote} content_length={ctx.content_length}")
        _lg.info(
            f"[{id(request):x}] processing {method} {path} "
            f"model={requested_model!r} stream={body_obj.get('stream', False) if body_obj else False} "
            f"inbound={inbound_protocol}"
        )

        # Fast path: /v1/models -> return custom model names (+ group names)
        if path.rstrip("/") == "/v1/models" and method.upper() == "GET":
            return await self._list_models()

        # ------------------------------------------------------------------
        # T22 / R-P0-22: the single v2/v3 fork point.
        #
        # A Responses request is served by the v3 resource handler when ALL of
        # the following hold, otherwise it falls through to the legacy path:
        #   1. the inbound protocol is RESPONSES,
        #   2. a v3 handler is wired in (store-backed setup),
        #   3. the feature flag evaluates to enabled for this request
        #      (env hard override > key rollout > model rollout > group
        #      rollout > global enabled > default true, R-P0-25).
        # Key rollout is applied to the *candidate* keys: when the feature
        # flag would enable v3 but every candidate key is excluded, we fall
        # back to the legacy path and record the fallback metric with reason
        # ``all_keys_excluded`` (so a bad rollout can never strand requests).
        # The feature flag is evaluated here — once, at the single fork — so
        # Chat / Anthropic traffic is never touched and a disabled flag makes
        # the *next* Responses request go straight back to v2.
        # ------------------------------------------------------------------
        if inbound_protocol == "responses":
            # T04 / AC-8.5: stamp which implementation serves this request.
            # Evaluated ONCE (R-P0-22) and written to the context before any
            # dispatch, so every downstream branch — including the fallbacks
            # below — carries an honest label instead of a guess.
            v3_ok = bool(self._v3 is not None and (self._feature_flags is None or self._feature_flags.v3_enabled(ctx)))
            ctx.responses_implementation = "v3" if v3_ok else "v2_emergency"
            if v3_ok:
                # Only CREATE consumes an upstream candidate.  Store-backed
                # retrieve/delete/cancel/input_items and the honest compact
                # capability response must remain usable even when no upstream
                # key is currently healthy.
                is_create = method.upper() == "POST" and path.rstrip("/") == "/v1/responses"
                ctx.v3_enabled = True
                ctx.endpoint = "create" if is_create else "resource"
                if not is_create:
                    return await self._dispatch_v3(request, ctx)

                candidates = self._resolve_candidates(requested_model)
                v3_candidates = self._filter_v3_candidates(candidates, ctx)
                if candidates and not v3_candidates:
                    # Rollout excluded every otherwise-eligible key — fall back
                    # to legacy and expose the exact operational reason.
                    record_v3_fallback(reason="all_keys_excluded")
                    ctx.responses_implementation = "v2_emergency"
                elif v3_candidates:
                    # T26/P0-1: create executes against a REAL upstream through
                    # the production chain (capability routing -> scheduler ->
                    # translator -> SSE pipeline -> terminal persistence).  Only
                    # store-backed resource endpoints and the honest compact 501
                    # go through the resource skeleton.
                    #
                    # The three create shapes are decided here, once, and each
                    # owns a whole function -- ``stream=true`` returns a
                    # ``web.StreamResponse`` and cannot share a body with the
                    # ``web.Response`` paths without losing the Phase A/B line.
                    if body_obj and body_obj.get("background"):
                        return await self._dispatch_v3_create_background(
                            request,
                            ctx,
                            v3_candidates,
                        )
                    if body_obj and body_obj.get("stream"):
                        return await self._dispatch_v3_create_stream(
                            request,
                            ctx,
                            v3_candidates,
                        )
                    return await self._dispatch_v3_create(
                        request,
                        ctx,
                        v3_candidates,
                    )

        # Legacy: Responses API (Codex): only POST is supported. GET
        # /v1/responses/{id} (retrieve) / DELETE are not implemented — return
        # 405 so Codex doesn't hang or retry with bogus bodies.  (When the v3
        # handler is wired, non-POST Responses requests above were already
        # served by v3; reaching this 405 means v3 was disabled or absent.)
        if path.startswith("/v1/responses") and method.upper() != "POST":
            return web.json_response(
                {"error": {"message": "method not allowed for /v1/responses", "type": "invalid_request_error"}},
                status=405,
            )

        # Short circuit: no keys configured
        candidates = self._resolve_candidates(requested_model)
        if not candidates:
            model_hint = f" for model {requested_model!r}" if requested_model else ""
            return web.json_response(
                {"error": f"no enabled keys{model_hint}"},
                status=503,
            )

        # Determine if this is a streaming request
        is_stream = bool(body_obj and body_obj.get("stream", False))
        is_anthropic = inbound_protocol == "anthropic"

        # Base headers (filter hop-by-hop)
        base_headers = {}
        for hk, hv in request.headers.items():
            kl = hk.lower()
            if kl not in (
                "host",
                "connection",
                "transfer-encoding",
                "content-length",
                "content-encoding",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailer",
                "upgrade",
                "x-forwarded-for",
                "x-forwarded-proto",
            ):
                base_headers[hk] = hv

        if not is_anthropic:
            # Keep original auth header if present
            pass

        # --- Non-streaming path ---
        if not is_stream:
            session_key = self._session_key(request, body_obj)
            tried: set[int] = set()
            while True:
                # First attempt: prefer the sticky session key (multi-turn continuity)
                if session_key and not tried:
                    # T35：进程重启后从 ResponseStore 恢复 binding，保持粘性连续性。
                    await self._restore_sticky_from_store(session_key)
                    sticky_k = self._get_sticky_key(
                        session_key,
                        candidates,
                        body_obj,
                        required_caps,
                    )
                    if sticky_k is None and session_key in self._binding_failover_reasons:
                        # 判据⑥：sticky key 因健康/能力不兼容被拒 → 记录故障迁移
                        await self._persist_sticky_failover(session_key)
                    k = sticky_k if sticky_k is not None else pick_key([x for x in candidates if x.key_id not in tried])
                else:
                    k = pick_key([x for x in candidates if x.key_id not in tried])
                if k is None:
                    # 优化点8：429 响应带 X-Zhongzhuan-Reason 头
                    reason = reason_for_exhaustion(candidates)
                    return web.json_response(
                        {"error": "all keys exhausted"},
                        status=429,
                        headers={"X-Zhongzhuan-Reason": reason},
                    )
                tried.add(k.key_id)

                if k.window is not None and not k.window.allow(1):
                    continue
                k.record_request()  # RPD counting

                client = await self._ensure_client(k.upstream_base)
                if client is None:
                    _lg.error(
                        f"[{id(request):x}] key_id={k.key_id} "
                        f"upstream_base={k.upstream_base!r} lazy-init failed, skipping"
                    )
                    continue

                # Determine if protocol translation is needed
                outbound_protocol = k.upstream_protocol
                need_translation = inbound_protocol != outbound_protocol

                # Prepare body, path, and headers
                upstream_path = path
                final_body = ctx.raw_body
                headers = dict(base_headers)
                # Non-streaming: allow upstream compression for faster response transfer.
                # httpx handles transparent decompression.
                # Streaming keeps Accept-Encoding: identity (set in _stream_proxy) to
                # avoid compressing SSE chunk boundaries.

                if need_translation:
                    # Translate request body
                    try:
                        body_obj_t = ctx.body or {}
                    except (json.JSONDecodeError, ValueError):
                        body_obj_t = {}

                    if inbound_protocol == "anthropic" and outbound_protocol == "openai":
                        translated_req = translate_request_a2o(body_obj_t, k.max_tokens_default)
                        upstream_path = "/v1/chat/completions"
                    elif inbound_protocol == "openai" and outbound_protocol == "anthropic":
                        translated_req = translate_request_o2a(body_obj_t, k.anthropic_version)
                        upstream_path = "/v1/messages"
                    elif inbound_protocol == "responses":
                        # Responses API (Codex) -> Chat Completions upstream
                        cc = convert_responses_request_to_chatcompletions(body_obj_t)
                        if outbound_protocol == "anthropic":
                            translated_req = translate_request_o2a(cc, k.anthropic_version)
                            upstream_path = "/v1/messages"
                        else:
                            translated_req = cc
                            upstream_path = "/v1/chat/completions"
                        # Ensure token usage is returned (needed for accounting).
                        if isinstance(translated_req, dict) and body_obj_t.get("stream") is True:
                            so = translated_req.setdefault("stream_options", {})
                            if not so.get("include_usage"):
                                so["include_usage"] = True
                    else:
                        translated_req = body_obj_t

                    if k.upstream_model:
                        translated_req["model"] = k.upstream_model

                    final_body = json.dumps(translated_req, ensure_ascii=False).encode()

                    if outbound_protocol == "anthropic":
                        headers["x-api-key"] = k.api_key
                        headers["anthropic-version"] = k.anthropic_version
                        headers.pop("Authorization", None)
                    else:
                        headers["Authorization"] = f"Bearer {k.api_key}"
                        headers.pop("x-api-key", None)
                        headers.pop("anthropic-version", None)

                    headers["Content-Length"] = str(len(final_body))
                else:
                    if requested_model and k.upstream_model and requested_model != k.upstream_model:
                        final_body = _swap_model_name(ctx.raw_body, requested_model, k.upstream_model)
                    headers["Authorization"] = f"Bearer {k.api_key}"
                    if final_body is not ctx.raw_body:
                        headers["Content-Length"] = str(len(final_body))

                # 客户端指纹模拟（v009）：Authorization 注入后、path 处理前注入
                self._apply_client_fingerprint(headers, k)
                # require_system 预设（如 workbuddy）：请求体缺 system 消息时补一条
                _injected = self._inject_system_message(final_body, k, getattr(k, "upstream_model", None))
                if _injected is not final_body:
                    final_body = _injected
                    headers["Content-Length"] = str(len(final_body))

                # upstream_path_override: non-empty → use directly as path/URL
                if k.upstream_path_override:
                    upstream_path = k.upstream_path_override
                    _lg.info(f"[{id(request):x}] key_id={k.key_id} using upstream_path_override={upstream_path!r}")

                try:
                    # Check if client is still connected before making expensive upstream calls
                    transport = request.transport
                    if transport is not None and transport.is_closing():
                        _lg.warning(f"[{id(request):x}] client transport closing before upstream request, aborting")
                        return web.Response(status=499, text="Client Closed Request")

                    _upstream_start = time.time()
                    resp = await client.request(
                        request.method,
                        upstream_path,
                        headers=headers,
                        content=final_body,
                    )
                    _upstream_elapsed = time.time() - _upstream_start
                    _lg.info(
                        f"[{id(request):x}] key_id={k.key_id} upstream responded in "
                        f"{_upstream_elapsed * 1000:.0f}ms status={resp.status_code}"
                    )
                except (ConnectionResetError, ConnectionError, OSError) as e:
                    # Client-side disconnect (timeout or cancel).
                    # This is NOT an upstream failure — do NOT mark the key as failed.
                    transport = request.transport
                    if transport is not None and transport.is_closing():
                        _lg.warning(f"[{id(request):x}] client disconnected before upstream response")
                        return web.Response(status=499, text="Client Closed Request")
                    # Otherwise it may be an upstream connection failure; log and retry.
                    _lg.error(f"[{id(request):x}] key_id={k.key_id} connection error: {type(e).__name__}: {e}")
                    mark_network_failure(k)
                    continue
                except Exception as e:
                    _lg.error(f"[{id(request):x}] key_id={k.key_id} exception: {type(e).__name__}: {e}")
                    mark_network_failure(k)
                    continue

                data = await resp.aread()
                resp_headers = dict(resp.headers)
                # Remove hop-by-hop / unwanted headers
                resp_headers.pop("content-encoding", None)
                resp_headers.pop("transfer-encoding", None)
                resp_headers.pop("content-length", None)
                # If the upstream returned gzip, decompress locally
                content_encoding = resp_headers.get("content-encoding", "").lower()
                if "gzip" in content_encoding:
                    import gzip

                    try:
                        data = gzip.decompress(data)
                    except Exception:
                        pass
                    resp_headers.pop("content-encoding", None)

                if resp.status_code >= 400:
                    # Translate error envelope if needed
                    err_msg = data.decode("utf-8", errors="replace")
                    # Responses inbound over an OpenAI upstream already gets an
                    # OpenAI-shaped error envelope — translating it as Anthropic
                    # would mangle it, so pass it straight through.
                    _skip_err_tr = inbound_protocol == "responses" and outbound_protocol != "anthropic"
                    if need_translation and not _skip_err_tr:
                        if inbound_protocol == "anthropic":
                            tr_status, tr_body = translate_error_o2a(resp.status_code, err_msg)
                        else:
                            tr_status, tr_body = translate_error_a2o(resp.status_code, err_msg)
                        status = tr_status
                        body = json.dumps(tr_body, ensure_ascii=False).encode()
                    else:
                        status = resp.status_code
                        body = data

                    # 优化点2：用 classify_failure 统一处理状态码分流（消除重复）
                    should_retry = classify_failure(k, resp.status_code, resp_headers)

                    _lg.info(
                        f"[{id(request):x}] key_id={k.key_id} failure status={status} "
                        f"key_state={k.status} retry={should_retry}"
                    )
                    if self.store:
                        latency_ms = int((time.time() - _request_start) * 1000)
                        asyncio.create_task(
                            log_request(
                                self.store,
                                client_ip=request.remote or "",
                                model_name=requested_model or "",
                                key_id=k.key_id,
                                status=resp.status_code,
                                latency_ms=latency_ms,
                                inbound_protocol=inbound_protocol,
                                outbound_protocol=outbound_protocol,
                                translated=need_translation,
                            )
                        )
                    # Retry auth failures (next key may be valid), 429, and 5xx.
                    # Other 4xx are request-side errors → return to client.
                    if should_retry:
                        continue
                    return web.Response(status=status, body=body)

                # Translate response body if needed
                _process_start = time.time()
                if need_translation:
                    try:
                        resp_data = json.loads(data)
                        if inbound_protocol == "anthropic":
                            translated_resp = translate_response_o2a(resp_data, requested_model or "")
                        elif inbound_protocol == "responses":
                            # Downstream is Chat Completions JSON (openai) or
                            # Anthropic JSON (anthropic) -> normalize to Chat
                            # Completions first, then to Responses API.
                            cc_resp = (
                                translate_response_a2o(resp_data, requested_model or "")
                                if outbound_protocol == "anthropic"
                                else resp_data
                            )
                            translated_resp = chatcompletions_to_responses(cc_resp, requested_model or "")
                        else:
                            translated_resp = translate_response_a2o(resp_data, requested_model or "")
                        data = json.dumps(translated_resp, ensure_ascii=False).encode()
                        resp_headers["Content-Type"] = "application/json"
                        _lg.info(
                            f"[{id(request):x}] key_id={k.key_id} response translated "
                            f"{outbound_protocol}->{inbound_protocol}"
                        )
                    except (json.JSONDecodeError, ValueError) as e:
                        _lg.warning(
                            f"[{id(request):x}] key_id={k.key_id} failed to translate response: {e}, returning raw"
                        )

                _process_elapsed = time.time() - _process_start
                total_elapsed = time.time() - _request_start
                _lg.info(
                    f"[{id(request):x}] key_id={k.key_id} upstream={_upstream_elapsed * 1000:.0f}ms "
                    f"proc={_process_elapsed * 1000:.0f}ms total={total_elapsed * 1000:.0f}ms body={len(data)}b"
                )
                mark_success(k)

                # Learn rate limits from success responses too (OpenAI sends
                # x-ratelimit-* headers on 200, not just 429)
                learn_rate_limits(k, resp_headers, resp.status_code)

                # Record token usage for TPM tracking + 配额扣减 + 成本估算
                _tokens_in = 0
                _tokens_out = 0
                try:
                    _resp_obj = json.loads(data)
                    _usage = _resp_obj.get("usage") if isinstance(_resp_obj, dict) else None
                    if isinstance(_usage, dict):
                        _tokens_in = int(_usage.get("prompt_tokens") or _usage.get("input_tokens") or 0)
                        _tokens_out = int(_usage.get("completion_tokens") or _usage.get("output_tokens") or 0)
                        k.record_tokens(_tokens_in, _tokens_out)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

                # Sticky session: remember which key served this conversation
                if session_key:
                    self._set_sticky(session_key, k.key_id, required_caps)
                    # T35 / R-P1-61 判据⑤：ResponseStore 持久化 session→route binding。
                    asyncio.create_task(
                        self._persist_sticky_binding(
                            session_key,
                            k.key_id,
                            required_caps,
                        )
                    )

                # Log successful request asynchronously（含 token 用量 + 配额扣减 + 成本）
                if self.store:
                    latency_ms = int((time.time() - _request_start) * 1000)
                    _token_id = request.get("token_id", 0)
                    asyncio.create_task(
                        self._log_and_deduct(
                            self.store,
                            client_ip=request.remote or "",
                            model_name=requested_model or "",
                            key_id=k.key_id,
                            status=resp.status_code,
                            latency_ms=latency_ms,
                            tokens_in=_tokens_in,
                            tokens_out=_tokens_out,
                            inbound_protocol=inbound_protocol,
                            outbound_protocol=outbound_protocol,
                            translated=need_translation,
                            token_id=_token_id,
                        )
                    )

                return web.Response(status=resp.status_code, body=data, headers=resp_headers)

        # --- Streaming path ---
        return await self._stream_proxy(
            request=request,
            body=ctx.raw_body,
            body_obj=body_obj or {},
            path=path,
            base_headers=base_headers,
            candidates=candidates,
            inbound_protocol=inbound_protocol,
            requested_model=requested_model,
            session_key=self._session_key(request, body_obj),
            required_caps=required_caps,
        )

    async def _stream_proxy(
        self,
        request: web.Request,
        body: bytes,
        body_obj: dict,
        path: str,
        base_headers: dict[str, str],
        candidates: list[KeyHealth],
        inbound_protocol: str,
        requested_model: str,
        session_key: str = "",
        required_caps: frozenset[str] = frozenset(),
    ) -> web.StreamResponse:
        _stream_start = time.time()
        resp = web.StreamResponse(status=200)
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Connection"] = "keep-alive"
        await resp.prepare(request)

        # Empty body = nothing to stream (e.g. preflight or malformed)
        if not body:
            _lg.info(f"[{id(request):x}] streaming: empty body, returning empty stream")
            try:
                await resp.write(b"data: [DONE]\n\n")
            except Exception:
                pass
            return resp  # 200 with empty SSE stream, no error

        # --- Start keepalive task (sends SSE comment every 10s to prevent idle timeout) ---
        keepalive_running = True

        async def _keepalive():
            while keepalive_running:
                try:
                    await asyncio.sleep(10)
                    # SSE comment lines are ignored by clients but reset idle timeout
                    await resp.write(b": keepalive\n\n")
                except (ConnectionResetError, ConnectionError, OSError, asyncio.CancelledError):
                    break
                except Exception:
                    break

        keepalive_task = asyncio.create_task(_keepalive())

        try:
            # --- Retry loop: keeps trying until a key works or client disconnects ---
            retry_delay = 2.0
            while True:
                tried: set[int] = set()
                attempt = 0
                # Circuit breaker: track exception types across one round.
                # If ALL keys in a round fail with the same exception type,
                # the upstream is systemically unavailable — do NOT retry.
                first_exc_type: str | None = None
                all_same_type = True

                for _ in range(len(candidates)):
                    # First attempt in each round: prefer sticky session key
                    if session_key and not tried:
                        # T35：进程重启后从 ResponseStore 恢复 binding，保持粘性连续性。
                        await self._restore_sticky_from_store(session_key)
                        sticky_k = self._get_sticky_key(
                            session_key,
                            candidates,
                            body_obj or {},
                            required_caps,
                        )
                        if sticky_k is None and session_key in self._binding_failover_reasons:
                            # 判据⑥：sticky key 因健康/能力不兼容被拒 → 记录故障迁移
                            await self._persist_sticky_failover(session_key)
                        k = (
                            sticky_k
                            if sticky_k is not None
                            else pick_key([x for x in candidates if x.key_id not in tried])
                        )
                    else:
                        k = pick_key([x for x in candidates if x.key_id not in tried])
                    if k is None:
                        break
                    tried.add(k.key_id)
                    attempt += 1

                    if k.window is not None and not k.window.allow(1):
                        continue
                    k.record_request()  # RPD counting

                    client = await self._ensure_client(k.upstream_base)
                    if client is None:
                        continue

                    # Determine if translation is needed for this key
                    outbound_protocol = k.upstream_protocol
                    need_translation = inbound_protocol != outbound_protocol

                    # Prepare body, path, headers
                    upstream_path = path
                    final_body = body
                    headers = dict(base_headers)
                    headers["Accept-Encoding"] = "identity"

                    if need_translation:
                        # ``body_obj`` comes from RequestContext and is the
                        # authoritative parse result, including a valid empty
                        # object. Never parse the raw bytes again on retries.
                        body_obj_s = body_obj

                        if inbound_protocol == "anthropic" and outbound_protocol == "openai":
                            translated_req = translate_request_a2o(body_obj_s, k.max_tokens_default)
                            upstream_path = "/v1/chat/completions"
                        elif inbound_protocol == "openai" and outbound_protocol == "anthropic":
                            translated_req = translate_request_o2a(body_obj_s, k.anthropic_version)
                            upstream_path = "/v1/messages"
                        elif inbound_protocol == "responses":
                            cc = convert_responses_request_to_chatcompletions(body_obj_s)
                            if outbound_protocol == "anthropic":
                                translated_req = translate_request_o2a(cc, k.anthropic_version)
                                upstream_path = "/v1/messages"
                            else:
                                translated_req = cc
                                upstream_path = "/v1/chat/completions"
                            if isinstance(translated_req, dict) and body_obj_s.get("stream") is True:
                                so = translated_req.setdefault("stream_options", {})
                                if not so.get("include_usage"):
                                    so["include_usage"] = True
                        else:
                            translated_req = body_obj_s

                        if k.upstream_model:
                            translated_req["model"] = k.upstream_model

                        final_body = json.dumps(translated_req, ensure_ascii=False).encode()

                        if outbound_protocol == "anthropic":
                            headers["x-api-key"] = k.api_key
                            headers["anthropic-version"] = k.anthropic_version
                            headers.pop("Authorization", None)
                        else:
                            headers["Authorization"] = f"Bearer {k.api_key}"
                            headers.pop("x-api-key", None)
                            headers.pop("anthropic-version", None)

                        headers["Content-Length"] = str(len(final_body))
                    else:
                        if requested_model and k.upstream_model and requested_model != k.upstream_model:
                            final_body = _swap_model_name(body, requested_model, k.upstream_model)
                        headers["Authorization"] = f"Bearer {k.api_key}"
                        if final_body is not body:
                            headers["Content-Length"] = str(len(final_body))

                    # 客户端指纹模拟（v009）：Authorization 注入后、path 处理前注入
                    self._apply_client_fingerprint(headers, k)
                    # require_system 预设（如 workbuddy）：请求体缺 system 消息时补一条
                    _injected = self._inject_system_message(final_body, k, getattr(k, "upstream_model", None))
                    if _injected is not final_body:
                        final_body = _injected
                        headers["Content-Length"] = str(len(final_body))

                    # upstream_path_override: non-empty → use directly as path/URL
                    if k.upstream_path_override:
                        upstream_path = k.upstream_path_override

                    try:
                        async for upstream_resp in client.stream(
                            request.method,
                            upstream_path,
                            headers=headers,
                            content=final_body,
                        ):
                            # Any 4xx/5xx is a failure: do NOT forward as SSE
                            # (the body is a JSON error envelope, not a stream,
                            # and forwarding it yields an unparseable response).
                            if upstream_resp.status_code >= 400:
                                # 优化点2：用 classify_failure 统一处理状态码分流
                                _st = upstream_resp.status_code
                                _up_headers = dict(upstream_resp.headers)
                                # T07: 使用返回值决定是否换 key。True=可重试（换下
                                # 一个 key）；False=请求侧错误（直接返回客户端）。
                                _retryable = classify_failure(k, _st, _up_headers)
                                # Drain error body for logging / circuit breaker
                                try:
                                    err_body = await upstream_resp.aread()
                                    err_txt = err_body.decode("utf-8", errors="replace")[:300]
                                except Exception:
                                    err_txt = ""
                                _lg.info(
                                    f"[{id(request):x}] streaming: key_id={k.key_id} "
                                    f"upstream status={_st} key_state={k.status} "
                                    f"retryable={_retryable} err={err_txt!r}"
                                )
                                if not _retryable:
                                    # 请求侧错误（如 400/404/422）：不换 key，直接返回错误。
                                    return web.Response(
                                        status=_st,
                                        body=err_body,
                                        content_type="application/json",
                                    )
                                # 可重试类错误：换下一个 key 重试
                                break

                            # Success! Cancel keepalive and forward stream
                            keepalive_running = False
                            keepalive_task.cancel()
                            try:
                                await keepalive_task
                            except (asyncio.CancelledError, Exception):
                                pass

                            _lg.info(
                                f"[{id(request):x}] streaming: key_id={k.key_id} "
                                f"upstream ready, forwarding SSE stream "
                                f"(translated={need_translation})"
                            )

                            # Create stream translator if needed
                            stream_translator: Any = None
                            if need_translation:
                                if inbound_protocol == "responses":
                                    # Chat Completions SSE (or Anthropic SSE) -> Responses SSE
                                    resp_tr = ResponsesStreamTranslator(model=requested_model or "")
                                    if outbound_protocol == "anthropic":
                                        # Anthropic upstream -> Chat Completions SSE -> Responses SSE
                                        stream_translator = CompositeStreamTranslator(
                                            StreamA2O(model=requested_model or ""), resp_tr
                                        )
                                    else:
                                        stream_translator = resp_tr
                                elif inbound_protocol == "anthropic":
                                    # OpenAI upstream → Anthropic client
                                    stream_translator = StreamO2A(model=requested_model or "")
                                else:
                                    # Anthropic upstream → OpenAI client
                                    stream_translator = StreamA2O(model=requested_model or "")

                            chunk_count = 0
                            try:
                                async for chunk in upstream_resp.aiter_raw():
                                    if chunk:
                                        if stream_translator:
                                            translated_chunks = await stream_translator.feed(chunk)
                                            for tc in translated_chunks:
                                                await resp.write(tc)
                                        else:
                                            await resp.write(chunk)
                                        chunk_count += 1
                            except (ConnectionResetError, ConnectionError, OSError):
                                _lg.warning(
                                    f"[{id(request):x}] streaming: key_id={k.key_id} client disconnected during stream"
                                )

                            if stream_translator and not stream_translator.done:
                                _lg.warning(
                                    f"[{id(request):x}] streaming: key_id={k.key_id} "
                                    f"upstream stream ended without finish "
                                    f"([DONE] / finish_reason missing); "
                                    f"synthesizing closing events. "
                                    f"terminal_reason=upstream_truncated "
                                    f"state={getattr(stream_translator, 'state', 'n/a')}"
                                )
                                closing = await finish_translator(stream_translator)
                                for ev in closing:
                                    await resp.write(ev)

                            _lg.info(f"[{id(request):x}] streaming: key_id={k.key_id} completed ({chunk_count} chunks)")
                            mark_success(k)

                            # 流式响应的 token 用量：尝试从 stream_translator 提取
                            _stream_tokens_in = 0
                            _stream_tokens_out = 0
                            if stream_translator and hasattr(stream_translator, "usage"):
                                _u = stream_translator.usage
                                if isinstance(_u, dict):
                                    _stream_tokens_in = int(_u.get("prompt_tokens", 0))
                                    _stream_tokens_out = int(_u.get("completion_tokens", 0))
                            if _stream_tokens_in or _stream_tokens_out:
                                k.record_tokens(_stream_tokens_in, _stream_tokens_out)

                            # Sticky session: remember which key served this conversation
                            if session_key:
                                self._set_sticky(
                                    session_key,
                                    k.key_id,
                                    required_caps,
                                )
                                # T35 / R-P1-61 判据⑤：ResponseStore 持久化 binding。
                                asyncio.create_task(
                                    self._persist_sticky_binding(
                                        session_key,
                                        k.key_id,
                                        required_caps,
                                    )
                                )

                            if self.store:
                                latency_ms = int((time.time() - _stream_start) * 1000)
                                _token_id = request.get("token_id", 0)
                                asyncio.create_task(
                                    self._log_and_deduct(
                                        self.store,
                                        client_ip=request.remote or "",
                                        model_name=requested_model or "",
                                        key_id=k.key_id,
                                        status=200,
                                        latency_ms=latency_ms,
                                        tokens_in=_stream_tokens_in,
                                        tokens_out=_stream_tokens_out,
                                        inbound_protocol=inbound_protocol,
                                        outbound_protocol=outbound_protocol,
                                        translated=need_translation,
                                        token_id=_token_id,
                                    )
                                )
                            return resp
                    except (ConnectionResetError, ConnectionError, OSError):
                        _lg.warning(f"[{id(request):x}] streaming: key_id={k.key_id} client disconnected")
                        return resp
                    except Exception as e:
                        mark_network_failure(k)
                        exc_type_str = type(e).__name__
                        _lg.error(f"[{id(request):x}] streaming: key_id={k.key_id} exception: {exc_type_str}: {e}")
                        # Track exception type for circuit breaker
                        if first_exc_type is None:
                            first_exc_type = exc_type_str
                        elif exc_type_str != first_exc_type:
                            all_same_type = False
                        continue

                # Circuit breaker: all keys failed with the same exception
                # (e.g. ReadError / ConnectError) — upstream is systemically down.
                # Don't waste time retrying; return 502 immediately.
                if attempt > 0 and all_same_type and first_exc_type not in ("",):
                    _lg.error(
                        f"[{id(request):x}] streaming: all {attempt} key(s) failed with "
                        f"{first_exc_type}, upstream appears unavailable. "
                        f"Returning 502."
                    )
                    try:
                        await resp.write(
                            b'event: error\ndata: {"error":{"type":"upstream_error",'
                            b'"message":"upstream temporarily unavailable"}}\n\n'
                        )
                    except Exception:
                        pass
                    break

                # All keys in this round failed — wait with backoff, then retry
                _lg.warning(
                    f"[{id(request):x}] streaming: all {attempt} key(s) "
                    f"failed this round, retrying in {retry_delay:.0f}s"
                )
                try:
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 1.5, 30.0)
                except (asyncio.CancelledError, Exception):
                    break

        except asyncio.CancelledError:
            pass
        finally:
            keepalive_running = False
            try:
                keepalive_task.cancel()
            except Exception:
                pass

        return resp

    async def _log_and_deduct(
        self,
        store,
        *,
        client_ip: str = "",
        model_name: str = "",
        key_id: int | None = None,
        status: int = 0,
        latency_ms: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        inbound_protocol: str = "",
        outbound_protocol: str = "",
        translated: bool = False,
        token_id: int = 0,
    ) -> None:
        """记录请求日志 + 扣减令牌配额 + 计算成本（异步调用）。"""
        # 计算成本
        cost = 0.0
        try:
            from ..store.pricing import calculate_cost

            cost = await calculate_cost(store, model_name, tokens_in, tokens_out)
        except Exception:
            pass

        # 写日志
        try:
            await log_request(
                store,
                client_ip=client_ip,
                model_name=model_name,
                key_id=key_id,
                status=status,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                inbound_protocol=inbound_protocol,
                outbound_protocol=outbound_protocol,
                translated=translated,
                token_id=token_id,
                cost=cost,
            )
        except Exception:
            _lg.exception("log_request failed")

        # 扣减令牌配额
        if token_id > 0 and (tokens_in > 0 or tokens_out > 0):
            try:
                from ..store.access_tokens import deduct_token_quota

                total_tokens = tokens_in + tokens_out
                await deduct_token_quota(store, token_id, total_tokens)
            except Exception:
                _lg.exception("deduct_token_quota failed")

    async def _list_models(self) -> web.Response:
        """Return the list of custom model names configured in the admin UI.

        This endpoint is hit by clients (Trae/Cursor/Cline) when they validate
        the base URL. We return the *custom* model names so the user picks them
        in the client's model dropdown. Group names are also exposed as callable
        model names so downstream clients can invoke a whole group.
        """
        from datetime import datetime, timezone

        now = int(datetime.now(timezone.utc).timestamp())
        seen: set[str] = set()
        data: list[dict] = []
        for k in self._keys:
            if not k.is_available():
                continue
            name = k.model_name
            if not name or name in seen:
                continue
            seen.add(name)
            data.append({"id": name, "object": "model", "created": now, "owned_by": "zhongzhuan"})
        # Expose group names as callable model names too
        for gname in self._groups:
            if gname and gname not in seen:
                seen.add(gname)
                data.append({"id": gname, "object": "model", "created": now, "owned_by": "zhongzhuan"})
        return web.json_response({"object": "list", "data": data})


class _BackgroundRequest:
    """A request-shaped stand-in for the detached background execution path.

    :meth:`ProxyHandler._prepare_v3_upstream_call` reads exactly two things off
    the inbound request -- the HTTP method and the headers worth forwarding --
    and a background job has neither: its client disconnected the moment the
    ``202`` was returned.  Passing this tiny object instead of fabricating a
    ``web.Request`` keeps that dependency visible and prevents a future edit
    from quietly forwarding a dead client's ``Authorization`` header upstream.
    """

    __slots__ = ("method", "headers")

    def __init__(self, method: str = "POST") -> None:
        self.method = method
        self.headers: dict[str, str] = {}


class _V3CreateContext:
    """Phase A's result: one create's facts, resolved and ready to execute.

    Produced by :meth:`ProxyHandler._prepare_v3_create` and consumed by the
    three create paths (non-stream / stream / background).  Holding it as a
    value object is what lets the streaming path prove its two-phase commit:
    either the context exists -- and everything that could still fail has
    already succeeded -- or a JSON error was returned instead of it.

    ``body_obj`` is the client's request as sent; ``upstream_body`` is the same
    request with the resolved chain injected into ``input`` (P0-5).  They are
    deliberately distinct: one is what we persist, the other is what we send.
    """

    __slots__ = (
        "body_obj",
        "upstream_body",
        "final_body",
        "sanitized",
        "decision",
        "response_id",
        "workspace_id",
        "previous_response_id",
        "chain_depth",
        "store_enabled",
        "rs",
    )

    def __init__(
        self,
        *,
        body_obj: dict[str, Any],
        upstream_body: dict[str, Any],
        final_body: bytes,
        sanitized: Any,
        decision: Any,
        response_id: str,
        workspace_id: str,
        previous_response_id: str,
        store_enabled: bool,
        rs: Any,
        chain_depth: int = 0,
    ) -> None:
        self.body_obj = body_obj
        self.upstream_body = upstream_body
        self.final_body = final_body
        self.sanitized = sanitized
        self.decision = decision
        self.response_id = response_id
        self.workspace_id = workspace_id
        self.previous_response_id = previous_response_id
        #: Depth of THIS turn in the state chain (0 = it starts one).  Carried
        #: so the background path can write the chain row after ``enqueue``
        #: without resolving the same chain a second time.
        self.chain_depth = chain_depth
        self.store_enabled = store_enabled
        self.rs = rs


class _V3UpstreamCall:
    """Everything needed to issue one v3 upstream request, already resolved.

    Produced by :meth:`ProxyHandler._prepare_v3_upstream_call` so the stream
    and non-stream paths share one -- and only one -- implementation of key
    selection, request translation, auth headers and path resolution.  Holding
    it as a value object is what makes the streaming path's two-phase commit
    possible: the object either exists (nothing left that can fail before the
    first byte) or an error result was returned instead.
    """

    __slots__ = (
        "key",
        "client",
        "method",
        "path",
        "headers",
        "body",
        "outbound_protocol",
        "need_translation",
    )

    def __init__(
        self,
        *,
        key: KeyHealth,
        client: UpstreamClient,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        outbound_protocol: str,
        need_translation: bool,
    ) -> None:
        self.key = key
        self.client = client
        self.method = method
        self.path = path
        self.headers = headers
        self.body = body
        self.outbound_protocol = outbound_protocol
        self.need_translation = need_translation


class _V3UpstreamResult:
    """Minimal status+body pair returned by :meth:`_run_v3_nonstream`.

    Both the early-error branch (``_http_json``) and the real ``httpx.Response``
    expose ``status_code`` + ``body`` so the caller needs no type dispatch.
    """

    __slots__ = ("status_code", "body")

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = int(status_code)
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")


def _http_json(status: int, payload: Any) -> _V3UpstreamResult:
    """Build an early-error result with ``status_code`` + ``body``.

    ``payload`` may be bytes, str or a JSON-serialisable object.
    """
    if isinstance(payload, bytes):
        body = payload
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _V3UpstreamResult(status, body)
