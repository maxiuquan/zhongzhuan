"""ResponsesV3Handler: dispatch the six resource endpoints (T21 / R-P0-40).

Resolves ``method`` + ``path`` with the shared
:func:`~zhongzhuan.proxy.protocol.responses_routes.resolve_responses_endpoint`
router, then calls the matching handler in :mod:`.endpoints`.  The handler is
backend-agnostic (it only touches the injected
:class:`~zhongzhuan.store.response_store.ResponseStore`), so it is testable
without an HTTP server.

Return contract: ``dispatch(...) -> (status: int, body: dict)``.  Unmatched
methods / unknown sub-resources yield ``405`` (official matrix, T15).

T22 wires the :class:`~.chain.ChainResolver` in here so every ``create`` shares
one (optionally tenant-narrowed) instance of the R-P0-29 guards.
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
from .endpoints import DEFAULT_PAGE_LIMIT
from .chain import ChainResolution, ChainResolver
from .schema import to_error_object


class ResponsesV3Handler:
    """Dispatch ``/v1/responses*`` requests to the resource handlers."""

    def __init__(self, store: ResponseStore, *, chain: ChainResolver | None = None) -> None:
        self._store = store
        #: T22: state-chain recovery + cycle guard (R-P0-29 / R-P1-31).  A
        #: tenant-narrowed resolver can be injected; the default uses the
        #: documented 64 / 2000 / 200k ceilings.
        self._chain = chain or ChainResolver(store)

    async def resolve_chain(
        self,
        previous_response_id: str,
        *,
        workspace_id: str = "",
    ) -> ChainResolution:
        """Expose chain recovery to the request builder (T24 upstream wiring)."""
        return await self._chain.resolve_chain(previous_response_id, workspace_id)

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
            return await endpoints.create(
                self._store,
                workspace_id=workspace_id,
                body=body,
                chain=self._chain,
            )
        if ep is ResponsesEndpoint.RETRIEVE:
            return await endpoints.retrieve(self._store, workspace_id=workspace_id, response_id=rid)
        if ep is ResponsesEndpoint.DELETE:
            return await endpoints.delete(self._store, workspace_id=workspace_id, response_id=rid)
        if ep is ResponsesEndpoint.CANCEL:
            return await endpoints.cancel(self._store, workspace_id=workspace_id, response_id=rid)
        if ep is ResponsesEndpoint.COMPACT:
            return await endpoints.compact(self._store, workspace_id=workspace_id, body=body)
        if ep is ResponsesEndpoint.INPUT_ITEMS:
            # GET /input_items 的分页参数来自 query string；非法值必须返回
            # 标准 400，而不是让 int() 抛出未处理 ValueError (T22)。
            after, limit, page_err = _parse_pagination(body)
            if page_err is not None:
                return page_err
            return await endpoints.input_items(
                self._store,
                workspace_id=workspace_id,
                response_id=rid,
                after=after,
                limit=limit,
            )
        # Defensive: every enum value is handled above.
        return to_error_object(
            message=f"unhandled endpoint: {ep}",
            code="not_implemented",
            status=501,
        )


def _parse_pagination(
    body: dict[str, Any] | None,
) -> tuple[int, int, tuple[int, dict[str, Any]] | None]:
    """Parse ``after`` / ``limit`` from the merged (query + body) mapping.

    Returns ``(after, limit, None)`` on success, or ``(0, 0, error_tuple)`` with
    a standard 400 response when a value is present but not a valid integer
    (T22: GET query params must never raise an uncaught ``ValueError``).

    ``after == -1`` is the internal "start from the beginning" cursor that the
    pagination loop echoes back on the first page; clients may send it too.
    Anything below ``-1`` is a client error.
    """
    body = body or {}
    after = -1
    limit = DEFAULT_PAGE_LIMIT
    raw_after = body.get("after")
    if raw_after is not None and raw_after != "":
        try:
            after = int(raw_after)
        except (TypeError, ValueError):
            return (
                0,
                0,
                to_error_object(
                    message=f"invalid 'after' value: {raw_after!r}; expected an integer",
                    code="invalid_request_error",
                    status=400,
                    param="after",
                ),
            )
        if after < -1:
            return (
                0,
                0,
                to_error_object(
                    message="'after' must be -1 or a non-negative integer",
                    code="invalid_request_error",
                    status=400,
                    param="after",
                ),
            )
    raw_limit = body.get("limit")
    if raw_limit is not None and raw_limit != "":
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return (
                0,
                0,
                to_error_object(
                    message=f"invalid 'limit' value: {raw_limit!r}; expected an integer",
                    code="invalid_request_error",
                    status=400,
                    param="limit",
                ),
            )
        if limit < 1:
            return (
                0,
                0,
                to_error_object(
                    message="'limit' must be a positive integer",
                    code="invalid_request_error",
                    status=400,
                    param="limit",
                ),
            )
    return after, limit, None


__all__ = ["ResponsesV3Handler"]
