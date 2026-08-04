"""RequestContext: single-parse request metadata for the proxy handler (T02).

Historically ``ProxyHandler.__call__`` read the request body, ``json.loads``
it, detected the inbound protocol and extracted the model *inline* — and then
the following ~340 lines re-parsed the same body repeatedly.  This module
extracts that preamble into a :class:`RequestContext` built exactly once per
request, so the rest of the pipeline consumes ``ctx.body`` / ``ctx.model`` /
``ctx.inbound_protocol`` instead of re-doing I/O and JSON parsing.

This is a **pure relocation** surface (T02 / R-P1-63 first half, R-P0-22
prerequisite): it must not change any behaviour.  ``InboundProtocol`` is a
``str`` subclass so ``ctx.inbound_protocol == "responses"`` keeps working.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from .protocol.detect import detect_inbound_protocol
from .protocol.responses_models import InboundProtocol


_RAW_BODY_KEY = "zhongzhuan.raw_body"
_JSON_BODY_KEY = "zhongzhuan.json_body"


async def read_request_body(request: web.Request) -> tuple[bytes, dict[str, Any] | None]:
    """Read and parse a JSON object once, caching both values on the request."""
    if _RAW_BODY_KEY in request:
        return request[_RAW_BODY_KEY], request.get(_JSON_BODY_KEY)

    raw_body = await request.read()
    body_obj: dict[str, Any] | None = None
    if raw_body:
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                body_obj = parsed
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            pass

    request[_RAW_BODY_KEY] = raw_body
    request[_JSON_BODY_KEY] = body_obj
    return raw_body, body_obj


@dataclass(slots=True)
class RequestContext:
    """Everything the pipeline needs to know about a single inbound request.

    ``body`` is parsed **exactly once** here; downstream code must read from
    it instead of re-``json.loads``.  ``v3_enabled`` and ``endpoint`` are
    placeholders filled in by the v3 router (T12) — they are ``None`` /
    ``False`` for now and must not be relied upon before that task lands.
    """

    request: web.Request
    raw_body: bytes
    body: dict[str, Any] | None
    inbound_protocol: InboundProtocol
    requested_model: str
    is_stream: bool
    path: str
    method: str
    headers: dict[str, str]
    content_length: int | None
    remote: str
    # v3 placeholders (filled by T12 router; inert until then).
    endpoint: str | None = None
    v3_enabled: bool = False


class RequestContextBuilder:
    """Builds a :class:`RequestContext` from an aiohttp request."""

    def __init__(self, detect_fn=detect_inbound_protocol) -> None:
        self._detect = detect_fn

    async def build(self, request: web.Request) -> RequestContext:
        raw_body, body_obj = await read_request_body(request)
        path = request.path
        method = request.method
        content_length = len(raw_body) if raw_body else None
        remote = request.remote or ""
        headers = dict(request.headers)

        # Protocol detection (path + headers, unchanged semantics).
        detected = self._detect(path, headers)
        inbound_protocol = InboundProtocol(detected)

        # The shared request cache guarantees one JSON parse across middleware
        # and the handler context builder.
        requested_model = ""
        if body_obj is not None:
            candidate = body_obj.get("model")
            requested_model = candidate.strip() if isinstance(candidate, str) else ""

        is_stream = bool(body_obj and body_obj.get("stream", False))

        return RequestContext(
            request=request,
            raw_body=raw_body,
            body=body_obj,
            inbound_protocol=inbound_protocol,
            requested_model=requested_model,
            is_stream=is_stream,
            path=path,
            method=method,
            headers=headers,
            content_length=content_length,
            remote=remote,
        )
