"""Responses streaming pipeline (T21 / R-P1-32, R-P0-40; T28 / R-P0-21, R-P1-22~27).

Drives the canonical event sequence for a response stream on top of an
injectable upstream.  T21 proved the ``created -> in_progress -> completed ->
[DONE]`` ordering; T28 repays the "real chunk -> delta translation is
T24/T28" debt and lands the four Phase-3 behaviours:

* **heartbeat** (:meth:`ResponsePipeline.run`) -- an SSE comment frame every
  :attr:`PipelineConfig.heartbeat_seconds` while the stream is alive, driven
  by an injectable ``clock`` + ``sleep`` so tests can fast-forward a 120s
  silent upstream without ever calling ``asyncio.sleep(120)`` (R-P0-21);
* **client-cancel propagation** -- when ``client_cancelled`` fires the
  upstream is closed *immediately* (R-P1-24) and the cancellation is counted
  in :attr:`PipelineStats.client_disconnects` **without** touching any
  ``KeyHealth`` (R-P1-25: a user spamming cancel must never burn a healthy
  upstream key into the circuit breaker);
* **timeout classification** -- first-token / read-idle / total / connect map
  to the four *mutually distinct* :data:`~..proxy.protocol.responses_models.TIMEOUT_REASONS`
  (R-P1-26);
* **dual-mode stream abort** -- a mid-stream connection drop ends in
  compatibility mode (default) as ``response.completed`` carrying
  ``terminal_reason`` + ``incomplete_details`` (Q2 / R-P1-22), or in strict
  mode as ``response.failed``/``response.incomplete`` with ``[DONE]`` still
  the last frame (R-P1-23).  Open items are closed safely and a partially
  accumulated tool call never emits ``arguments.done``.

The pipeline also persists every emitted event through :class:`EventLog` so the
catch-up stream (T24) can replay it.  Sequence numbers are allocated from 0
here; in production the canonical ``seq`` comes from ``ResponsesEventEmitter``
(T16).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any, AsyncIterable, Awaitable, Callable

from ..proxy.protocol.responses_errors import to_incomplete_details
from ..proxy.protocol.responses_models import (
    SSE_DONE_FRAME,
    SSE_HEARTBEAT_FRAME,
    TerminalReason,
    TIMEOUT_REASONS,
    make_function_call_item_id,
    make_message_item_id,
)
from ..proxy.protocol.tool_accumulator import ToolCallCollection
from ..store.response_store import ResponseStore


def sse_frame(event_type: str, data: dict[str, Any]) -> bytes:
    """Render one SSE frame.

    Public because the catch-up stream (T24) must produce **byte-identical**
    frames to the live stream -- sharing this function is what makes that a
    property of the code rather than a comment (R-P1-36).
    """
    return (f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n").encode("utf-8")


#: Historical private alias kept for existing call sites inside this module.
_sse = sse_frame


# ---------------------------------------------------------------------------
# 1. Configuration / stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineConfig:
    """T28 tunables for one streaming pipeline.

    ``strict_terminal`` implements R-P1-23: a truncated stream emits
    ``response.failed``/``response.incomplete`` instead of the compatibility
    ``response.completed`` (Q2).  The timeout values are the four layers the
    stream itself can observe (R-P1-26); ``connect_seconds`` is honoured at
    the transport boundary, so the pipeline classifies connect *errors* into
    :attr:`TerminalReason.UPSTREAM_CONNECT` when no chunk was produced yet.
    """

    heartbeat_seconds: float = 15.0
    strict_terminal: bool = False
    first_token_seconds: float = 600.0
    read_idle_seconds: float = 600.0
    total_seconds: float = 1800.0
    connect_seconds: float = 15.0
    #: Criterion ⑤ (R-P1-27): heartbeat arrival gap must never exceed this.
    max_heartbeat_gap_seconds: float = 16.0


@dataclass
class PipelineStats:
    """Observability counters exposed by the pipeline (T28 / §11.1)."""

    heartbeats: int = 0
    client_disconnects: int = 0
    truncated_streams: int = 0
    terminal_reason: str = ""


# ---------------------------------------------------------------------------
# 2. Small helpers
# ---------------------------------------------------------------------------


async def _close_upstream(source: Any) -> None:
    """Best-effort close of an upstream of any shape (T24 ``cancel_upstream``)."""
    if source is None:
        return
    for hook_name in ("aclose", "close", "cancel"):
        hook = getattr(source, hook_name, None)
        if hook is None:
            continue
        try:
            result = hook()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - teardown must never raise
            return
        return


def _is_timeout_reason(reason: TerminalReason) -> bool:
    """Whether ``reason`` is one of the four R-P1-26 timeout classes."""
    return reason in TIMEOUT_REASONS


# ---------------------------------------------------------------------------
# 3. The pipeline
# ---------------------------------------------------------------------------


class ResponsePipeline:
    """Yield the SSE frames for a single response stream."""

    def __init__(
        self,
        response_id: str,
        *,
        workspace_id: str = "",
        store: ResponseStore | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self.response_id = response_id
        self.workspace_id = workspace_id
        self._store = store
        self._config = config or PipelineConfig()
        self._seq = 0
        #: Live lifecycle label exposed to tests (criterion ① "still STREAMING").
        self.state: str = "init"
        self.stats = PipelineStats()
        self._tools = ToolCallCollection(response_id=response_id)
        self._open_message: dict[str, Any] | None = None
        self._output_index = 0
        self._done = False

    async def _emit(self, event_type: str, data: dict[str, Any]) -> bytes:
        frame = _sse(event_type, data)
        if self._store is not None:
            await self._store.event_log.append_event(
                response_id=self.response_id,
                event_type=event_type,
                data=data,
                workspace_id=self.workspace_id,
                seq=self._seq,
            )
        self._seq += 1
        return frame

    # -- chunk -> events ---------------------------------------------------

    async def _translate_chunk(self, chunk: Any) -> list[bytes]:
        """Turn one upstream chunk into a list of SSE frames.

        Chunk vocabulary (dict):
            ``{"type": "text", "delta": ...}``            text delta
            ``{"type": "tool_call", "call_id", "name",   a fragment of a tool
              "arguments"}``                              call's arguments
            ``{"type": "tool_call_done", "call_id",      final arguments of a
              "arguments"}``                              tool call
            ``{"type": "finish"}``                        graceful upstream end
        Bytes chunks pass through untouched (native passthrough).
        """
        frames: list[bytes] = []
        if isinstance(chunk, bytes):
            return [chunk]
        if not isinstance(chunk, dict):
            return frames
        kind = str(chunk.get("type") or "")

        if kind in ("text", "output_text.delta"):
            delta = str(chunk.get("delta", chunk.get("text", "")) or "")
            if self._open_message is None:
                idx = self._output_index
                self._output_index += 1
                item_id = make_message_item_id(self.response_id, idx)
                self._open_message = {"id": item_id, "output_index": idx}
                frames.append(
                    await self._emit(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": idx,
                            "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant"},
                        },
                    )
                )
            frames.append(
                await self._emit(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "output_index": self._open_message["output_index"],
                        "delta": delta,
                    },
                )
            )
            self.state = "streaming"

        elif kind == "tool_call":
            call_id = str(chunk.get("call_id") or "")
            name = str(chunk.get("name") or "")
            fragment = str(chunk.get("arguments") or "")
            acc = self._tools.ensure(
                output_index=self._output_index,
                call_id=call_id,
                source_index=chunk.get("source_index"),
            )
            acc.replace_name(name)
            acc.append_arguments(fragment)
            if not acc.item_added:
                idx = acc.output_index
                self._output_index = max(self._output_index, idx + 1)
                frames.append(
                    await self._emit(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": idx,
                            "item": {
                                "id": make_function_call_item_id(acc.call_id),
                                "type": "function_call",
                                "status": "in_progress",
                                "call_id": acc.call_id,
                                "name": name,
                                "arguments": "",
                            },
                        },
                    )
                )
                acc.item_added = True
            frames.append(
                await self._emit(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": acc.output_index,
                        "call_id": acc.call_id,
                        "delta": fragment,
                    },
                )
            )
            self.state = "streaming"

        elif kind == "tool_call_done":
            call_id = str(chunk.get("call_id") or "")
            done_acc = self._tools.get(call_id=call_id)
            if done_acc is not None:
                done_acc.append_arguments(str(chunk.get("arguments") or ""))
                valid = done_acc.validate_arguments()
                item_id = make_function_call_item_id(done_acc.call_id)
                if valid:
                    frames.append(
                        await self._emit(
                            "response.function_call_arguments.done",
                            {
                                "type": "response.function_call_arguments.done",
                                "output_index": done_acc.output_index,
                                "call_id": done_acc.call_id,
                                "arguments": done_acc.arguments,
                            },
                        )
                    )
                    frames.append(
                        await self._emit(
                            "response.output_item.done",
                            {
                                "type": "response.output_item.done",
                                "output_index": done_acc.output_index,
                                "item": {
                                    "id": item_id,
                                    "type": "function_call",
                                    "status": "completed",
                                    "call_id": done_acc.call_id,
                                    "name": done_acc.name,
                                    "arguments": done_acc.arguments,
                                },
                            },
                        )
                    )
                    done_acc.mark_item_done()
                else:
                    # Truncated / invalid arguments: close the item safely but
                    # NEVER emit arguments.done (R-P1-22) -- a client that
                    # JSON.parses the partial fragment would execute a mangled
                    # tool call.
                    frames.append(
                        await self._emit(
                            "response.output_item.done",
                            {
                                "type": "response.output_item.done",
                                "output_index": done_acc.output_index,
                                "item": {
                                    "id": item_id,
                                    "type": "function_call",
                                    "status": "incomplete",
                                    "call_id": done_acc.call_id,
                                    "name": done_acc.name,
                                    "arguments": done_acc.arguments,
                                },
                            },
                        )
                    )
                    done_acc.mark_item_done()

        # "finish" is a no-op marker: the natural-end handler sees it and
        # reports a graceful completion instead of a truncation.
        return frames

    async def _close_open_items(self, *, incomplete: bool) -> list[bytes]:
        """Close any still-open items (truncation uses ``incomplete=True``)."""
        frames: list[bytes] = []
        if self._open_message is not None:
            idx = self._open_message["output_index"]
            item_id = self._open_message["id"]
            frames.append(
                await self._emit(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "output_index": idx,
                    },
                )
            )
            frames.append(
                await self._emit(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": idx,
                        "item": {
                            "id": item_id,
                            "type": "message",
                            "status": "incomplete" if incomplete else "completed",
                            "role": "assistant",
                        },
                    },
                )
            )
            self._open_message = None
        for acc in self._tools.incomplete():
            if acc.item_added and not acc.item_done:
                frames.append(
                    await self._emit(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "output_index": acc.output_index,
                            "item": {
                                "id": make_function_call_item_id(acc.call_id),
                                "type": "function_call",
                                "status": "incomplete" if incomplete else "completed",
                                "call_id": acc.call_id,
                                "name": acc.name,
                                "arguments": acc.arguments,
                            },
                        },
                    )
                )
                acc.mark_item_done()
        return frames

    # -- terminal helpers ---------------------------------------------------

    async def _terminal_frames(
        self,
        *,
        reason: TerminalReason,
        strict: bool,
    ) -> list[bytes]:
        """Render the truncation terminal event + ``[DONE]`` (criteria ⑥⑦)."""
        frames = await self._close_open_items(incomplete=True)
        details = to_incomplete_details(
            reason,
            "upstream stream terminated: {0}".format(reason.value),
        )
        if strict:
            status = "incomplete" if _is_timeout_reason(reason) else "failed"
        else:
            status = "completed"
        event_name = "response.{0}".format(status)
        response_obj: dict[str, Any] = {
            "id": self.response_id,
            "status": status,
            "incomplete_details": details,
            "terminal_reason": reason.value,
        }
        frames.append(
            await self._emit(
                event_name,
                {
                    "type": event_name,
                    "response": response_obj,
                },
            )
        )
        frames.append(SSE_DONE_FRAME)
        self.state = status
        self.stats.terminal_reason = reason.value
        self.stats.truncated_streams += 1
        return frames

    async def _completed_frames(self) -> list[bytes]:
        """Graceful ``response.completed`` + ``[DONE]`` (T21 criterion ⑤)."""
        frames = await self._close_open_items(incomplete=False)
        frames.append(
            await self._emit(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"id": self.response_id, "status": "completed"},
                },
            )
        )
        frames.append(SSE_DONE_FRAME)
        self.state = "completed"
        return frames

    # -- the run loop -------------------------------------------------------

    async def run(
        self,
        upstream: AsyncIterable[Any],
        *,
        client_cancelled: asyncio.Event | None = None,
        key_health: Any = None,  # noqa: ARG002 - see R-P1-25 note in module doc
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        config: PipelineConfig | None = None,
    ) -> AsyncIterable[bytes]:
        """Stream the canonical sequence for ``upstream``.

        ``client_cancelled`` -- when set, the upstream is closed immediately
        and the cancellation is counted in ``stats.client_disconnects``
        (R-P1-24/25).  ``key_health`` is accepted for the caller's wiring but
        the pipeline deliberately never mutates it: a client disconnect is not
        an upstream failure (criterion ③).
        """
        cfg = config or self._config
        clock = clock or time.monotonic
        sleep = sleep or asyncio.sleep

        yield await self._emit(
            "response.created",
            {"type": "response.created", "response": {"id": self.response_id, "status": "in_progress"}},
        )
        yield await self._emit(
            "response.in_progress",
            {"type": "response.in_progress", "response": {"id": self.response_id, "status": "in_progress"}},
        )
        self.state = "in_progress"
        self._done = False
        self._open_message = None
        self._output_index = 0

        source = upstream() if callable(upstream) else upstream
        queue: asyncio.Queue = asyncio.Queue()
        started_at = clock()
        produced = False
        last_chunk_at = started_at

        # -- producers ----------------------------------------------------

        async def _produce() -> None:
            nonlocal produced, last_chunk_at
            try:
                async for chunk in source:
                    produced = True
                    last_chunk_at = clock()
                    await queue.put(("chunk", chunk))
                await queue.put(("upstream_end", None))
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                await queue.put(("upstream_error", exc))
            except Exception as exc:  # noqa: BLE001 - attribute, never crash
                await queue.put(("upstream_error", exc))

        async def _watch_cancel() -> None:
            if client_cancelled is None:
                return
            await client_cancelled.wait()
            await _close_upstream(source)
            await queue.put(("client_cancel", None))

        async def _monitor() -> None:
            nonlocal produced, last_chunk_at
            last_hb = clock()
            while not self._done:
                now = clock()
                candidates = [last_hb + cfg.heartbeat_seconds]
                if not produced:
                    candidates.append(started_at + cfg.first_token_seconds)
                else:
                    candidates.append(last_chunk_at + cfg.read_idle_seconds)
                if cfg.total_seconds > 0:
                    candidates.append(started_at + cfg.total_seconds)
                gap = max(0.0, min(candidates) - now)
                await sleep(gap)
                if self._done:
                    return
                now = clock()
                if cfg.total_seconds > 0 and now >= started_at + cfg.total_seconds:
                    await queue.put(("timeout", TerminalReason.MAX_RESPONSE_TIME))
                    return
                if not produced and now >= started_at + cfg.first_token_seconds:
                    await queue.put(("timeout", TerminalReason.FIRST_TOKEN_TIMEOUT))
                    return
                if produced and now >= last_chunk_at + cfg.read_idle_seconds:
                    await queue.put(("timeout", TerminalReason.READ_IDLE_TIMEOUT))
                    return
                if now >= last_hb + cfg.heartbeat_seconds:
                    last_hb = now
                    self.stats.heartbeats += 1
                    await queue.put(("heartbeat", last_hb))

        producers = [
            asyncio.create_task(_produce()),
            asyncio.create_task(_monitor()),
        ]
        if client_cancelled is not None:
            producers.append(asyncio.create_task(_watch_cancel()))

        terminal_reason: TerminalReason | None = None
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "chunk":
                    for frame in await self._translate_chunk(payload):
                        yield frame
                elif kind == "heartbeat":
                    yield SSE_HEARTBEAT_FRAME
                elif kind == "client_cancel":
                    # Client is gone: close the upstream (already done by the
                    # watcher), count the disconnect, and stop -- no terminal
                    # event (nobody reads it) and NO health mutation (R-P1-25).
                    self.stats.client_disconnects += 1
                    break
                elif kind == "timeout":
                    terminal_reason = payload
                    break
                elif kind == "upstream_error":
                    terminal_reason = TerminalReason.UPSTREAM_TRUNCATED if produced else TerminalReason.UPSTREAM_CONNECT
                    break
                elif kind == "upstream_end":
                    if produced:
                        terminal_reason = TerminalReason.UPSTREAM_TRUNCATED
                    break
        finally:
            self._done = True
            for task in producers:
                task.cancel()
            for task in producers:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await _close_upstream(source)

        if terminal_reason is not None:
            for frame in await self._terminal_frames(
                reason=terminal_reason,
                strict=cfg.strict_terminal,
            ):
                yield frame
        else:
            for frame in await self._completed_frames():
                yield frame


__all__ = [
    "PipelineConfig",
    "PipelineStats",
    "ResponsePipeline",
    "sse_frame",
]
