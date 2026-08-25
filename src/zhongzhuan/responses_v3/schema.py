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
    """Map a :class:`ResponseRecord` to the official ``response`` object.

    v3.2 整改：``instructions`` / ``metadata`` / ``tools`` 此前硬编码为
    ``null`` / ``{}`` / ``[]``——而 endpoints.create 落库时把整个请求体（含
    这三个字段）存进了 ``responses.request``。回包从存储行如实回显，客户端
    （Codex 会校验 metadata 往返）才不会看到「写进去的和读出来的不一样」。
    """
    request = record.request if isinstance(record.request, dict) else {}
    instructions = request.get("instructions")
    metadata = request.get("metadata")
    tools = request.get("tools")
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
        # R-P1-31: instructions 是 per-request 的，这里只是**回显本行落库值**，
        # 与链式继承无关（继承语义在 ChainResolver / 上游注入层）。
        "instructions": instructions if instructions is not None else None,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "previous_response_id": record.previous_response_id or None,
        "background": bool(record.background),
        "tools": tools if isinstance(tools, list) else [],
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
