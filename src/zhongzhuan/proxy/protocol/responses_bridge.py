"""Responses Bridge v3 turn bridge (T17).

The :class:`ResponsesTurnBridge` is the orchestration layer of §2.10 / §4 总体
架构: it composes the four components that each own exactly one concern --

* :class:`~.sse_parser.SSEParser`        -- byte-level SSE framing (no decode
  assumptions, split-chunk safe, §3.4 / B10);
* :class:`~.turn_accumulator.TurnAccumulator` -- per-turn state accumulation
  (text / ephemeral reasoning / tool calls / output index allocator, §5.2-5.4);
* :class:`~.turn_accumulator.OutputIndexAllocator` -- the single global output
  index space (§5.4);
* :class:`~.responses_emitter.ResponsesEventEmitter` -- the single writer of
  Responses SSE events, owning the lifecycle state machine and the monotonic
  ``sequence_number`` (§5.6).

The bridge itself contains **no** framing or index logic -- it only drives the
four components in the right order.  Externally it exposes a deliberately small
surface: ``feed`` / ``afinish`` (plus ``finish_safely`` / ``done`` / ``usage``
for the legacy translator contract).

``responses.py`` is reduced to a thin compatibility facade that delegates to
this bridge (门面 + 组合, §2.10), so the existing
``ResponsesStreamTranslator`` signature and behaviour are preserved and the old
tests keep passing with zero modification.
"""

from __future__ import annotations

import json
import time

from .responses_emitter import ResponsesEventEmitter
from .responses_models import (
    ItemType,
    OutputItem,
    ReasoningEventMode,
    ResponseStatus,
    make_function_call_item_id,
)
from .sse_parser import SSEParser
from .turn_accumulator import TurnAccumulator

#: Reasoning event-name families per :data:`ReasoningEventMode` (Q1).
_REASONING_EVENT: dict[str, str] = {
    ReasoningEventMode.SUMMARY_TEXT.value: "reasoning_summary_text",
    ReasoningEventMode.TEXT.value: "reasoning_text",
}
_REASONING_PART_EVENT: dict[str, str] = {
    ReasoningEventMode.SUMMARY_TEXT.value: "reasoning_summary_part",
    ReasoningEventMode.TEXT.value: "reasoning_text_part",
}


