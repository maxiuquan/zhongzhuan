"""Responses Bridge v3 endpoint routing (T15).

Resolves an incoming ``method`` + ``path`` pair into a
:class:`~.responses_models.ResponsesEndpoint` plus the path parameters
(``response_id``, ``item_id``, etc.), per §4.2.2 of the architecture document.

The six official endpoints (§4.2.2):

    POST   /v1/responses                          -> CREATE
    GET    /v1/responses/{response_id}            -> RETRIEVE
    DELETE /v1/responses/{response_id}            -> DELETE
    POST   /v1/responses/{response_id}/cancel     -> CANCEL
    POST   /v1/responses/compact                  -> COMPACT
    GET    /v1/responses/{response_id}/input_items -> INPUT_ITEMS

This module is a **pure function** -- no IO, no store access, no protocol
state.  It only decides *which* endpoint a request targets so the ResponseStore
layer (T16) and the v3 handler can dispatch correctly.  Keeping it pure makes
the routing matrix trivially testable and lets the caller apply auth/tenant
checks before touching the store.

Deviations / notes:
* ``/v1/responses/compact`` is a POST with no ``response_id`` in the path.
* ``output_items`` (GET) is not in the six official endpoints; the official
  listing endpoint is ``input_items``.  We still recognise ``output_items`` as
  a forward-compatible alias but map it to ``INPUT_ITEMS`` only if the payload
  is queried generically -- for now it is treated as unknown (405) until the
  official API adds it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .responses_models import ResponsesEndpoint

#: ``/v1/responses`` prefix.
_PREFIX = "/v1/responses"

#: ``GET|POST|DELETE`` recognised methods.
_METHODS = {"GET", "POST", "DELETE"}


@dataclass(frozen=True, slots=True)
class RouteMatch:
    """Outcome of :func:`resolve_responses_endpoint`."""

    endpoint: ResponsesEndpoint | None
    response_id: str = ""
    #: Path segments after the endpoint (e.g. "item_id" for a sub-resource).
    params: dict[str, str] = field(default_factory=dict)
    matched: bool = True
    #: Human-readable reason when the route is not matched (405).
    reason: str = ""

    @property
    def is_endpoint(self) -> bool:
        return self.endpoint is not None


def resolve_responses_endpoint(method: str, path: str) -> RouteMatch:
    """Resolve ``method`` + ``path`` to a :class:`RouteMatch`.

    Returns a match with ``endpoint=None`` for any path that is not a Recognised
    Responses resource, or a method that is not allowed for that resource.  The
    caller decides the HTTP status (typically 405 for unmatched/unknown).
    """
    method = (method or "").upper()
    if method not in _METHODS:
        return RouteMatch(None, reason="method_not_allowed")

    if not path or path.rstrip("/") == _PREFIX:
        # POST /v1/responses -> CREATE; anything else -> 405.
        if method == "POST":
            return RouteMatch(ResponsesEndpoint.CREATE)
        return RouteMatch(None, reason="method_not_allowed")

    if not path.startswith(_PREFIX + "/"):
        return RouteMatch(None, reason="not_responses_path")

    head = path[len(_PREFIX) + 1:]
    segments = [s for s in head.split("/") if s]

    # POST /v1/responses/compact
    if segments == ["compact"]:
        if method == "POST":
            return RouteMatch(ResponsesEndpoint.COMPACT)
        return RouteMatch(None, reason="method_not_allowed")

    # /\{response_id\}
    if len(segments) == 1:
        rid = segments[0]
        if method == "GET":
            return RouteMatch(ResponsesEndpoint.RETRIEVE, response_id=rid)
        if method == "DELETE":
            return RouteMatch(ResponsesEndpoint.DELETE, response_id=rid)
        return RouteMatch(None, response_id=rid, reason="method_not_allowed")

    # /\{response_id\}/cancel
    if len(segments) == 2 and segments[1] == "cancel":
        if method == "POST":
            return RouteMatch(ResponsesEndpoint.CANCEL, response_id=segments[0])
        return RouteMatch(None, response_id=segments[0], reason="method_not_allowed")

    # /\{response_id\}/input_items
    if len(segments) == 2 and segments[1] == "input_items":
        if method == "GET":
            return RouteMatch(ResponsesEndpoint.INPUT_ITEMS, response_id=segments[0])
        return RouteMatch(None, response_id=segments[0], reason="method_not_allowed")

    # Unknown sub-resource -> 405.
    return RouteMatch(None, reason="unknown_responses_subresource")


def is_responses_path(path: str) -> bool:
    """Whether ``path`` targets the Responses API (``/v1/responses*``)."""
    return (path or "").rstrip("/") == _PREFIX or path.startswith(_PREFIX + "/")


def method_allows(endpoint: ResponsesEndpoint, method: str) -> bool:
    """Whether ``method`` is allowed for ``endpoint`` (official matrix)."""
    allowed: dict[ResponsesEndpoint, set[str]] = {
        ResponsesEndpoint.CREATE: {"POST"},
        ResponsesEndpoint.RETRIEVE: {"GET"},
        ResponsesEndpoint.DELETE: {"DELETE"},
        ResponsesEndpoint.CANCEL: {"POST"},
        ResponsesEndpoint.COMPACT: {"POST"},
        ResponsesEndpoint.INPUT_ITEMS: {"GET"},
    }
    return (method or "").upper() in allowed.get(endpoint, set())


__all__ = [
    "RouteMatch",
    "resolve_responses_endpoint",
    "is_responses_path",
    "method_allows",
]