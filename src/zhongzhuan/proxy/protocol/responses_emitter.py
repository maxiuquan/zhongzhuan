"""Responses Bridge v3 event emitter (§5.6).

The :class:`ResponsesEventEmitter` is the single writer of Responses SSE
events for one response.  It owns:

* the **monotonic ``sequence_number``** (§4.2.8) -- every event carries one;
* the **explicit lifecycle state machine** ``INIT -> CREATED -> IN_PROGRESS ->
  STREAMING -> COMPLETING -> COMPLETED`` (plus the FAILED/INCOMPLETE/CANCELLED
  terminal states added in :mod:`responses_models`);
* **created/in_progress sent immediately** on connect, before the first
  upstream token (铁律 3);
* **paired added/done** per output item and content part;
* **exactly-once completed and ``[DONE]``**;
* **rejection of illegal transitions** -- a delta after ``COMPLETED``, a
  duplicate ``added`` for the same item, an append after ``done``, a duplicate
  ``completed`` -- each recorded and refused (§5.6);
* **SSE comment heartbeats** every ~10-15s (铁律 5), which never change the
  response state (R-P0-21).

This module imports only from :mod:`.responses_models` and
:mod:`.responses_errors` and stays free of any IO -- the caller writes the
returned ``bytes`` frames to the wire.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .responses_errors import to_incomplete_details
from .responses_models import (
    EmitterState,
    ItemStatus,
    ItemType,
    OutputItem,
    ResponseStatus,
    SSE_DONE_FRAME,
    SSE_HEARTBEAT_FRAME,
    TERMINAL_EMITTER_STATES,
    coerce_enum,
    is_legal_transition,
    transition_label,
)


@dataclass
class EmitterConfig:
    """Tunables for the emitter (§15 config)."""

    #: Terminal event to emit in compatibility mode (``completed``).
    compatibility_terminal_event: str = "completed"
    #: Heartbeat interval in seconds (0 disables heartbeats).
    heartbeat_seconds: float = 15.0
    #: Whether the upstream is a native Responses endpoint (affects some
    #: passthrough semantics; kept for forward-compat, not used by the state
    #: machine itself).
    native_passthrough: bool = False


@dataclass
class EmitStats:
    """Counters for observability (§11.1)."""

    events: int = 0
    heartbeats: int = 0
    illegal_transitions: int = 0
    items_added: int = 0
    items_done: int = 0
    bytes_written: int = 0


class ResponsesEventEmitter:
    """Deterministic, idempotent Responses SSE event writer.

    Frames are returned as ``bytes``; the caller writes them to the client.
    The emitter never blocks and never does IO itself.
    """

    def __init__(
        self,
        *,
        response_id: str,
        model: str = "",
        created_at: int | None = None,
        config: EmitterConfig | None = None,
    ) -> None:
        self.response_id = response_id
        self.model = model
        self.created_at = created_at if created_at is not None else int(time.time())
        self.config = config or EmitterConfig()
        self.state: EmitterState = EmitterState.INIT
        self.stats = EmitStats()
        self._seq = 0
        self._done_emitted = False
        self._completed_emitted = False
        self._open_items: set[str] = set()
        self._done_items: set[str] = set()
        self._illegal: list[str] = []

    # -- sequence ------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # -- transition ----------------------------------------------------------

    def _transition(self, dst: EmitterState) -> bool:
        """Transition state; record and refuse illegal moves (§5.6)."""
        if not is_legal_transition(self.state, dst):
            self.stats.illegal_transitions += 1
            self._illegal.append(transition_label(self.state, dst))
            return False
        self.state = dst
        return True

    # -- frame helpers -------------------------------------------------------

    def _frame(self, event_type: str, data: dict[str, Any]) -> bytes:
        data["sequence_number"] = self._next_seq()
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        frame = ("event: {0}\ndata: {1}\n\n".format(event_type, payload)).encode("utf-8")
        self.stats.events += 1
        self.stats.bytes_written += len(frame)
        return frame

    def _response_object(self, status: str, **extra: Any) -> dict[str, Any]:
        obj: dict[str, Any] = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created_at,
            "status": status,
            "model": self.model,
            "output": [],
            "error": None,
        }
        obj.update(extra)
        return obj

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> list[bytes]:
        """Emit ``response.created`` + ``response.in_progress`` immediately."""
        if self.state != EmitterState.INIT:
            return []
        self._transition(EmitterState.CREATED)
        frames: list[bytes] = [
            self._frame("response.created", {
                "type": "response.created",
                "response": self._response_object("in_progress"),
            }),
        ]
        self._transition(EmitterState.IN_PROGRESS)
        frames.append(self._frame("response.in_progress", {
            "type": "response.in_progress",
            "response": self._response_object("in_progress"),
        }))
        return frames

    def open_item(self, item: "OutputItem") -> list[bytes]:
        """Emit ``response.output_item.added`` for ``item`` (idempotent)."""
        if item.id in self._open_items or item.id in self._done_items:
            self.stats.illegal_transitions += 1
            self._illegal.append("duplicate_added:{0}".format(item.id))
            return []
        self._transition(EmitterState.STREAMING)
        self._open_items.add(item.id)
        self.stats.items_added += 1
        return [self._frame("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": item.output_index,
            "item": self._item_wire(item, status="in_progress"),
        })]

    def close_item(self, item: "OutputItem", *, status: str = "completed") -> list[bytes]:
        """Emit ``response.output_item.done`` for ``item`` (idempotent)."""
        if item.id in self._done_items:
            return []
        if item.id not in self._open_items:
            self.stats.illegal_transitions += 1
            self._illegal.append("close_without_open:{0}".format(item.id))
            return []
        wire = self._item_wire(item, status=status)
        self._open_items.discard(item.id)
        self._done_items.add(item.id)
        self.stats.items_done += 1
        return [self._frame("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": item.output_index,
            "item": wire,
        })]

    @staticmethod
    def _item_wire(item: "OutputItem", *, status: str) -> dict[str, Any]:
        """Build the official ``item`` wire object from an :class:`OutputItem`."""
        wire: dict[str, Any] = {
            "id": item.id,
            "type": item.item_type.value,
            "status": status,
        }
        if item.role:
            wire["role"] = item.role
        if item.call_id:
            wire["call_id"] = item.call_id
        if item.name:
            wire["name"] = item.name
        wire.update(item.extra)
        return wire

    def delta(self, event_type: str, data: dict[str, Any]) -> list[bytes]:
        """Emit a delta event; refuse any delta after a terminal state."""
        if self.state in TERMINAL_EMITTER_STATES:
            self.stats.illegal_transitions += 1
            self._illegal.append("delta_after_terminal:{0}".format(event_type))
            return []
        self._transition(EmitterState.STREAMING)
        data["type"] = event_type
        return [self._frame(event_type, data)]

    # -- terminal ------------------------------------------------------------

    def terminate(
        self,
        status: ResponseStatus,
        *,
        terminal_reason: str = "",
        incomplete_details: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> list[bytes]:
        """Emit the terminal response event and ``[DONE]`` (exactly once).

        ``status`` is the official ``response.status`` (``completed`` /
        ``failed`` / ``incomplete`` / ``cancelled``).  In compatibility mode a
        truncated stream still emits ``completed`` with ``incomplete_details``
        (Q2); strict mode emits ``failed``/``incomplete``.
        """
        if self._completed_emitted:
            return []
        terminal = coerce_enum(ResponseStatus, status, ResponseStatus.COMPLETED)
        emitter_state = {
            ResponseStatus.COMPLETED: EmitterState.COMPLETED,
            ResponseStatus.FAILED: EmitterState.FAILED,
            ResponseStatus.INCOMPLETE: EmitterState.INCOMPLETE,
            ResponseStatus.CANCELLED: EmitterState.CANCELLED,
        }.get(terminal, EmitterState.COMPLETED)

        self._transition(EmitterState.COMPLETING)

        # Close any still-open items so the stream is well-formed.
        frames: list[bytes] = []
        for item_id in list(self._open_items):
            frames += self._close_open_item(item_id)

        # Emit the terminal event (completed/failed/incomplete/cancelled).
        event_name = "response.{0}".format(terminal.value)
        response_obj = self._response_object(terminal.value)
        if incomplete_details:
            response_obj["incomplete_details"] = incomplete_details
        elif terminal == ResponseStatus.INCOMPLETE and not incomplete_details:
            response_obj["incomplete_details"] = to_incomplete_details(
                terminal_reason or "unknown", "incomplete"
            )
        if error:
            response_obj["error"] = error
        if terminal_reason:
            response_obj["terminal_reason"] = terminal_reason
        frames.append(self._frame(event_name, {
            "type": event_name,
            "response": response_obj,
        }))
        self._completed_emitted = True
        self._transition(emitter_state)

        # Exactly-once [DONE].
        if not self._done_emitted:
            frames.append(SSE_DONE_FRAME)
            self._done_emitted = True
        return frames

    def _close_open_item(self, item_id: str) -> list[bytes]:
        """Best-effort close of an item left open at termination."""
        self._done_items.add(item_id)
        self._open_items.discard(item_id)
        self.stats.items_done += 1
        return [self._frame("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"id": item_id, "status": "incomplete"},
        })]

    # -- heartbeat -----------------------------------------------------------

    def heartbeat(self) -> list[bytes]:
        """Emit an SSE comment heartbeat (never transitions state)."""
        self.stats.heartbeats += 1
        self.stats.bytes_written += len(SSE_HEARTBEAT_FRAME)
        return [SSE_HEARTBEAT_FRAME]

    # -- helpers -------------------------------------------------------------

    @property
    def done(self) -> bool:
        """Whether a terminal event + [DONE] have been emitted."""
        return self._done_emitted

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_EMITTER_STATES

    @property
    def illegal_transitions(self) -> list[str]:
        return list(self._illegal)

    def usage(self) -> dict[str, Any]:
        return {}


__all__ = [
    "EmitterConfig",
    "EmitStats",
    "ResponsesEventEmitter",
]