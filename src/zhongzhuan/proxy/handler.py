"""/v1/* route handler: pass-through with multi-key retry."""
from __future__ import annotations

import json

from aiohttp import web

from ..store import Store
from ..store.logs import log_request
from ..upstream import UpstreamClient
from .ratelimit import KeyHealth
from .retry import mark_failure, mark_success
from .scheduler import pick_key


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

    async def reload_keys(self) -> int:
        """Reload keys from the store and update self.keys. Returns new count."""
        if self.load_keys_fn is None:
            return len(self.keys)
        new_keys = await self.load_keys_fn()
        self.keys = new_keys
        return len(new_keys)

    async def __call__(self, request: web.Request) -> web.StreamResponse:
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
        _lg.info(f"[{_req_id}] processing {request.method} {path} model={requested_model!r} stream={is_stream}")

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

            client = self.upstream_clients.get(k.upstream_base)
            if client is None:
                _lg.error(f"[{_req_id}] key_id={k.key_id} upstream_base={k.upstream_base!r} no matching upstream client, skipping")
                continue

            # Swap model name only when the request's model matches this key's model name
            final_body = body
            if requested_model and k.upstream_model and k.model_name and requested_model == k.model_name:
                final_body = _swap_model_name(body, requested_model, k.upstream_model)
                _lg.info(f"[{_req_id}] key_id={k.key_id} swapped model {requested_model!r} -> {k.upstream_model!r}")

            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {k.api_key}"
            _lg.info(f"[{_req_id}] key_id={k.key_id} using key {k.api_key[:8]}...{k.api_key[-4:]}")
            headers["Accept-Encoding"] = "identity"
            # Update Content-Length if body was modified
            if final_body is not body:
                headers["Content-Length"] = str(len(final_body))

            try:
                # Check if client is still connected before making expensive upstream calls
                transport = request.transport
                if transport is not None and transport.is_closing():
                    _lg.warning(f"[{_req_id}] client transport closing before upstream request, aborting")
                    return web.Response(status=499, text="Client Closed Request")

                if is_stream:
                    full_url = f"{k.upstream_base.rstrip('/')}{path}"
                    _lg.info(f"[{_req_id}] key_id={k.key_id} streaming request to {full_url}")
                    return await self._stream_proxy(
                        request, path, base_headers, body, requested_model,
                    )
                _lg.info(f"[{_req_id}] key_id={k.key_id} sending request to {path}")
                resp = await client.request(
                    request.method, path, headers=headers, content=final_body,
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

            # Log successful request
            if self.store:
                await log_request(self.store, client_ip=request.remote, model_name=requested_model or "",
                                  key_id=k.key_id, status=resp.status_code, latency_ms=0)

            return web.Response(status=resp.status_code, body=data, headers=resp_headers)

        _lg.error(f"[{_req_id}] all {attempt} key(s) failed after {len(tried)} attempt(s)")
        if last_error:
            status, body = last_error
            # Log failed request
            if self.store:
                await log_request(self.store, client_ip=request.remote, model_name=requested_model or "",
                                  status=status, latency_ms=0, error="upstream failed")
            return web.Response(status=status, body=body)
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

                    client = self.upstream_clients.get(k.upstream_base)
                    if client is None:
                        continue

                    # Swap model name
                    final_body = body
                    if requested_model and k.upstream_model and k.model_name and requested_model == k.model_name:
                        final_body = _swap_model_name(body, requested_model, k.upstream_model)

                    headers = dict(base_headers)
                    headers["Authorization"] = f"Bearer {k.api_key}"
                    headers["Accept-Encoding"] = "identity"
                    if final_body is not body:
                        headers["Content-Length"] = str(len(final_body))

                    try:
                        async for upstream_resp in client.stream(
                            request.method, path, headers=headers, content=final_body,
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

                            _lg.info(f"[{_req_id}] streaming: key_id={k.key_id} upstream ready, forwarding SSE stream")
                            chunk_count = 0
                            try:
                                async for chunk in upstream_resp.aiter_raw():
                                    if chunk:
                                        await resp.write(chunk)
                                        chunk_count += 1
                            except (ConnectionResetError, ConnectionError, OSError):
                                _lg.warning(f"[{_req_id}] streaming: key_id={k.key_id} client disconnected during stream")
                            _lg.info(f"[{_req_id}] streaming: key_id={k.key_id} completed ({chunk_count} chunks)")
                            mark_success(k)

                            if self.store:
                                await log_request(self.store, client_ip=request.remote,
                                                  model_name=requested_model or "",
                                                  key_id=k.key_id, status=200, latency_ms=0)
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


def make_handler(upstream_clients, keys, proxy_timeout, store=None, load_keys_fn=None) -> Handler:
    return Handler(upstream_clients=upstream_clients, keys=keys, proxy_timeout=proxy_timeout, store=store, load_keys_fn=load_keys_fn)