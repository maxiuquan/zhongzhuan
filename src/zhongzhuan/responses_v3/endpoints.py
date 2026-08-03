"""The six Responses resource endpoint handlers (T21 / R-P1-28..33).

Each handler is a pure ``(handler, request) -> (status, body)`` coroutine with
no routing logic of its own; :class:`~.handler.ResponsesV3Handler` resolves the
endpoint and dispatches here.  They rely only on
:class:`~zhongzhuan.store.response_store.ResponseStore` and the schema mappers,
so they are trivially unit-testable.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from ..store.response_store import ResponseStore
from .schema import to_error_object, to_input_items_list, to_response_object

DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


def _new_response_id() -> str:
    return "resp_" + uuid.uuid4().hex


async def create(
    rs: ResponseStore, *, workspace_id: str, body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    store = bool(body.get("store", True))
    response_id = _new_response_id()
    model = str(body.get("model", ""))
    previous_response_id = str(body.get("previous_response_id", "") or "")
    now = int(time.time())
    if store:
        await rs.create_response(
            response_id=response_id,
            workspace_id=workspace_id,
            model=model,
            status="in_progress",
            previous_response_id=previous_response_id,
            background=bool(body.get("background", False)),
            request=body,
        )
    obj = {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "model": model,
        "status": "in_progress",
        "output": [],
        "usage": {},
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "metadata": body.get("metadata", {}),
        "previous_response_id": previous_response_id or None,
        "background": bool(body.get("background", False)),
        "tools": body.get("tools", []),
        "tool_choice": body.get("tool_choice", "auto"),
        "store": store,
        "stream": bool(body.get("stream", False)),
    }
    return 200, obj


async def retrieve(
    rs: ResponseStore, *, workspace_id: str, response_id: str,
) -> tuple[int, dict[str, Any]]:
    rec = await rs.get_response(response_id, workspace_id=workspace_id)
    if rec is None:
        return to_error_object(
            message=f"Response {response_id} not found", code="not_found", status=404,
        )
    return 200, to_response_object(rec, stored=True)


async def delete(
    rs: ResponseStore, *, workspace_id: str, response_id: str,
) -> tuple[int, dict[str, Any]]:
    ok = await rs.delete_response(response_id, workspace_id=workspace_id)
    if not ok:
        return to_error_object(
            message=f"Response {response_id} not found", code="not_found", status=404,
        )
    return 200, {"id": response_id, "object": "response", "deleted": True}


async def cancel(
    rs: ResponseStore, *, workspace_id: str, response_id: str,
) -> tuple[int, dict[str, Any]]:
    rec = await rs.get_response(response_id, workspace_id=workspace_id)
    if rec is None:
        return to_error_object(
            message=f"Response {response_id} not found", code="not_found", status=404,
        )
    await rs.set_cancelled(response_id, workspace_id=workspace_id)
    rec = await rs.get_response(response_id, workspace_id=workspace_id)
    return 200, to_response_object(rec, stored=True)


async def compact(
    rs: ResponseStore, *, workspace_id: str, body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    # Honest stub: compaction regenerates a summarised conversation via the
    # upstream, which is wired in T24/T28.  Return 501 (not 405) so the endpoint
    # is reachable and documented, not silently dropped.
    return to_error_object(
        message="compact is not implemented in the v3 skeleton (T24/T28)",
        code="not_implemented",
        status=501,
    )


async def input_items(
    rs: ResponseStore,
    *,
    workspace_id: str,
    response_id: str,
    after: int = -1,
    limit: int = DEFAULT_PAGE_LIMIT,
) -> tuple[int, dict[str, Any]]:
    rec = await rs.get_response(response_id, workspace_id=workspace_id)
    if rec is None:
        return to_error_object(
            message=f"Response {response_id} not found", code="not_found", status=404,
        )
    limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
    items = await rs.list_input_items(response_id, after_seq=after, limit=limit)
    has_more = len(items) == limit
    # Advance the cursor to the last returned seq (contiguous seqs) so the next
    # page continues where this one ended (OpenAI uses an item id cursor; this
    # skeleton uses seq, see T21 deviation note in schema.to_input_items_list).
    next_after = after + len(items)
    return 200, to_input_items_list(items, limit=limit, after_seq=next_after, has_more=has_more)


__all__ = ["create", "retrieve", "delete", "cancel", "compact", "input_items"]
