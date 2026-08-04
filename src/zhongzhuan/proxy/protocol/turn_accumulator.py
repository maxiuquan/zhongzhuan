"""Responses Bridge v3 turn-level accumulation (T11).

Hosts the three per-turn accumulators of §4.0 / §5.2 and the global output
index allocator of §5.4:

* :class:`TextAccumulator`        -- one message's text content.
* :class:`EphemeralReasoningAccumulator` -- reasoning output, **only in-memory
  for the current turn** (铁律 1: reasoning is out-only, never persisted or
  replayed into the next turn).
* :class:`OutputIndexAllocator`   -- the single global output-index space shared
  by message / reasoning / tool-call items (§5.4).
* :class:`TurnAccumulator`        -- the per-turn container that owns the above
  plus the :class:`~.tool_accumulator.ToolCallCollection`.

Design rules enforced here:
* Each output item is allocated its global ``output_index`` exactly once, on
  first creation (§5.4).  The upstream Chat choice index / tool index is never
  used directly as a Responses output index.
* Reasoning deltas are released as soon as the turn ends; nothing is retained
  for the next request (§5.2).
* All imports are limited to :mod:`.responses_models` and
  :mod:`.tool_accumulator` so this module stays cycle-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .responses_models import (
    ItemStatus,
    ItemType,
    OutputItem,
    make_function_call_item_id,
    make_message_item_id,
    make_reasoning_item_id,
)
from .tool_accumulator import ToolCallAccumulator, ToolCallCollection


# ---------------------------------------------------------------------------
# 1. Output index allocator (§5.4)
# ---------------------------------------------------------------------------


class OutputIndexAllocator:
    """Monotonic, never-reused global output index for one response."""

    def __init__(self, *, start: int = 0) -> None:
        self._next = start

    def next(self) -> int:
        """Allocate and return the next global output index."""
        idx = self._next
        self._next += 1
        return idx

    @property
    def next_index(self) -> int:
        return self._next

    def peek(self) -> int:
        """Return the next index without consuming it."""
        return self._next


# ---------------------------------------------------------------------------
# 2. Text accumulator
# ---------------------------------------------------------------------------


@dataclass
class TextAccumulator:
    """Accumulate one message's text across arbitrary deltas."""

    output_index: int
    item_id: str = ""
    text: str = ""
    added: bool = False
    done: bool = False

    def append(self, fragment: str) -> None:
        if fragment:
            self.text += fragment

    def mark_added(self) -> None:
        self.added = True

    def mark_done(self) -> None:
        self.done = True


# ---------------------------------------------------------------------------
# 3. Ephemeral reasoning accumulator (§5.2)
# ---------------------------------------------------------------------------


@dataclass
class EphemeralReasoningAccumulator:
    """Reasoning output for the current turn only (铁律 1).

    Released at turn end; never persisted, never replayed into the next
    upstream request, never part of the sticky-session fingerprint.
    """

    output_index: int
    item_id: str = ""
    text: str = ""
    added: bool = False
    done: bool = False

    def append(self, fragment: str) -> None:
        if fragment:
            self.text += fragment

    def mark_added(self) -> None:
        self.added = True

    def mark_done(self) -> None:
        self.done = True

    def clear(self) -> None:
        """Release the reasoning text (R-P1-04)."""
        self.text = ""


# ---------------------------------------------------------------------------
# 4. Turn accumulator
# ---------------------------------------------------------------------------


