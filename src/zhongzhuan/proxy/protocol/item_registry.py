"""Responses Bridge v3 versioned item registry (T10).

The 18 official Responses item types, each with a small handler that knows how
to:
* ``parse`` -- a raw wire object into a :class:`~.responses_models.NormalizedItem`
  (binding ``id``, ``seq``, ``item_type``, ``role`` and a canonical ``payload``);
* ``serialize`` -- a :class:`NormalizedItem` back to the official wire shape;
* ``redact`` -- a wire object before it is persisted or replayed, so reasoning
  items keep metadata only and never leak raw text (R-P0-14 / R-P1-29).

The registry is the single source of truth for the 18 item types enumerated in
:attr:`~.responses_models.ItemType`.  It imports only from
:mod:`.responses_models` and :mod:`.responses_errors`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .responses_models import (
    ItemType,
    NormalizedItem,
    canonical_json,
    coerce_enum,
    enum_values,
)

# ---------------------------------------------------------------------------
# 1. Item type constants (wire strings)
# ---------------------------------------------------------------------------

#: The complete set of official Responses item type strings.
ITEM_TYPES: tuple[str, ...] = tuple(enum_values(ItemType))

#: Item types that carry a ``role`` (message items).
MESSAGE_ITEM_TYPES: frozenset[str] = frozenset({"message"})

#: Item types whose ``payload`` must be redacted to metadata only (reasoning).
REDACTED_ITEM_TYPES: frozenset[str] = frozenset({"reasoning"})

#: Item types that are output-only (never valid as inputs).
OUTPUT_ONLY_ITEM_TYPES: frozenset[str] = frozenset({
    "output_text",
    "reasoning",
    "function_call",
    "file_search_call",
    "web_search_call",
    "computer_call",
    "code_interpreter_call",
    "image_generation_call",
    "local_shell_call",
    "mcp_call",
    "mcp_list_tools",
    "mcp_approval_request",
    "mcp_approval_response",
})


# ---------------------------------------------------------------------------
# 2. Item handler
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemHandler:
    """Per-item-type parse/serialize/redact behaviour."""

    item_type: str
    #: Whether the item is a valid input item (``False`` for output-only types).
    is_input: bool = True
    #: Whether the item carries a ``role``.
    has_role: bool = False
    #: Whether the payload must be redacted to metadata only.
    redact_payload: bool = False
    #: Optional role extraction from a raw wire object.
    role_extractor: Callable[[dict[str, Any]], str] | None = None


def _role_of(item: dict[str, Any]) -> str:
    return str(item.get("role") or "")


def _call_id_role(item: dict[str, Any]) -> str:
    # function_call / function_call_output carry no role; caller_id is separate.
    return ""


#: Registry keyed by official item type string (exactly the 18 ItemType values).
ITEM_REGISTRY: dict[str, ItemHandler] = {
    "message": ItemHandler("message", is_input=True, has_role=True, role_extractor=_role_of),
    "reasoning": ItemHandler("reasoning", is_input=False, redact_payload=True),
    "function_call": ItemHandler("function_call", is_input=False),
    "function_call_output": ItemHandler("function_call_output", is_input=True),
    "custom_tool_call": ItemHandler("custom_tool_call", is_input=True),
    "custom_tool_call_output": ItemHandler("custom_tool_call_output", is_input=True),
    "file_search_call": ItemHandler("file_search_call", is_input=False),
    "web_search_call": ItemHandler("web_search_call", is_input=False),
    "computer_call": ItemHandler("computer_call", is_input=False),
    "computer_call_output": ItemHandler("computer_call_output", is_input=True),
    "code_interpreter_call": ItemHandler("code_interpreter_call", is_input=False),
    "image_generation_call": ItemHandler("image_generation_call", is_input=False),
    "local_shell_call": ItemHandler("local_shell_call", is_input=False),
    "local_shell_call_output": ItemHandler("local_shell_call_output", is_input=True),
    "mcp_call": ItemHandler("mcp_call", is_input=False),
    "mcp_list_tools": ItemHandler("mcp_list_tools", is_input=False),
    "mcp_approval_request": ItemHandler("mcp_approval_request", is_input=False),
    "mcp_approval_response": ItemHandler("mcp_approval_response", is_input=False),
}


def get_handler(item_type: str) -> ItemHandler | None:
    """Return the :class:`ItemHandler` for ``item_type`` (``None`` if unknown)."""
    return ITEM_REGISTRY.get(item_type)


def is_known_item_type(item_type: str) -> bool:
    """Whether ``item_type`` is a registered Responses item type."""
    return item_type in ITEM_REGISTRY


def is_input_item(item_type: str) -> bool:
    """Whether ``item_type`` is a valid input item."""
    handler = ITEM_REGISTRY.get(item_type)
    return handler.is_input if handler else False


def item_role(item: dict[str, Any]) -> str:
    """Extract the ``role`` from a raw wire item (``""`` when none)."""
    handler = ITEM_REGISTRY.get(str(item.get("type") or ""))
    if handler and handler.role_extractor:
        return handler.role_extractor(item)
    return ""


# ---------------------------------------------------------------------------
# 3. Parse / serialize / redact
# ---------------------------------------------------------------------------


def parse_item(raw: Mapping[str, Any], seq: int) -> NormalizedItem | None:
    """Parse a raw wire item into a :class:`NormalizedItem`.

    Returns ``None`` for unrecognised item types (caller decides whether that
    is a hard error or a dropped item).  ``payload`` is the canonical JSON
    object with ``id``/``seq``/``type``/``role`` bound and everything else kept
    as-is (reasoning payloads are redacted via :func:`redact_item`).
    """
    if not isinstance(raw, Mapping):
        return None
    item_type = str(raw.get("type") or "")
    if not is_known_item_type(item_type):
        return None

    item_id = str(raw.get("id") or "")
    role = item_role(dict(raw))
    payload: dict[str, Any] = dict(raw)
    payload["type"] = item_type
    payload["id"] = item_id
    payload["seq"] = seq
    if role:
        payload["role"] = role

    handler = get_handler(item_type)
    redacted = bool(handler and handler.redact_payload)
    if redacted:
        payload = redact_item(payload)

    return NormalizedItem(
        id=item_id,
        seq=seq,
        item_type=item_type,
        role=role,
        payload=payload,
        redacted=redacted,
    )


def serialize_item(item: NormalizedItem) -> dict[str, Any]:
    """Serialize a :class:`NormalizedItem` back to the official wire shape.

    The wire object is ``item.payload`` minus the internal ``seq`` bookkeeping
    (``seq`` is a store cursor, not a wire field).  ``id``/``type``/``role`` are
    preserved from the payload.
    """
    wire = dict(item.payload)
    wire.pop("seq", None)
    return wire


def redact_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a wire item to metadata only.

    Reasoning items keep ``id``/``type``/``summary`` size/budget metadata but
    drop every raw text blob (``content``, ``summary[*].text``).  This is the
    only place that guarantees reasoning text never reaches the store or the
    replay path (R-P0-14 / R-P1-29 / R-P1-40).
    """
    out: dict[str, Any] = dict(raw)
    item_type = str(out.get("type") or "")
    if item_type == "reasoning":
        # Keep structural summary metadata, drop the text bodies.
        summary = out.get("summary")
        if isinstance(summary, list):
            out["summary"] = [
                {k: v for k, v in s.items() if k != "text"}
                for s in summary if isinstance(s, dict)
            ]
        for drop_key in ("content", "text", "encrypted_content"):
            out.pop(drop_key, None)
    return out


