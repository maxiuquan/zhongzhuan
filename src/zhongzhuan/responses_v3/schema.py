"""Response object <-> wire schema mapping (T21 / R-P1-28 / R-P1-30).

Maps the persisted :class:`~zhongzhuan.store.response_store.ResponseRecord` to
the official OpenAI Responses ``response`` object, and builds the paginated
``list`` object for ``input_items``.  Keeping the mapping here (separate from
the handler) means the contract is unit-testable without a store or server.
"""

from __future__ import annotations

from typing import Any

from ..store.response_store import ResponseRecord


def to_response_object(record: ResponseRecord, *, stored: bool = True) -> dict[str, Any]:
    """Map a :class:`ResponseRecord` to the official ``response`` object."""
    obj: dict[str, Any] = {
        "id": record.response_id,
        "object": "response",
        "created_at": record.created_at,
        "model": record.model,
        "status": record.status,
        "output": record.output,
        "usage": record.usage,
        "error": record.error or None,
        "incomplete_details": record.incomplete_details or None,
        "instructions": None,
        "metadata": {},
        "previous_response_id": record.previous_response_id or None,
        "background": bool(record.background),
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "temperature": None,
        "top_p": None,
        "max_output_tokens": None,
        "text": None,
        "truncation": None,
        "user": None,
        "store": stored,
        "include": [],
        "stream": False,
    }
    if record.terminal_reason:
        obj["incomplete_details"] = obj["incomplete_details"] or {}
        obj["incomplete_details"].setdefault("reason", record.terminal_reason)
    return obj


def to_input_items_list(
    items: list[dict[str, Any]],
    *,
    limit: int,
    after_seq: int,
    has_more: bool,
) -> dict[str, Any]:
    """Build the official ``list`` object for ``input_items`` pagination."""
    data = [dict(it) for it in items]
    first_id = data[0].get("id") if data else None
    last_id = data[-1].get("id") if data else None
    return {
        "object": "list",
        "data": data,
        "first_id": first_id,
        "last_id": last_id,
        "has_more": has_more,
        # Cursor echo (seq-based; OpenAI uses item id, see T21 deviation note).
        "limit": limit,
        "after": after_seq,
    }


def to_error_object(
    *,
    message: str,
    code: str = "invalid_request_error",
    status: int = 400,
    param: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return an ``(http_status, error_body)`` tuple in the official shape."""
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": code,
            "code": code,
        }
    }
    if param is not None:
        body["error"]["param"] = param
    return status, body


__all__ = ["to_response_object", "to_input_items_list", "to_error_object"]
