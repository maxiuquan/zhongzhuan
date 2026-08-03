"""ResponsesV3Handler: dispatch the six resource endpoints (T21 / R-P0-40).

Resolves ``method`` + ``path`` with the shared
:func:`~zhongzhuan.proxy.protocol.responses_routes.resolve_responses_endpoint`
router, then calls the matching handler in :mod:`.endpoints`.  The handler is
backend-agnostic (it only touches the injected
:class:`~zhongzhuan.store.response_store.ResponseStore`), so it is testable
without an HTTP server.

Return contract: ``dispatch(...) -> (status: int, body: dict)``.  Unmatched
methods / unknown sub-resources yield ``405`` (official matrix, T15).
"""
from __future__ import annotations

from typing import Any

from ..proxy.protocol.responses_models import ResponsesEndpoint
from ..proxy.protocol.responses_routes import (
    is_responses_path,
    resolve_responses_endpoint,
)
from ..store.response_store import ResponseStore
from . import endpoints
from .schema import to_error_object


class ResponsesV3Handler:
    """Dispatch ``/v1/responses*`` requests to the resource handlers."""

    def __init__(self, store: ResponseStore) -> None:
        self._store = store

    async def dispatch(
        self,
        method: str,
        path: str,
        *,
        workspace_id: str = "",
        body: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = body or {}
        if not is_responses_path(path):
            return to_error_object(
                message=f"not a Responses API path: {path}",
                code="not_found",
                status=404,
            )

        match = resolve_responses_endpoint(method, path)
        if not match.is_endpoint:
            # Unmatched method / unknown sub-resource -> 405 (T15 matrix).
            return to_error_object(
                message="method not allowed for this Responses endpoint",
                code="method_not_allowed",
                status=405,
            )

        ep = match.endpoint
        rid = match.response_id
        if ep is ResponsesEndpoint.CREATE:
            return await endpoints.create(self._store, workspace_id=workspace_id, body=body)
        if ep is ResponsesEndpoint.RETRIEVE:
            return await endpoints.retrieve(self._store, workspace_id=workspace_id, response_id=rid)
        if ep is ResponsesEndpoint.DELETE:
            return await endpoints.delete(self._store, workspace_id=workspace_id, response_id=rid)
        if ep is ResponsesEndpoint.CANCEL:
            return await endpoints.cancel(self._store, workspace_id=workspace_id, response_id=rid)
        if ep is ResponsesEndpoint.COMPACT:
            return await endpoints.compact(self._store, workspace_id=workspace_id, body=body)
        if ep is ResponsesEndpoint.INPUT_ITEMS:
            after = int(body.get("after", -1)) if isinstance(body.get("after"), (int, str)) else -1
            limit = int(body.get("limit", 20)) if isinstance(body.get("limit"), (int, str)) else 20
            return await endpoints.input_items(
                self._store, workspace_id=workspace_id, response_id=rid, after=after, limit=limit,
            )
        # Defensive: every enum value is handled above.
        return to_error_object(
            message=f"unhandled endpoint: {ep}", code="not_implemented", status=501,
        )


__all__ = ["ResponsesV3Handler"]