def parse_input_items(input_val: Any, *, start_seq: int = 0) -> list[NormalizedItem]:
    """Parse a Responses ``input`` field (str or list) into normalized items.

    A plain string is wrapped into a single user message item.  A list is
    parsed item-by-item; unrecognised entries are skipped (the caller decides
    whether they are a hard error via :func:`is_known_item_type`).
    """
    items: list[NormalizedItem] = []
    if isinstance(input_val, str):
        text = input_val.strip() or "..."
        items.append(NormalizedItem(
            id="",
            seq=start_seq,
            item_type="message",
            role="user",
            payload={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        ))
        return items

    if not isinstance(input_val, list):
        return items

    for idx, raw in enumerate(input_val):
        if not isinstance(raw, Mapping):
            continue
        parsed = parse_item(raw, start_seq + idx)
        if parsed is not None:
            items.append(parsed)
    return items


def change_reasoning_content(reasoning: dict[str, Any], *, with_text: bool) -> dict[str, Any]:
    """Return a copy of a reasoning item with/without the raw text retained.

    Used by the turn bridge (T17) to decide whether reasoning text is kept for
    downstream emitters or dropped (metadata-only) for persistence.
    """
    out = dict(reasoning)
    if not with_text:
        out = redact_item(out)
    return out


__all__ = [
    "ITEM_TYPES",
    "MESSAGE_ITEM_TYPES",
    "REDACTED_ITEM_TYPES",
    "OUTPUT_ONLY_ITEM_TYPES",
    "ItemHandler",
    "ITEM_REGISTRY",
    "get_handler",
    "is_known_item_type",
    "is_input_item",
    "item_role",
    "parse_item",
    "serialize_item",
    "redact_item",
    "parse_input_items",
    "change_reasoning_content",
]