"""State-chain recovery and cycle guarding (T22 / R-P0-29 / R-P1-31 / R-P1-40).

``previous_response_id`` turns the stateless Responses API into a linked list of
turns.  Walking that list is the only way to rebuild the visible context of a
conversation, and it is also the easiest way to hang the whole proxy: a client
that stores a self-referencing id, an A->B->A loop, or a 100k-turn chain would
otherwise spin forever or blow the context budget.

:class:`ChainResolver` walks the chain **backwards** (child -> root) with four
independent guards mandated by R-P0-29:

1. **self-reference** -- a record whose ``previous_response_id`` equals its own
   id is rejected outright;
2. **ancestor cycle** -- every visited id is kept in a ``visited`` set, so
   ``A->B->A`` and ``A->B->C->A`` are caught on the repeat visit;
3. **max depth** -- default 64 ancestors, tenant-narrowable via the constructor
   (narrowing only: :meth:`resolve_chain` takes ``min(call, tenant)``);
4. **recovery budget** -- ``max_items`` / ``max_tokens`` cap how much history a
   single request may drag back in.

Every guard, plus a missing / deleted / cross-tenant parent, produces a
**standard Responses error object** (see :func:`chain_error_response`).  The
chain is *never* silently degraded to a stateless request -- that would answer
a follow-up question with no context and look like a model regression.

Reasoning handling (铁律 1 / R-P1-31 / R-P1-40): recovered ``reasoning`` items
are dropped from :attr:`ChainResolution.items` entirely, so consumed reasoning
text can never be replayed into the next upstream Chat/Anthropic history.  The
redacted ``{id,type,status,summary,...}`` metadata placeholder stays in the
store and is still surfaced by ``retrieve`` / ``input_items`` for API-object
completeness.

``instructions`` is deliberately **not** inherited from ancestors (R-P1-31):
only conversation *items* are recovered, never request-level knobs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..proxy.protocol.item_registry import (
    parse_input_items,
    parse_item,
    serialize_item,
)
from ..proxy.protocol.responses_errors import to_error_response
from ..proxy.protocol.responses_models import (
    ErrorClass,
    NormalizedItem,
    TerminalReason,
)
from ..store.response_store import ResponseRecord, ResponseStore

#: Default maximum number of ancestors walked for one request (R-P0-29).
DEFAULT_MAX_CHAIN_DEPTH: int = 64

#: Default cap on recovered items across the whole chain (R-P0-29).
DEFAULT_MAX_CHAIN_ITEMS: int = 2000

#: Default cap on recovered tokens across the whole chain (R-P0-29).
DEFAULT_MAX_CHAIN_TOKENS: int = 200_000

#: Item type whose text must never be replayed upstream (铁律 1 / R-P1-40).
REASONING_ITEM_TYPE: str = "reasoning"

#: Request key marking a compaction boundary: the walk stops *at* such a record
#: because its input already is the summarised prefix of everything before it.
#: Honest note: nothing writes this key yet -- ``/v1/responses/compact`` is a
#: 501 stub and T24/T28 owns the writer.  The read side is implemented here so
#: compaction does not need to touch the guard logic later.
COMPACT_BOUNDARY_KEY: str = "compact_boundary"

#: Crude token estimate divisor.  A real tokenizer is provider-specific and far
#: too heavy for a guard that only needs an order-of-magnitude bound.
_CHARS_PER_TOKEN: int = 4


@dataclass
class ChainResolution:
    """Outcome of one :meth:`ChainResolver.resolve_chain` call (§3.8).

    ``items`` is in **chronological** order (root turn first, parent turn last)
    and is guaranteed reasoning-free.  ``visited`` is in **walk** order (parent
    first, root last) -- it is the cycle-detection trace, not a replay order.

    DEVIATION (§3.8): ``message`` is added on top of the documented five fields.
    It is optional with a default, so the documented constructor still applies;
    it carries the human-readable reason into
    :func:`chain_error_response` instead of forcing every call site to
    re-derive one from ``error`` + ``terminal_reason``.
    """

    items: list[NormalizedItem] = field(default_factory=list)
    depth: int = 0
    visited: list[str] = field(default_factory=list)
    error: ErrorClass | None = None
    terminal_reason: TerminalReason | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        """Whether the chain resolved cleanly (no guard tripped)."""
        return self.error is None


class ChainResolver:
    """Walk ``previous_response_id`` chains safely (R-P0-29 / R-P1-31).

    The tenant-level ceilings passed to ``__init__`` may only **narrow** the
    per-call limits, never widen them, so a workspace policy cannot be
    escalated by a crafted request body.
    """

    def __init__(
        self,
        store: ResponseStore,
        *,
        max_depth: int = DEFAULT_MAX_CHAIN_DEPTH,
        max_items: int = DEFAULT_MAX_CHAIN_ITEMS,
        max_tokens: int = DEFAULT_MAX_CHAIN_TOKENS,
    ) -> None:
        self._store = store
        self._max_depth = max(1, int(max_depth))
        self._max_items = max(1, int(max_items))
        self._max_tokens = max(1, int(max_tokens))

    async def resolve_chain(
        self,
        response_id: str,
        ws: str,
        *,
        max_depth: int = DEFAULT_MAX_CHAIN_DEPTH,
        max_items: int = DEFAULT_MAX_CHAIN_ITEMS,
        max_tokens: int = DEFAULT_MAX_CHAIN_TOKENS,
    ) -> ChainResolution:
        """Recover the visible context of ``response_id`` within workspace ``ws``.

        ``response_id`` is the *parent* -- i.e. the ``previous_response_id`` of
        the request being built -- so the returned ``items`` are everything the
        model already saw, minus reasoning (R-P1-31).

        Returns a :class:`ChainResolution` whose ``error`` is set (and ``items``
        empty) when any R-P0-29 guard trips.  It never raises for bad client
        input and never falls back to an empty stateless context.
        """
        depth_cap = max(1, min(int(max_depth), self._max_depth))
        items_cap = max(1, min(int(max_items), self._max_items))
        tokens_cap = max(1, min(int(max_tokens), self._max_tokens))

        res = ChainResolution()
        current = str(response_id or "")
        if not current:
            return _fail(
                res,
                ErrorClass.INVALID_CLIENT_REQUEST,
                None,
                "previous_response_id must be a non-empty response id",
            )

        seen: set[str] = set()
        # Per-ancestor item buckets, collected child -> root; reversed at the end.
        buckets: list[list[NormalizedItem]] = []
        total_items = 0
        total_tokens = 0

        while current:
            if current in seen:
                return _fail(
                    res,
                    ErrorClass.INVALID_CLIENT_REQUEST,
                    TerminalReason.RESPONSE_CHAIN_CYCLE,
                    f"response chain cycle detected at {current}",
                )
            if res.depth >= depth_cap:
                return _fail(
                    res,
                    ErrorClass.INVALID_CLIENT_REQUEST,
                    TerminalReason.RESPONSE_CHAIN_TOO_DEEP,
                    f"response chain exceeds the maximum depth of {depth_cap}",
                )

            record = await self._store.get_response(current, workspace_id=ws)
            if record is None:
                # Missing, deleted, or owned by another workspace -- all three are
                # indistinguishable on purpose (no cross-tenant existence oracle).
                return _fail(
                    res,
                    ErrorClass.INVALID_CLIENT_REQUEST,
                    None,
                    f"previous_response_id {current} not found",
                )

            seen.add(current)
            res.visited.append(current)
            res.depth += 1

            # Read one item past the remaining budget so an oversized turn
            # trips the guard below instead of being silently truncated.
            bucket = await self._visible_items(record, limit=items_cap - total_items + 1)
            total_items += len(bucket)
            if total_items > items_cap:
                return _fail(
                    res,
                    ErrorClass.INVALID_CLIENT_REQUEST,
                    TerminalReason.RESPONSE_CHAIN_TOO_DEEP,
                    f"recovered chain exceeds the maximum of {items_cap} items",
                )
            total_tokens += sum(_estimate_tokens(it) for it in bucket)
            if total_tokens > tokens_cap:
                return _fail(
                    res,
                    ErrorClass.INVALID_CLIENT_REQUEST,
                    TerminalReason.RESPONSE_CHAIN_TOO_DEEP,
                    f"recovered chain exceeds the maximum of {tokens_cap} tokens",
                )
            buckets.append(bucket)

            parent = str(record.previous_response_id or "")
            if parent and parent == current:
                return _fail(
                    res,
                    ErrorClass.INVALID_CLIENT_REQUEST,
                    TerminalReason.RESPONSE_CHAIN_CYCLE,
                    f"response {current} references itself as previous_response_id",
                )
            if _is_compact_boundary(record):
                # The compacted record's own input is the summarised prefix, so
                # everything older is intentionally unreachable.
                break
            current = parent

        # Root-first chronological order, with a stable seq re-numbering so the
        # result reads like a single flat input array.
        ordered: list[NormalizedItem] = []
        for bucket in reversed(buckets):
            for item in bucket:
                ordered.append(_reseq(item, len(ordered)))
        res.items = ordered
        return res

    # -- internals -----------------------------------------------------------

    async def _visible_items(
        self, record: ResponseRecord, *, limit: int = DEFAULT_MAX_CHAIN_ITEMS,
    ) -> list[NormalizedItem]:
        """Items of one turn that the model may see again, reasoning excluded.

        A turn contributes its request input first, then its own output, which
        is the order the upstream saw them in.  ``limit`` is the caller's
        remaining item budget: reading beyond it is pointless because the
        budget guard is about to reject the whole resolution anyway.
        """
        limit = max(1, int(limit))
        raw: list[Any] = []
        stored_inputs = await self._store.list_input_items(
            record.response_id, after_seq=-1, limit=limit,
        )
        if stored_inputs:
            raw.extend(stored_inputs)
        else:
            # Records written by paths that only persist the raw request body
            # (or by an older build) still have their input under request.input.
            raw.extend(_normalize_input(_request_of(record).get("input")))

        outputs = record.output
        if not outputs:
            outputs = await self._store.list_output_items(
                record.response_id, limit=limit,
            )
        raw.extend(outputs or [])

        return normalize_history(raw)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def normalize_history(raw_items: list[Any]) -> list[NormalizedItem]:
    """Parse stored wire items into replayable, reasoning-free normalized items.

    ``parse_item`` already redacts reasoning payloads on the way in (R-P0-14);
    this drops the reasoning items altogether because their *text* must never
    be re-sent upstream and their metadata means nothing to a Chat/Anthropic
    history (铁律 1 / R-P1-40).
    """
    out: list[NormalizedItem] = []
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        item = parse_item(entry, len(out))
        if item is None or item.item_type == REASONING_ITEM_TYPE:
            continue
        out.append(item)
    return out


def build_upstream_input(
    resolution: ChainResolution, current_input: Any = None,
) -> list[dict[str, Any]]:
    """Flatten a resolved chain plus this turn's input into one wire array.

    The result is what a request builder feeds to the upstream translator.  It
    is guaranteed reasoning-free on both halves (R-P1-31 / R-P1-40) and it
    carries **no** inherited ``instructions``: only items travel down a chain.
    """
    wire: list[dict[str, Any]] = [
        serialize_item(it)
        for it in resolution.items
        if it.item_type != REASONING_ITEM_TYPE
    ]
    for item in parse_input_items(current_input, start_seq=len(wire)):
        if item.item_type == REASONING_ITEM_TYPE:
            continue
        wire.append(serialize_item(item))
    return wire


def chain_error_response(resolution: ChainResolution) -> tuple[int, dict[str, Any]]:
    """Render a failed :class:`ChainResolution` as a standard Responses error.

    R-P0-29 forbids degrading a broken chain into a stateless request, so this
    is the only sanctioned outcome for a tripped guard.  ``param`` points at
    ``previous_response_id`` because that is the field the client controls.
    """
    err = resolution.error or ErrorClass.INVALID_CLIENT_REQUEST
    message = resolution.message or "previous_response_id could not be resolved"
    if resolution.terminal_reason is not None:
        message = f"{message} ({resolution.terminal_reason.value})"
    return to_error_response(err, message, "previous_response_id")


def _fail(
    res: ChainResolution,
    err: ErrorClass,
    reason: TerminalReason | None,
    message: str,
) -> ChainResolution:
    res.items = []
    res.error = err
    res.terminal_reason = reason
    res.message = message
    return res


def _reseq(item: NormalizedItem, seq: int) -> NormalizedItem:
    """Return ``item`` renumbered to ``seq`` (``NormalizedItem`` is frozen)."""
    payload = dict(item.payload)
    payload["seq"] = seq
    return NormalizedItem(
        id=item.id,
        seq=seq,
        item_type=item.item_type,
        role=item.role,
        payload=payload,
        redacted=item.redacted,
    )


def _normalize_input(input_val: Any) -> list[Any]:
    """Coerce a stored ``request.input`` (str or list) into a list of wire items."""
    if isinstance(input_val, list):
        return list(input_val)
    if isinstance(input_val, str) and input_val.strip():
        return [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": input_val}],
        }]
    return []


def _request_of(record: ResponseRecord) -> dict[str, Any]:
    """``record.request`` as a dict (the column is free-form decoded JSON)."""
    return record.request if isinstance(record.request, dict) else {}


def _is_compact_boundary(record: ResponseRecord) -> bool:
    return bool(_request_of(record).get(COMPACT_BOUNDARY_KEY))


def _estimate_tokens(item: NormalizedItem) -> int:
    try:
        size = len(json.dumps(item.payload, ensure_ascii=False))
    except (TypeError, ValueError):
        size = 0
    return max(1, size // _CHARS_PER_TOKEN)


__all__ = [
    "DEFAULT_MAX_CHAIN_DEPTH",
    "DEFAULT_MAX_CHAIN_ITEMS",
    "DEFAULT_MAX_CHAIN_TOKENS",
    "REASONING_ITEM_TYPE",
    "COMPACT_BOUNDARY_KEY",
    "ChainResolution",
    "ChainResolver",
    "normalize_history",
    "build_upstream_input",
    "chain_error_response",
]
