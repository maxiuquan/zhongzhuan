"""/v1/* route handler: pass-through with multi-key retry + protocol translation."""
from __future__ import annotations

import json
import time
import urllib.parse

from aiohttp import web

from ..store import Store
from ..store.logs import log_request
from ..upstream import UpstreamClient
from .ratelimit import KeyHealth
from .retry import mark_failure, mark_success
from .scheduler import pick_key
from .protocol.detect import detect_inbound_protocol
from .protocol.translate_a2o import translate_request_a2o, translate_response_o2a
from .protocol.translate_o2a import translate_request_o2a, translate_response_a2o
from .protocol.errors import translate_error_a2o, translate_error_o2a
from .protocol.stream_o2a import StreamO2A
from .protocol.stream_a2o import StreamA2O


def _json_loads(data: bytes) -> object:
    return json.loads(data)


def _swap_model_name(body: bytes, old_name: str, new_name: str) -> bytes:
    """Replace the "model" field value in JSON body."""
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    if isinstance(obj, dict) and obj.get("model") == old_name:
        obj["model"] = new_name
    return json.dumps(obj).encode()


class Handler:
    def __init__(
        self,
        upstream_clients: dict[str, UpstreamClient],
        keys: list[KeyHealth],
        proxy_timeout: float,
        store: Store | None = None,
        load_keys_fn=None,
    ) -> None:
        if not keys:
            raise ValueError("keys must not be empty")
        self.upstream_clients = upstream_clients
        self.keys = keys
        self.proxy_timeout = proxy_timeout
        self.store = store
        self.load_keys_fn = load_keys_fn

    async def _ensure_client(self, upstream_base: str) -> UpstreamClient | None:
        """Lazy-initialize upstream client if not already registered."""
        client = self.upstream_clients.get(upstream_base)
        if client is not None:
            return client
        try:
            from loguru import logger as _lg
            _lg.info(f"lazy-creating upstream client for {upstream_base!r}")
            client = UpstreamClient(base_url=upstream_base, timeout=self.proxy_timeout)
            await client.start()
            self.upstream_clients[upstream_base] = client
            return client
        except Exception as e:
            from loguru import logger as _lg
            _lg.error(f"failed to create upstream client for {upstream_base!r}: {e}")
            return None

    async def reload_keys(self) -> int:
        """Reload keys from the store and update self.keys. Returns new count."""
        if self.load_keys_fn is None:
            return len(self.keys)
        new_keys = await self.load_keys_fn()
        self.keys = new_keys
        return len(new_keys)

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        _request_start = time.time()
        # Handle /v1/models locally: return the list of custom model names
        if request.method == "GET" and request.path == "/v1/models":
            return await self._list_models()

        # Debug: log every incoming request
        from loguru import logger as _lg
        _lg.warning(f"[REQ] {request.method} {request.path} remote={request.remote} content_length={request.content_length} hdrs={dict(request.headers)}")
        # Read body — wrap in try to log failures instead of silently hanging
        try:
            body = await request.read()
        except Exception as _e:
            _lg.error(f"[REQ] read failed: {type(_e).__name__}: {_e}")
            return web.json_response({"error": {"message": f"read failed: {_e}"}}, status=400)
        if body:
            _lg.warning(f"[REQ BODY] {body[:500]!r}")
        # Also write to a separate file for easy access
        try:
            with open(r"f:\xiangmu\zhongzhuan\logs\requests.log", "a", encoding="utf-8") as _f:
                _f.write(f"[REQ] {request.method} {request.path} remote={request.remote}\n")
                _f.write(f"[HDRS] {dict(request.headers)}\n")
                if body:
                    _f.write(f"[BODY] {body[:1000]!r}\n")
                _f.write("-" * 80 + "\n")
        except Exception as _e:
            pass

        base_headers = dict(request.headers)
        for h in ("Host", "Authorization"):
            base_headers.pop(h, None)
        path = request.path

        # Parse body to extract the requested model name and stream flag
        requested_model: str | None = None
        is_stream = False
        try:
            if body:
                body_obj = _json_loads(body)
                if isinstance(body_obj, dict):
                    requested_model = body_obj.get("model")
                    is_stream = bool(body_obj.get("stream", False))
        except Exception:
            pass

        import uuid
        _req_id = str(uuid.uuid4())[:8]

        # Detect inbound protocol (openai vs anthropic)
        inbound_protocol = detect_inbound_protocol(path, dict(request.headers))
        _lg.info(f"[{_req_id}] processing {request.method} {path} model={requested_model!r} stream={is_stream} inbound={inbound_protocol}")

        # Handle Anthropic count_tokens endpoint
        if path == "/v1/messages/count_tokens" and request.method == "POST":
            return await self._handle_count_tokens(request, body, inbound_protocol)

        # Filter keys by requested model (if model name is specified)
        candidates = self.keys
        if requested_model:
            candidates = [k for k in self.keys if k.model_name == requested_model]
            if not candidates:
                _lg.error(f"[{_req_id}] no keys configured for model {requested_model!r}")
                return web.json_response(
                    {"error": {"message": f"no keys configured for model '{requested_model}'", "type": "model_not_found"}},
                    status=503,
                )

        # Streaming path: delegate to _stream_proxy immediately.
        # _stream_proxy handles its own pick_key + rate-limit + translation + retry,
        # so we MUST NOT do any of that here — doing so caused duplicate
        # translation work and an inconsistent key pick between __call__
        # (selected key X, did translation) and _stream_proxy (selected key Y,
        # did translation again, discarded __call__'s work).
        if is_stream:
            return await self._stream_proxy(
                request, path, base_headers, body, requested_model,
                inbound_protocol=inbound_protocol,
            )

        tried: set[int] = set()
        last_error: tuple[int, bytes] | None = None
        attempt = 0
        for _ in range(len(candidates)):
            k = pick_key([x for x in candidates if x.key_id not in tried])
            if k is None:
                _lg.warning(f"[{_req_id}] no more available keys to try (tried={len(tried)})")
                break
            tried.add(k.key_id)
            attempt += 1

            # Check rate limit
            if k.window is not None and not k.window.allow(1):
                _lg.warning(f"[{_req_id}] key_id={k.key_id} model={k.model_name!r} rate-limited (rpm={k.rpm_limit}), skipping")
                continue
            _lg.info(f"[{_req_id}] attempt={attempt} key_id={k.key_id} model={k.model_name!r} upstream={k.upstream_base!r} upstream_model={k.upstream_model!r}")

            client = await self._ensure_client(k.upstream_base)
            if client is None:
                _lg.error(f"[{_req_id}] key_id={k.key_id} upstream_base={k.upstream_base!r} lazy-init failed, skipping")
                continue

            # Determine if protocol translation is needed
            outbound_protocol = k.upstream_protocol
            need_translation = inbound_protocol != outbound_protocol

            # Prepare body, path, and headers
            upstream_path = path
            final_body = body
            headers = dict(base_headers)
            headers["Accept-Encoding"] = "identity"

            if need_translation:
                # Translate request body
                try:
                    body_obj_t = json.loads(body) if body else {}
                except (json.JSONDecodeError, ValueError):
                    body_obj_t = {}

                if inbound_protocol == "anthropic" and outbound_protocol == "openai":
                    translated_req = translate_request_a2o(body_obj_t, k.max_tokens_default)
                    upstream_path = "/v1/chat/completions"
                elif inbound_protocol == "openai" and outbound_protocol == "anthropic":
                    translated_req = translate_request_o2a(body_obj_t, k.anthropic_version)
                    upstream_path = "/v1/messages"
                else:
                    translated_req = body_obj_t

                # Swap model name
                if k.upstream_model:
                    translated_req["model"] = k.upstream_model

                final_body = json.dumps(translated_req, ensure_ascii=False).encode()

                # Build headers for outbound protocol
                if outbound_protocol == "anthropic":
                    headers["x-api-key"] = k.api_key
                    headers["anthropic-version"] = k.anthropic_version
                    headers.pop("Authorization", None)
                else:
                    headers["Authorization"] = f"Bearer {k.api_key}"
                    headers.pop("x-api-key", None)
                    headers.pop("anthropic-version", None)

                headers["Content-Length"] = str(len(final_body))
                _lg.info(f"[{_req_id}] key_id={k.key_id} translated {inbound_protocol}->{outbound_protocol} path={upstream_path}")
            else:
                # Passthrough: swap model name only when matching
                if requested_model and k.upstream_model and k.model_name and requested_model == k.model_name:
                    final_body = _swap_model_name(body, requested_model, k.upstream_model)
                    _lg.info(f"[{_req_id}] key_id={k.key_id} swapped model {requested_model!r} -> {k.upstream_model!r}")
                headers["Authorization"] = f"Bearer {k.api_key}"
                if final_body is not body:
                    headers["Content-Length"] = str(len(final_body))

            _lg.info(f"[{_req_id}] key_id={k.key_id} using key {k.api_key[:8]}...{k.api_key[-4:]}")

            try:
                # Check if client is still connected before making expensive upstream calls
                transport = request.transport
                if transport is not None and transport.is_closing():
                    _lg.warning(f"[{_req_id}] client transport closing before upstream request, aborting")
                    return web.Response(status=499, text="Client Closed Request")

                _lg.info(f"[{_req_id}] key_id={k.key_id} sending request to {upstream_path}")
                resp = await client.request(
                    request.method, upstream_path, headers=headers, content=final_body,
                )
            except (ConnectionResetError, ConnectionError, OSError) as e:
                # Client-side disconnect (timeout or cancel).
                # This is NOT an upstream failure — do NOT mark the key as failed.
                transport = request.transport
                _lg.warning(f"[{_req_id}] key_id={k.key_id} client disconnected: {type(e).__name__}: {e} "
                            f"transport_closing={transport is not None and transport.is_closing()}")
                return web.Response(status=499, text="Client Closed Request")
            except Exception as e:
                mark_failure(k)
                _lg.error(f"[{_req_id}] key_id={k.key_id} request exception: {type(e).__name__}: {e}")
                # Upstream unreachable — return 503 (Service Unavailable), never 502
                last_error = (503, json.dumps({
                    "error": {"message": f"upstream unreachable: {type(e).__name__}: {e}", "type": "upstream_error"}
                }).encode())
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                mark_failure(k)
                _lg.warning(f"[{_req_id}] key_id={k.key_id} upstream returned {resp.status_code}, trying next key")
                last_error = (resp.status_code, await resp.aread())
                continue

            # 4xx 错误统一处理：认证/权限/请求格式等错误
            # 重试其他 key 对这些错误意义不大（除非多 key 池里部分 key 有效），
            # 但仍尝试完所有候选 key，最后走兜底翻译返回。
            if 400 <= resp.status_code < 500:
                err_body = await resp.aread()
                _lg.error(f"[{_req_id}] key_id={k.key_id} upstream {resp.status_code} "
                          f"body={err_body[:500]!r}")
                last_error = (resp.status_code, err_body)
                # 认证/权限错误：key 有问题，不 mark_failure（避免误降健康分）
                if resp.status_code in (401, 403):
                    continue
                # 其他 4xx（400/404/422）：请求本身有问题，重试无意义，直接返回
                mark_success(k)
                break

            mark_success(k)
            _lg.info(f"[{_req_id}] key_id={k.key_id} success status={resp.status_code}")
            data = await resp.aread()
            resp_headers = dict(resp.headers)
            # If upstream sent gzip, decompress so aiohttp can re-encode/serve properly
            content_encoding = resp_headers.get("content-encoding", "").lower()
            if "gzip" in content_encoding:
                import gzip
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            for h in ("content-length", "transfer-encoding", "connection", "content-encoding"):
                resp_headers.pop(h, None)

            # Translate response if protocols differ
            if need_translation:
                try:
                    resp_data = json.loads(data)
                    if inbound_protocol == "anthropic":
                        # Outbound was openai, translate response back to anthropic
                        translated_resp = translate_response_o2a(resp_data, requested_model or "")
                    else:
                        # Outbound was anthropic, translate response back to openai
                        translated_resp = translate_response_a2o(resp_data, requested_model or "")
                    data = json.dumps(translated_resp, ensure_ascii=False).encode()
                    resp_headers["Content-Type"] = "application/json"
                    _lg.info(f"[{_req_id}] key_id={k.key_id} response translated {outbound_protocol}->{inbound_protocol}")
                except (json.JSONDecodeError, ValueError) as e:
                    _lg.warning(f"[{_req_id}] key_id={k.key_id} failed to translate response: {e}, returning raw")

            # Log successful request
            if self.store:
                latency_ms = int((time.time() - _request_start) * 1000)
                await log_request(self.store, client_ip=request.remote, model_name=requested_model or "",
                                  key_id=k.key_id, status=resp.status_code, latency_ms=latency_ms,
                                  inbound_protocol=inbound_protocol, outbound_protocol=outbound_protocol,
                                  translated=need_translation)

            return web.Response(status=resp.status_code, body=data, headers=resp_headers)

        _lg.error(f"[{_req_id}] all {attempt} key(s) failed after {len(tried)} attempt(s)")
        if last_error:
            status, body = last_error
            # 提取上游错误消息（兼容 OpenAI/Anthropic/其他多种格式）
            err_msg = ""
            try:
                err_data = json.loads(body)
                if isinstance(err_data, dict):
                    err_field = err_data.get("error")
                    if isinstance(err_field, dict):
                        err_msg = err_field.get("message", "") or err_field.get("detail", "")
                    elif isinstance(err_field, str) and err_field:
                        err_msg = err_field
                    elif err_data.get("message"):
                        err_msg = err_data["message"]
                    elif err_data.get("detail"):
                        err_msg = str(err_data["detail"])
            except (json.JSONDecodeError, ValueError):
                # 非 JSON：用原始 body 文本
                err_msg = body.decode("utf-8", errors="replace")[:500]
            if not err_msg:
                err_msg = f"upstream returned HTTP {status}"
            _lg.error(f"[{_req_id}] final error: status={status} msg={err_msg!r}")

            # 翻译错误信封：上游协议错误格式 → 入站协议错误格式
            if need_translation:
                if inbound_protocol == "anthropic":
                    # 出站是 openai，翻译回 anthropic
                    tr_status, tr_body = translate_error_o2a(status, err_msg)
                else:
                    # 出站是 anthropic，翻译回 openai
                    tr_status, tr_body = translate_error_a2o(status, err_msg)
                status = tr_status
                body = json.dumps(tr_body, ensure_ascii=False).encode()
                _lg.info(f"[{_req_id}] last_error translated -> {inbound_protocol}")
            # Log failed request
            if self.store:
                latency_ms = int((time.time() - _request_start) * 1000)
                await log_request(self.store, client_ip=request.remote, model_name=requested_model or "",
                                  status=status, latency_ms=latency_ms, error=f"upstream {status}: {err_msg[:200]}")
            return web.Response(status=status, body=body,
                                headers={"Content-Type": "application/json"})
        return web.json_response(
            {"error": {"message": "all upstream keys failed after retries", "type": "upstream_error"}},
            status=503,
        )

    async def _stream_proxy(
        self,
        request: web.Request,
        path: str,
        base_headers: dict,
        body: bytes,
        requested_model: str | None,
        inbound_protocol: str = "openai",
    ) -> web.StreamResponse:
        """SSE streaming pass-through with multi-key retry.

        Sends 200+SSE headers IMMEDIATELY to prevent client timeout,
        then keeps retrying upstream keys until one succeeds.
        Sends SSE keepalive pings during retry waits to keep the
        connection alive. Never returns errors — retries indefinitely
        to avoid interrupting the client's workflow.
        """
        from loguru import logger as _lg
        import uuid
        import asyncio
        _req_id = str(uuid.uuid4())[:8]
        _stream_start = time.time()

        # --- Send 200+SSE headers immediately ---
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        try:
            await resp.prepare(request)
        except (ConnectionResetError, ConnectionError, OSError):
            _lg.warning(f"[{_req_id}] streaming: client already disconnected before SSE prep")
            return resp

        # --- Filter keys by requested model ---
        candidates = self.keys
        if requested_model:
            candidates = [k for k in self.keys if k.model_name == requested_model]
            if not candidates:
                _lg.error(f"[{_req_id}] streaming: no keys configured for model {requested_model!r}")
                return resp  # 200 with empty SSE stream, no error

        # --- Start keepalive task (sends SSE comment every 10s to prevent idle timeout) ---
        keepalive_running = True

        async def _keepalive():
            while keepalive_running:
                try:
                    await asyncio.sleep(10)
                    # SSE comment lines are ignored by clients but reset idle timeout
                    await resp.write(b': keepalive\n\n')
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
                for _ in range(len(candidates)):
                    k = pick_key([x for x in candidates if x.key_id not in tried])
                    if k is None:
                        break
                    tried.add(k.key_id)
                    attempt += 1

                    if k.window is not None and not k.window.allow(1):
                        continue

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
                        try:
                            body_obj_s = json.loads(body) if body else {}
                        except (json.JSONDecodeError, ValueError):
                            body_obj_s = {}

                        if inbound_protocol == "anthropic" and outbound_protocol == "openai":
                            translated_req = translate_request_a2o(body_obj_s, k.max_tokens_default)
                            upstream_path = "/v1/chat/completions"
                        elif inbound_protocol == "openai" and outbound_protocol == "anthropic":
                            translated_req = translate_request_o2a(body_obj_s, k.anthropic_version)
                            upstream_path = "/v1/messages"
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
                        if requested_model and k.upstream_model and k.model_name and requested_model == k.model_name:
                            final_body = _swap_model_name(body, requested_model, k.upstream_model)
                        headers["Authorization"] = f"Bearer {k.api_key}"
                        if final_body is not body:
                            headers["Content-Length"] = str(len(final_body))

                    try:
                        async for upstream_resp in client.stream(
                            request.method, upstream_path, headers=headers, content=final_body,
                        ):
                            if upstream_resp.status_code >= 500 or upstream_resp.status_code == 429:
                                mark_failure(k)
                                break

                            # Success! Cancel keepalive and forward stream
                            keepalive_running = False
                            keepalive_task.cancel()
                            try:
                                await keepalive_task
                            except (asyncio.CancelledError, Exception):
                                pass

                            _lg.info(f"[{_req_id}] streaming: key_id={k.key_id} upstream ready, forwarding SSE stream (translated={need_translation})")

                            # Create stream translator if needed
                            stream_translator = None
                            if need_translation:
                                if inbound_protocol == "anthropic":
                                    # OpenAI upstream → Anthropic client
                                    stream_translator = StreamO2A(model=requested_model or "")
                                else:
                                    # Anthropic upstream → OpenAI client
                                    stream_translator = StreamA2O(model=requested_model or "")

                            chunk_count = 0
                            try:
                                async for chunk in upstream_resp.aiter_raw():
                                    if chunk:
                                        # Log first few raw chunks for diagnosis.
                                        # Empty/abnormally short streams (e.g. 2 chunks
                                        # in <20ms) are almost always upstream error
                                        # events stuffed into SSE — without seeing the
                                        # raw bytes we can't tell what upstream returned.
                                        if chunk_count < 5:
                                            _lg.warning(
                                                f"[{_req_id}] streaming: key_id={k.key_id} "
                                                f"raw chunk#{chunk_count} ({len(chunk)}B): {chunk[:500]!r}"
                                            )
                                        if stream_translator:
                                            translated_chunks = await stream_translator.feed(chunk)
                                            for tc in translated_chunks:
                                                await resp.write(tc)
                                        else:
                                            await resp.write(chunk)
                                        chunk_count += 1
                            except (ConnectionResetError, ConnectionError, OSError):
                                _lg.warning(f"[{_req_id}] streaming: key_id={k.key_id} client disconnected during stream")

                            # If the translator stream hasn't emitted message_stop yet
                            # (e.g. upstream ended without [DONE], or the final
                            # finish_reason chunk was lost to a malformed SSE event),
                            # synthesize the closing events now so the Anthropic
                            # client receives a well-formed stream and doesn't hang
                            # waiting for message_stop.
                            if stream_translator and not stream_translator.done:
                                _lg.warning(
                                    f"[{_req_id}] streaming: key_id={k.key_id} "
                                    f"upstream stream ended without finish ([DONE] / "
                                    f"finish_reason missing); synthesizing closing "
                                    f"events. state={stream_translator.state}"
                                )
                                closing = stream_translator.finish_safely()
                                for ev in closing:
                                    await resp.write(ev)

                            _lg.info(f"[{_req_id}] streaming: key_id={k.key_id} completed ({chunk_count} chunks)")
                            mark_success(k)

                            if self.store:
                                latency_ms = int((time.time() - _stream_start) * 1000)
                                await log_request(self.store, client_ip=request.remote,
                                                  model_name=requested_model or "",
                                                  key_id=k.key_id, status=200, latency_ms=latency_ms,
                                                  inbound_protocol=inbound_protocol,
                                                  outbound_protocol=outbound_protocol,
                                                  translated=need_translation)
                            return resp
                    except (ConnectionResetError, ConnectionError, OSError):
                        _lg.warning(f"[{_req_id}] streaming: key_id={k.key_id} client disconnected")
                        return resp
                    except Exception as e:
                        mark_failure(k)
                        _lg.error(f"[{_req_id}] streaming: key_id={k.key_id} exception: {type(e).__name__}: {e}")
                        continue

                # All keys in this round failed — wait with backoff, then retry
                _lg.warning(f"[{_req_id}] streaming: all {attempt} key(s) failed this round, retrying in {retry_delay:.0f}s")
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

    async def _list_models(self) -> web.Response:
        """Return the list of custom model names configured in the admin UI.

        This endpoint is hit by clients (Trae/Cursor/Cline) when they validate
        the base URL. We return the *custom* model names so the user picks them
        in the client's model dropdown.
        """
        from datetime import datetime, timezone
        now = int(datetime.now(timezone.utc).timestamp())
        seen: set[str] = set()
        data: list[dict] = []
        if self.store is not None:
            from ..store.models import list_models as _list_models_db
            for m in await _list_models_db(self.store):
                if m.name in seen:
                    continue
                seen.add(m.name)
                data.append({
                    "id": m.name,
                    "object": "model",
                    "created": now,
                    "owned_by": "zhongzhuan",
                })
        if not data:
            # Fallback: derive model names from configured keys
            for k in self.keys:
                if k.model_name and k.model_name not in seen:
                    seen.add(k.model_name)
                    data.append({
                        "id": k.model_name,
                        "object": "model",
                        "created": now,
                        "owned_by": "zhongzhuan",
                    })
        return web.json_response({"object": "list", "data": data})

    async def _handle_count_tokens(
        self, request: web.Request, body: bytes, inbound_protocol: str,
    ) -> web.Response:
        """Handle /v1/messages/count_tokens — estimate input tokens locally.

        For Anthropic upstream: could passthrough, but local estimate is sufficient
        for the client's quota planning. Uses len(text)/4 approximation.
        """
        from loguru import logger as _lg
        try:
            body_obj = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            body_obj = {}

        # Extract all text from messages + system
        total_chars = 0
        system = body_obj.get("system", "")
        if isinstance(system, str):
            total_chars += len(system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    total_chars += len(block.get("text", ""))

        for msg in body_obj.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        total_chars += len(block.get("text", ""))

        # Rough estimate: ~4 chars per token
        estimated_tokens = max(1, total_chars // 4)
        _lg.info(f"count_tokens: estimated {estimated_tokens} tokens from {total_chars} chars")

        return web.json_response({"input_tokens": estimated_tokens})


def make_handler(upstream_clients, keys, proxy_timeout, store=None, load_keys_fn=None) -> Handler:
    return Handler(upstream_clients=upstream_clients, keys=keys, proxy_timeout=proxy_timeout, store=store, load_keys_fn=load_keys_fn)