class ResponsesTurnBridge:
    """One-stream orchestration of parser / accumulator / emitter.

    Drives a single Chat Completions SSE stream and translates it into
    Responses SSE.  Implements the same interface as the other stream
    translators used by the proxy: ``await feed(chunk) -> list[bytes]``,
    ``done`` (property), ``finish_safely()``, ``afinish()`` and ``usage``.

    Args:
        model: The upstream model name, echoed on the Responses response.
        reasoning_event_mode: :data:`ReasoningEventMode` value.  ``disabled``
            suppresses reasoning items entirely; ``reasoning_text`` emits the
            ``reasoning_text.*`` family; the default ``reasoning_summary_text``
            emits the ``reasoning_summary_text.*`` family (Q1).
        response_id: Optional explicit response id (else generated).
        created_at: Optional explicit ``created_at`` epoch (else now).
    """

    def __init__(
        self,
        *,
        model: str = "",
        reasoning_event_mode: str = ReasoningEventMode.SUMMARY_TEXT.value,
        response_id: str | None = None,
        created_at: int | None = None,
    ) -> None:
        self.model = model
        self.reasoning_event_mode = reasoning_event_mode or ReasoningEventMode.SUMMARY_TEXT.value
        self.response_id = response_id or f"resp_{int(time.time() * 1000)}"
        self.created_at = created_at if created_at is not None else int(time.time())

        self._parser = SSEParser()
        self._acc = TurnAccumulator(response_id=self.response_id)
        self._emitter = ResponsesEventEmitter(
            response_id=self.response_id,
            model=model,
            created_at=self.created_at,
        )
        self._finished = False
        self._started = False
        self.usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}

        # output_index -> item-id bookkeeping for well-formed close ordering.
        self._msg_done: dict[int, bool] = {}
        self._tool_done: dict[int, bool] = {}

    # ------------------------------------------------------------------
    # Interface (legacy translator contract)
    # ------------------------------------------------------------------

    @property
    def done(self) -> bool:
        return self._finished

    def finish_safely(self) -> list[bytes]:
        """Close all open items, emit the terminal event + ``[DONE]`` (once)."""
        if self._finished:
            return []
        return self._finish()

    async def afinish(self) -> list[bytes]:
        """Async close (documented ``afinish``); mirrors :meth:`finish_safely`."""
        return self.finish_safely()

    async def feed(self, chunk: bytes) -> list[bytes]:
        """Feed one upstream byte chunk; return the Responses frames to send."""
        if self._finished:
            return []
        out: list[bytes] = []
        for frame in self._parser.feed(chunk):
            if frame.is_done_sentinel():
                continue
            data_str = frame.data.strip()
            if not data_str:
                continue
            try:
                parsed = json.loads(data_str)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                out.extend(self._process(parsed))
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_started(self) -> list[bytes]:
        if self._started:
            return []
        self._started = True
        # response.created + response.in_progress immediately on connect
        # (铁律 3), the first event of the stream.
        return self._emitter.start()

    def _process(self, parsed: dict) -> list[bytes]:
        # Capture usage from the final usage-only chunk (include_usage).
        u = parsed.get("usage")
        if isinstance(u, dict):
            pt = u.get("prompt_tokens", 0) or 0
            ct = u.get("completion_tokens", 0) or 0
            if pt or ct:
                self.usage = {"prompt_tokens": pt, "completion_tokens": ct}

        choices = parsed.get("choices") or []
        if not choices:
            return []

        frames: list[bytes] = self._ensure_started()
        choice = choices[0]
        delta = choice.get("delta") or {}

        # -- reasoning -------------------------------------------------
        if delta.get("reasoning_content"):
            frames.extend(self._open_reasoning())
            frames.extend(self._emit_reasoning_delta(delta["reasoning_content"]))

        # -- content ----------------------------------------------------
        if delta.get("content"):
            content = delta["content"]
            frames.extend(self._handle_content(content))

        # -- tool calls -------------------------------------------------
        if delta.get("tool_calls"):
            frames.extend(self._close_current_message())
            for tc in delta["tool_calls"]:
                frames.extend(self._handle_tool_call(tc))

        # -- finish reason ----------------------------------------------
        if choice.get("finish_reason"):
            frames.extend(self._close_all())
            frames.extend(self._emit_completed())
        return frames

    # -- content ---------------------------------------------------------

    def _handle_content(self, content: str) -> list[bytes]:
        frames: list[bytes] = []
        # " thinking"/" response" wrapper (some providers).
        if " thinking" in content:
            content = content.replace(" thinking", "")
            frames.extend(self._open_reasoning())
        if " response" in content:
            parts = content.split(" response")
            think_part = parts[0]
            text_part = " response".join(parts[1:])
            if think_part:
                frames.extend(self._emit_reasoning_delta(think_part))
            frames.extend(self._close_reasoning())
            content = text_part
        if not content:
            return frames

        # Find or create the current message accumulator.
        msg = self._current_message()
        if msg is None:
            msg = self._acc.new_message(role="assistant")
            frames.extend(self._open_message(msg))
        msg.append(content)
        frames.extend(self._emit_output_text_delta(msg, content))
        return frames

    def _current_message(self):
        return self._acc.messages[-1] if self._acc.messages else None

    def _open_message(self, msg) -> list[bytes]:
        item = OutputItem(
            id=msg.item_id,
            output_index=msg.output_index,
            item_type=ItemType.MESSAGE,
            role="assistant",
        )
        frames = list(self._emitter.open_item(item))
        frames.extend(
            self._emitter.delta(
                "response.content_part.added",
                {
                    "item_id": msg.item_id,
                    "output_index": msg.output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""},
                },
            )
        )
        return frames

    def _emit_output_text_delta(self, msg, text: str) -> list[bytes]:
        return self._emitter.delta(
            "response.output_text.delta",
            {
                "item_id": msg.item_id,
                "output_index": msg.output_index,
                "content_index": 0,
                "delta": text,
                "logprobs": [],
            },
        )

    def _close_message(self, msg) -> list[bytes]:
        if self._msg_done.get(msg.output_index):
            return []
        self._msg_done[msg.output_index] = True
        frames: list[bytes] = []
        full = msg.text
        frames.extend(
            self._emitter.delta(
                "response.output_text.done",
                {
                    "item_id": msg.item_id,
                    "output_index": msg.output_index,
                    "content_index": 0,
                    "text": full,
                    "logprobs": [],
                },
            )
        )
        frames.extend(
            self._emitter.delta(
                "response.content_part.done",
                {
                    "item_id": msg.item_id,
                    "output_index": msg.output_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": full},
                },
            )
        )
        item = OutputItem(
            id=msg.item_id,
            output_index=msg.output_index,
            item_type=ItemType.MESSAGE,
            role="assistant",
            extra={
                "content": [
                    {
                        "type": "output_text",
                        "annotations": [],
                        "logprobs": [],
                        "text": full,
                    }
                ]
            },
        )
        frames.extend(self._emitter.close_item(item, status="completed"))
        return frames

    def _close_current_message(self) -> list[bytes]:
        msg = self._current_message()
        if msg is None:
            return []
        return self._close_message(msg)

    # -- reasoning --------------------------------------------------------

    def _reasoning_enabled(self) -> bool:
        return self.reasoning_event_mode != ReasoningEventMode.DISABLED.value

    def _open_reasoning(self) -> list[bytes]:
        if not self._reasoning_enabled():
            return []
        if self._acc.reasoning is not None:
            return []
        rea = self._acc.open_reasoning()
        frames: list[bytes] = []
        item = OutputItem(
            id=rea.item_id,
            output_index=rea.output_index,
            item_type=ItemType.REASONING,
        )
        frames.extend(self._emitter.open_item(item))
        event = _REASONING_PART_EVENT.get(self.reasoning_event_mode, "reasoning_summary_part")
        frames.extend(
            self._emitter.delta(
                f"response.{event}.added",
                {
                    "item_id": rea.item_id,
                    "output_index": rea.output_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            )
        )
        return frames

    def _emit_reasoning_delta(self, text: str) -> list[bytes]:
        if not self._reasoning_enabled():
            return []
        rea = self._acc.reasoning
        if rea is None:
            return []
        if not text:
            return []
        rea.append(text)
        event = _REASONING_EVENT.get(self.reasoning_event_mode, "reasoning_summary_text")
        return self._emitter.delta(
            f"response.{event}.delta",
            {
                "item_id": rea.item_id,
                "output_index": rea.output_index,
                "summary_index": 0,
                "delta": text,
            },
        )

    def _close_reasoning(self) -> list[bytes]:
        if not self._reasoning_enabled():
            return []
        rea = self._acc.reasoning
        if rea is None or rea.done:
            return []
        rea.mark_done()
        frames: list[bytes] = []
        event = _REASONING_EVENT.get(self.reasoning_event_mode, "reasoning_summary_text")
        part_event = _REASONING_PART_EVENT.get(self.reasoning_event_mode, "reasoning_summary_part")
        frames.extend(
            self._emitter.delta(
                f"response.{event}.done",
                {
                    "item_id": rea.item_id,
                    "output_index": rea.output_index,
                    "summary_index": 0,
                    "text": rea.text,
                },
            )
        )
        frames.extend(
            self._emitter.delta(
                f"response.{part_event}.done",
                {
                    "item_id": rea.item_id,
                    "output_index": rea.output_index,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": rea.text},
                },
            )
        )
        item = OutputItem(
            id=rea.item_id,
            output_index=rea.output_index,
            item_type=ItemType.REASONING,
            extra={"summary": [{"type": "summary_text", "text": rea.text}]},
        )
        frames.extend(self._emitter.close_item(item, status="completed"))
        return frames

    # -- tool calls -------------------------------------------------------

    def _handle_tool_call(self, tc: dict) -> list[bytes]:
        frames: list[bytes] = []
        tc_idx = tc.get("index", 0) or 0
        new_call_id = tc.get("id")
        func_name = tc.get("function", {}).get("name")

        acc = self._acc.tools.get(call_id=new_call_id or "", source_index=tc_idx)
        if acc is None:
            acc = self._acc.open_tool_call(
                call_id=new_call_id or "",
                source_index=tc_idx,
                name=func_name,
            )
            frames.extend(self._open_tool_call(acc))
        else:
            if func_name:
                acc.replace_name(func_name)
            if new_call_id:
                acc.bind_call_id(new_call_id)

        args = tc.get("function", {}).get("arguments")
        if args:
            acc.append_arguments(args)
            frames.extend(self._emit_tool_args_delta(acc, args))
        return frames

    def _tool_item_id(self, acc) -> str:
        """The stable Responses ``item.id`` of a tool call (P0-4).

        :class:`ToolCallCollection` fixes ``acc.item_id`` at creation time from
        ``response_id + output_index``.  The ``make_function_call_item_id``
        (``fc_{call_id}``) fallback only fires for accumulators built outside
        the collection (direct construction in unit tests); it preserves the
        historical shape rather than emitting an empty id.  Using ``acc.item_id``
        is what keeps ``output_item.added`` and ``output_item.done`` identical
        even when ``call_id`` binds late (P0-4 / AC-4.1).
        """
        return acc.item_id or make_function_call_item_id(acc.call_id)

    def _open_tool_call(self, acc) -> list[bytes]:
        item_id = self._tool_item_id(acc)
        frames = self._emitter.open_item(
            OutputItem(
                id=item_id,
                output_index=acc.output_index,
                item_type=ItemType.FUNCTION_CALL,
                call_id=acc.call_id,
                name=acc.name,
            )
        )
        return frames

    def _emit_tool_args_delta(self, acc, args: str) -> list[bytes]:
        item_id = self._tool_item_id(acc)
        return self._emitter.delta(
            "response.function_call_arguments.delta",
            {
                "item_id": item_id,
                "output_index": acc.output_index,
                "delta": args,
            },
        )

    def _close_tool_call(self, acc) -> list[bytes]:
        if self._tool_done.get(acc.output_index):
            return []
        item_id = self._tool_item_id(acc)
        frames: list[bytes] = []

        # 铁律 2: never emit a runnable function call for truncated / invalid
        # arguments.  Only a call whose arguments parse AND whose top level is a
        # JSON object may emit ``function_call_arguments.done`` + be closed as
        # ``completed``.  Anything else (empty / truncated / non-object) is left
        # incomplete: the client must never JSON.parse a partial fragment and
        # execute it as ``{}``.
        if acc.validate_arguments(require_object=True):
            self._tool_done[acc.output_index] = True
            frames.extend(
                self._emitter.delta(
                    "response.function_call_arguments.done",
                    {
                        "item_id": item_id,
                        "output_index": acc.output_index,
                        "arguments": acc.arguments,
                    },
                )
            )
            frames.extend(
                self._emitter.close_item(
                    OutputItem(
                        id=item_id,
                        output_index=acc.output_index,
                        item_type=ItemType.FUNCTION_CALL,
                        call_id=acc.call_id,
                        name=acc.name,
                        extra={"arguments": acc.arguments},
                    ),
                    status="completed",
                )
            )
        else:
            # Truncated / invalid arguments: close the item safely as incomplete
            # and NEVER emit ``arguments.done`` (R-P1-22).  Idempotent via the
            # same ``_tool_done`` guard so `_close_all` / finish cannot double-close.
            self._tool_done[acc.output_index] = True
            frames.extend(
                self._emitter.close_item(
                    OutputItem(
                        id=item_id,
                        output_index=acc.output_index,
                        item_type=ItemType.FUNCTION_CALL,
                        call_id=acc.call_id,
                        name=acc.name,
                        extra={"arguments": acc.arguments},
                    ),
                    status="incomplete",
                )
            )
        return frames

    # -- close / terminal -------------------------------------------------

    def _close_all(self) -> list[bytes]:
        frames: list[bytes] = []
        for msg in self._acc.messages:
            frames.extend(self._close_message(msg))
        frames.extend(self._close_reasoning())
        for acc in self._acc.tools.list_all():
            frames.extend(self._close_tool_call(acc))
        return frames

    def _emit_completed(self) -> list[bytes]:
        return self._emitter.terminate(ResponseStatus.COMPLETED)

    def _finish(self) -> list[bytes]:
        # Ensure created/in_progress were emitted even if the upstream died
        # before any parsed chunk (⑤; 铁律 3).
        frames: list[bytes] = self._ensure_started()
        frames.extend(self._close_all())
        frames.extend(self._emit_completed())
        # Release the ephemeral reasoning buffer (R-P1-04) at turn end.
        self._acc.release()
        self._finished = True
        return frames


__all__ = [
    "ResponsesTurnBridge",
]