@dataclass
class TurnAccumulator:
    """The per-turn container the stream pipeline drives.

    Single owner of the text / reasoning / tool accumulators and the output
    index allocator for one response.  ``response_id`` is bound at construction
    so synthetic call ids and item ids are stable and reproducible.
    """

    response_id: str = ""
    require_json_object_arguments: bool = True

    messages: list[TextAccumulator] = field(default_factory=list)
    reasoning: EphemeralReasoningAccumulator | None = None
    tools: ToolCallCollection = field(default_factory=ToolCallCollection)
    allocator: OutputIndexAllocator = field(default_factory=OutputIndexAllocator)

    def __post_init__(self) -> None:
        if self._tools_unset():
            self.tools = ToolCallCollection(
                response_id=self.response_id,
                require_json_object=self.require_json_object_arguments,
            )

    def _tools_unset(self) -> bool:
        # ToolCallCollection is a mutable default; detect a fresh instance.
        return not getattr(self.tools, "tools_by_call_id", None) and not getattr(
            self.tools, "tools_by_source_index", None
        )

    # -- messages ------------------------------------------------------------

    def new_message(self, *, role: str = "assistant") -> TextAccumulator:
        idx = self.allocator.next()
        acc = TextAccumulator(
            output_index=idx,
            item_id=make_message_item_id(self.response_id, idx),
        )
        self.messages.append(acc)
        return acc

    def open_reasoning(self) -> EphemeralReasoningAccumulator:
        idx = self.allocator.next()
        acc = EphemeralReasoningAccumulator(
            output_index=idx,
            item_id=make_reasoning_item_id(self.response_id, idx),
        )
        self.reasoning = acc
        return acc

    def open_tool_call(
        self,
        *,
        call_id: str = "",
        source_index: int | None = None,
        name: str | None = None,
    ) -> ToolCallAccumulator:
        idx = self.allocator.next()
        acc = self.tools.ensure(
            output_index=idx,
            call_id=call_id,
            source_index=source_index,
        )
        if name:
            acc.replace_name(name)
        return acc

    def release(self) -> None:
        """Free ephemeral reasoning (R-P1-04) at turn end."""
        if self.reasoning is not None:
            self.reasoning.clear()
            self.reasoning = None

    def all_items(self) -> list[Any]:
        """All output items in creation order (message/reasoning/tools)."""
        items: list[Any] = list(self.messages)
        if self.reasoning is not None:
            items.append(self.reasoning)
        items.extend(self.tools.list_all())
        items.sort(key=lambda it: it.output_index)
        return items

    def open_items(self) -> list[OutputItem]:
        """Items already announced with ``output_item.added`` but not yet done.

        Read-only view used by the T23 circuit breaker to close a stream that
        is being torn down mid-flight: an ``added`` without its ``done`` is a
        malformed Responses stream, so every one of these must be closed before
        the terminal event.

        "Open" means *added and not done* -- an item that was never announced
        must not be closed, because the emitter rejects a ``done`` it never saw
        an ``added`` for.  Returned in ``output_index`` order so the teardown
        is deterministic.
        """
        out: list[OutputItem] = []
        for msg in self.messages:
            if msg.added and not msg.done:
                out.append(
                    OutputItem(
                        id=msg.item_id,
                        output_index=msg.output_index,
                        item_type=ItemType.MESSAGE,
                        status=ItemStatus.IN_PROGRESS,
                        role="assistant",
                    )
                )
        rsn = self.reasoning
        if rsn is not None and rsn.added and not rsn.done:
            out.append(
                OutputItem(
                    id=rsn.item_id,
                    output_index=rsn.output_index,
                    item_type=ItemType.REASONING,
                    status=ItemStatus.IN_PROGRESS,
                )
            )
        for tool in self.tools.list_all():
            if tool.item_added and not tool.item_done:
                out.append(
                    OutputItem(
                        id=make_function_call_item_id(tool.call_id),
                        output_index=tool.output_index,
                        item_type=ItemType.FUNCTION_CALL,
                        status=ItemStatus.IN_PROGRESS,
                        call_id=tool.call_id,
                        name=tool.name,
                    )
                )
        out.sort(key=lambda it: it.output_index)
        return out


__all__ = [
    "OutputIndexAllocator",
    "TextAccumulator",
    "EphemeralReasoningAccumulator",
    "TurnAccumulator",
]
