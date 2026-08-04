"""/v1/* route handler: pass-through with multi-key retry + protocol translation."""
from __future__ import annotations

import json
import time
import urllib.parse

from aiohttp import web

import asyncio

from ..store import Store
from ..store.logs import log_request
from ..upstream import UpstreamClient
from ..config.timeouts import DEFAULT_TIMEOUT_POLICY, TimeoutPolicy
from .context import RequestContextBuilder
from .ratelimit import KeyHealth, STATE_HEALTHY
from .retry import (
    mark_auth_failure, mark_rate_limited, mark_server_error,
    mark_network_failure, mark_failure, mark_success, learn_rate_limits,
    classify_failure, reason_for_exhaustion,
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
) -> ProxyHandler:
    """Factory: create a ProxyHandler for the aiohttp route.

    ``proxy_timeout`` (single float) is the deprecated shape kept for existing
    callers/tests; ``timeouts`` is the six-layer policy introduced by T01.
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
    ) -> None:
        self._clients = clients
        self._keys = keys
        self.store = store
        # Timeout wiring (T01): the six-layer policy wins; the legacy single
        # float is only used when no policy was supplied.
        self._timeouts: TimeoutPolicy | None = timeouts
        self._timeout = proxy_timeout
        if self._timeouts is None and proxy_timeout is None:
            self._timeouts = DEFAULT_TIMEOUT_POLICY
        self._load_keys_fn = load_keys_fn
        # RequestContext building (T02): single-parse preamble.
        self._ctx_builder = RequestContextBuilder()
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
        - no match (or empty) → all available keys (backwards-compatible).
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
            matched = [k for k in available if getattr(k, "aliases", "") and requested_model in
                       [a.strip() for a in str(getattr(k, "aliases", "")).split(",") if a.strip()]]
            if matched:
                return matched

        return available

    # ---- Sticky session helpers ----

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
            text = content if isinstance(content, str) else json.dumps(
                content, ensure_ascii=False, sort_keys=True,
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
        """从请求体提取「本请求所需能力」集合（T35 / R-P1-60 判据⑥）。

        Responses API 的 hosted tool 通过 ``tools[N].type`` 声明能力；请求侧
        ``metadata`` / ``instructions`` 里的 ``stateful_responses`` 与
        ``background`` 也计为所需能力。只读一次，无副作用；非 Responses 请求
        返回空集（空集视为「无能力要求」，任何绑定都兼容）。
        """
        if not body_obj:
            return frozenset()
        req: set[str] = set()
        tools = body_obj.get("tools")
        if isinstance(tools, (list, tuple)):
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                tool_type = str(tool.get("type") or "").strip()
                if not tool_type:
                    continue
                # 仅统计 hosted tool（R-P1-60）：普通 function tool 不需要能力承载。
                from .protocol.responses_models import HOSTED_TOOL_CAPABILITY
                cap = HOSTED_TOOL_CAPABILITY.get(tool_type)
                if cap is not None:
                    req.add(cap.value)
        if body_obj.get("background"):
            req.add("background")
        if body_obj.get("metadata"):
            md = body_obj["metadata"]
            if isinstance(md, dict) and md.get("stateful_responses"):
                req.add("stateful_responses")
        return frozenset(req)

    def _get_sticky_key(
        self,
        session_key: str,
        candidates: list[KeyHealth],
        body_obj: dict | None = None,
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
                required = self._required_capabilities(body_obj)
                if required and bound_caps and not (required <= bound_caps):
                    reason = (
                        f"capability mismatch: required={sorted(required)} "
                        f"bound={sorted(bound_caps)}"
                    )
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
        self, session_key: str, key_id: int, caps: frozenset[str],
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
                self._set_groups([
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "strategy": r.get("strategy"),
                        "members": [m["model_id"] for m in (r.get("members") or [])],
                    }
                    for r in rows
                ])
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

    async def start_background_tasks(self) -> None:
        """启动后台周期任务。应在 aiohttp app.on_startup 时调用。"""
        if self._bg_running:
            return
        self._bg_running = True
        self._bg_tasks.append(asyncio.create_task(self._sticky_cleanup_loop()))
        if self.store is not None:
            self._bg_tasks.append(asyncio.create_task(self._health_snapshot_loop()))
        _lg.info(f"started {len(self._bg_tasks)} background tasks")

    async def stop_background_tasks(self) -> None:
        """停止后台任务。应在 aiohttp app.on_cleanup 时调用。"""
        self._bg_running = False
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
                        await save_health(self.store, KeyHealthRow(
                            key_id=k.key_id,
                            status=k.status,
                            cooldown_until=k.cooldown_until,
                            rpm_limit=k.rpm_limit,
                            tpm_limit=k.tpm_limit,
                            success_count=k.success_count,
                            failure_count=k.total_failures,
                            recent_429_count=k.recent_429_count,
                        ))
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                _lg.exception("health snapshot loop error")
                await asyncio.sleep(60)

    async def __call__(self, request: web.Request) -> web.Response:
        _request_start = time.time()
        store = self.store

        # -- Build request context once (T02): body is parsed exactly once here --
        ctx = await self._ctx_builder.build(request)
        path = ctx.path
        method = ctx.method
        body_obj = ctx.body
        inbound_protocol = ctx.inbound_protocol
        requested_model = ctx.requested_model
        remote = ctx.remote

        # Log the incoming request early (before processing)
        _lg.info(
            f"[REQ] {method} {path} remote={remote} content_length={ctx.content_length}"
        )
        _lg.info(
            f"[{id(request):x}] processing {method} {path} "
            f"model={requested_model!r} stream={body_obj.get('stream', False) if body_obj else False} "
            f"inbound={inbound_protocol}"
        )

        # Fast path: /v1/models -> return custom model names (+ group names)
        if path.rstrip("/") == "/v1/models" and method.upper() == "GET":
            return await self._list_models()

        # Responses API (Codex): only POST is supported. GET /v1/responses/{id}
        # (retrieve) / DELETE are not implemented — return 405 so Codex doesn't
        # hang or retry with bogus bodies.
        if path.startswith("/v1/responses") and method.upper() != "POST":
            return web.json_response(
                {"error": {"message": "method not allowed for /v1/responses", "type": "invalid_request_error"}},
                status=405,
            )

        # Short circuit: no keys configured
        candidates = self._resolve_candidates(requested_model)
        if not candidates:
            return web.json_response(
                {"error": "no enabled keys"}, status=503,
            )

        # Determine if this is a streaming request
        is_stream = bool(body_obj and body_obj.get("stream", False))
        is_anthropic = inbound_protocol == "anthropic"

        # Base headers (filter hop-by-hop)
        base_headers = {}
        for k, v in request.headers.items():
            kl = k.lower()
            if kl not in (
                "host", "connection", "transfer-encoding", "content-length",
                "content-encoding", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailer", "upgrade",
                "x-forwarded-for", "x-forwarded-proto",
            ):
                base_headers[k] = v

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
                    sticky_k = self._get_sticky_key(session_key, candidates, body_obj)
                    if sticky_k is None and session_key in self._binding_failover_reasons:
                        # 判据⑥：sticky key 因健康/能力不兼容被拒 → 记录故障迁移
                        await self._persist_sticky_failover(session_key)
                    k = sticky_k if sticky_k is not None else pick_key(
                        [x for x in candidates if x.key_id not in tried]
                    )
                else:
                    k = pick_key([x for x in candidates if x.key_id not in tried])
                if k is None:
                    # 优化点8：429 响应带 X-Zhongzhuan-Reason 头
                    reason = reason_for_exhaustion(candidates)
                    return web.json_response(
                        {"error": "all keys exhausted"}, status=429,
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
                        body_obj_t = json.loads(ctx.raw_body) if ctx.raw_body else {}
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

                # upstream_path_override: non-empty → use directly as path/URL
                if k.upstream_path_override:
                    upstream_path = k.upstream_path_override
                    _lg.info(
                        f"[{id(request):x}] key_id={k.key_id} "
                        f"using upstream_path_override={upstream_path!r}"
                    )

                try:
                    # Check if client is still connected before making expensive upstream calls
                    transport = request.transport
                    if transport is not None and transport.is_closing():
                        _lg.warning(
                            f"[{id(request):x}] client transport closing before upstream request, aborting"
                        )
                        return web.Response(status=499, text="Client Closed Request")

                    _upstream_start = time.time()
                    resp = await client.request(
                        request.method, upstream_path, headers=headers, content=final_body,
                    )
                    _upstream_elapsed = time.time() - _upstream_start
                    _lg.info(
                        f"[{id(request):x}] key_id={k.key_id} upstream responded in "
                        f"{_upstream_elapsed*1000:.0f}ms status={resp.status_code}"
                    )
                except (ConnectionResetError, ConnectionError, OSError) as e:
                    # Client-side disconnect (timeout or cancel).
                    # This is NOT an upstream failure — do NOT mark the key as failed.
                    transport = request.transport
                    if transport is not None and transport.is_closing():
                        _lg.warning(
                            f"[{id(request):x}] client disconnected before upstream response"
                        )
                        return web.Response(status=499, text="Client Closed Request")
                    # Otherwise it may be an upstream connection failure; log and retry.
                    _lg.error(
                        f"[{id(request):x}] key_id={k.key_id} connection error: {type(e).__name__}: {e}"
                    )
                    mark_network_failure(k)
                    continue
                except Exception as e:
                    _lg.error(
                        f"[{id(request):x}] key_id={k.key_id} exception: {type(e).__name__}: {e}"
                    )
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
                                client_ip=request.remote,
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
                            cc_resp = translate_response_a2o(resp_data, requested_model or "") \
                                if outbound_protocol == "anthropic" else resp_data
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
                            f"[{id(request):x}] key_id={k.key_id} failed to translate "
                            f"response: {e}, returning raw"
                        )

                _process_elapsed = time.time() - _process_start
                total_elapsed = time.time() - _request_start
                _lg.info(
                    f"[{id(request):x}] key_id={k.key_id} upstream={_upstream_elapsed*1000:.0f}ms "
                    f"proc={_process_elapsed*1000:.0f}ms total={total_elapsed*1000:.0f}ms body={len(data)}b"
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
                        _tokens_in = int(_usage.get("prompt_tokens", _usage.get("input_tokens", 0)))
                        _tokens_out = int(_usage.get("completion_tokens", _usage.get("output_tokens", 0)))
                        k.record_tokens(_tokens_in, _tokens_out)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

                # Sticky session: remember which key served this conversation
                if session_key:
                    caps = self._required_capabilities(body_obj)
                    self._set_sticky(session_key, k.key_id, caps)
                    # T35 / R-P1-61 判据⑤：ResponseStore 持久化 session→route binding。
                    asyncio.create_task(
                        self._persist_sticky_binding(session_key, k.key_id, caps)
                    )

                # Log successful request asynchronously（含 token 用量 + 配额扣减 + 成本）
                if self.store:
                    latency_ms = int((time.time() - _request_start) * 1000)
                    _token_id = request.get("token_id", 0)
                    asyncio.create_task(
                        self._log_and_deduct(
                            self.store, client_ip=request.remote,
                            model_name=requested_model or "",
                            key_id=k.key_id, status=resp.status_code,
                            latency_ms=latency_ms,
                            tokens_in=_tokens_in, tokens_out=_tokens_out,
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
    ) -> web.Response:
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
                            session_key, candidates, body_obj or {},
                        )
                        if sticky_k is None and session_key in self._binding_failover_reasons:
                            # 判据⑥：sticky key 因健康/能力不兼容被拒 → 记录故障迁移
                            await self._persist_sticky_failover(session_key)
                        k = sticky_k if sticky_k is not None else pick_key(
                            [x for x in candidates if x.key_id not in tried]
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
                        # Reuse the already-parsed body dict from __call__
                        # to avoid re-parsing a 100KB body on every retry.
                        body_obj_s = body_obj if body_obj is not None else {}
                        if not body_obj_s and body:
                            try:
                                body_obj_s = json.loads(body)
                            except (json.JSONDecodeError, ValueError):
                                body_obj_s = {}

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

                    # upstream_path_override: non-empty → use directly as path/URL
                    if k.upstream_path_override:
                        upstream_path = k.upstream_path_override

                    try:
                        async for upstream_resp in client.stream(
                            request.method, upstream_path, headers=headers, content=final_body,
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
                                        headers={"Content-Type": "application/json"},
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
                            stream_translator = None
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
                                    f"[{id(request):x}] streaming: key_id={k.key_id} "
                                    f"client disconnected during stream"
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

                            _lg.info(
                                f"[{id(request):x}] streaming: key_id={k.key_id} "
                                f"completed ({chunk_count} chunks)"
                            )
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
                                caps = self._required_capabilities(body_obj)
                                self._set_sticky(session_key, k.key_id, caps)
                                # T35 / R-P1-61 判据⑤：ResponseStore 持久化 binding。
                                asyncio.create_task(
                                    self._persist_sticky_binding(session_key, k.key_id, caps)
                                )

                            if self.store:
                                latency_ms = int((time.time() - _stream_start) * 1000)
                                _token_id = request.get("token_id", 0)
                                asyncio.create_task(
                                    self._log_and_deduct(
                                        self.store, client_ip=request.remote,
                                        model_name=requested_model or "",
                                        key_id=k.key_id, status=200,
                                        latency_ms=latency_ms,
                                        tokens_in=_stream_tokens_in, tokens_out=_stream_tokens_out,
                                        inbound_protocol=inbound_protocol,
                                        outbound_protocol=outbound_protocol,
                                        translated=need_translation,
                                        token_id=_token_id,
                                    )
                                )
                            return resp
                    except (ConnectionResetError, ConnectionError, OSError):
                        _lg.warning(
                            f"[{id(request):x}] streaming: key_id={k.key_id} "
                            f"client disconnected"
                        )
                        return resp
                    except Exception as e:
                        mark_network_failure(k)
                        exc_type_str = type(e).__name__
                        _lg.error(
                            f"[{id(request):x}] streaming: key_id={k.key_id} "
                            f"exception: {exc_type_str}: {e}"
                        )
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
        self, store, *,
        client_ip: str = "", model_name: str = "",
        key_id: int | None = None, status: int = 0, latency_ms: int = 0,
        tokens_in: int = 0, tokens_out: int = 0,
        inbound_protocol: str = "", outbound_protocol: str = "",
        translated: bool = False, token_id: int = 0,
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
                client_ip=client_ip, model_name=model_name,
                key_id=key_id, status=status, latency_ms=latency_ms,
                tokens_in=tokens_in, tokens_out=tokens_out,
                inbound_protocol=inbound_protocol,
                outbound_protocol=outbound_protocol,
                translated=translated, token_id=token_id, cost=cost,
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
