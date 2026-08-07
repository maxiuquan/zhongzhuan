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
    SSE_HEARTBEAT_FRAME,
    TerminalReason,
    TIMEOUT_REASONS,
    make_function_call_item_id,
    make_message_item_id,
)
from ..proxy.protocol.tool_accumulator import ToolCallAccumulator, ToolCallCollection
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
    #: P0-7 / 铁律 5: the shipped defaults are the hard ceilings of the law --
    #: 300s to the first token, 300s of read idle, 900s wall clock.  They were
    #: 600/600/1800, which silently doubled 铁律 5 for every caller that did
    #: not pass an explicit config.
    first_token_seconds: float = 300.0
    read_idle_seconds: float = 300.0
    total_seconds: float = 900.0
    connect_seconds: float = 15.0
    #: Criterion ⑤ (R-P1-27): heartbeat arrival gap must never exceed this.
    max_heartbeat_gap_seconds: float = 16.0
    #: Codex 26.x "Concurrent reasoning summaries"（请求带
    #: ``reasoning.summary='detailed'``）期望每个响应都有 reasoning 生命周期
    #: 事件；上游对 gpt-5.6-sol 在真实请求下不回 reasoning_content（2026-08-07
    #: 实测 8 个变体全为 0 帧）。开启后：若整个流从未发出任何 reasoning 事件，
    #: 在 message item 前补发一个空 reasoning item 的完整生命周期，让严格客户端
    #: 不再因缺失 reasoning 生命周期而直接结束会话（默认关闭，由 handler 按请求
    #: 是否要求推理决定开启）。
    emit_empty_reasoning: bool = False

    def __post_init__(self) -> None:
        """AC-7.2 / AC-7.3: clamp configured values back inside 铁律 5.

        Clamping (rather than raising) is deliberate: a mistyped timeout in
        YAML must not turn into a proxy that refuses to boot.  The law is a
        *ceiling* on patience, so the safe direction is always "be stricter".
        ``frozen=True`` forces ``object.__setattr__``.
        """
        object.__setattr__(self, "first_token_seconds", min(300.0, float(self.first_token_seconds)))
        object.__setattr__(self, "read_idle_seconds", min(300.0, float(self.read_idle_seconds)))
        object.__setattr__(self, "total_seconds", min(900.0, float(self.total_seconds)))

    @classmethod
    def from_config(cls, cfg: Any, **overrides: Any) -> "PipelineConfig":
        """AC-7.4: build from the ``responses_bridge`` section.

        Two levels are read, because the settings live at two levels:
        ``strict_terminal`` on the bridge itself and the four timeouts under
        ``bridge.timeout``.  Whichever object the caller happens to hold (root
        ``Config``, the bridge, or the bare ``timeout`` section) is resolved
        down to both, so no call site needs to know the nesting.

        Args:
            cfg: Root ``Config``, the ``responses_bridge`` section, or the
                ``timeout`` section.  ``None`` / a config without the section
                yields the shipped defaults instead of an ``AttributeError``.
            **overrides: Explicit values that win over the config (used by the
                background worker, which does not share the client-facing
                heartbeat cadence).

        Returns:
            A :class:`PipelineConfig` whose timeouts are already clamped.
        """
        bridge = getattr(cfg, "responses_bridge", None) or cfg
        timeout = getattr(bridge, "timeout", None) or bridge

        values: dict[str, Any] = {}
        for name in ("first_token_seconds", "read_idle_seconds", "total_seconds", "connect_seconds"):
            raw = getattr(timeout, name, None)
            if raw is not None:
                values[name] = float(raw)
        # P0-2 / 铁律 2: read from the bridge level, and never coerce with
        # float() -- this one is a bool.
        strict = getattr(bridge, "strict_terminal", None)
        if strict is not None:
            values["strict_terminal"] = bool(strict)
        values.update(overrides)
        return cls(**values)


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


def _tool_item_id(acc: ToolCallAccumulator) -> str:
    """The stable Responses ``item.id`` of a tool call (P0-4).

    :class:`ToolCallCollection` fixes ``item_id`` at creation time from
    ``response_id + output_index``.  The ``call_id`` fallback only fires for
    accumulators built outside the collection (direct construction in unit
    tests); it preserves the historical ``fc_{call_id}`` shape rather than
    emitting an empty id.
    """
    return acc.item_id or make_function_call_item_id(acc.call_id)


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
        #: The assistant message as it was streamed, kept so the terminal row
        #: can be persisted with a real ``output`` array (a retrieve() after a
        #: stream must not answer ``completed`` with an empty body).  Only the
        #: text is retained -- deltas already went out and live in the event log.
        self._message_item: dict[str, Any] | None = None
        self._message_text: list[str] = []
        #: 当前 message item 的 content part id（OpenAI 用 ``pt_*``）。首个文本
        #: delta 前发 ``content_part.added``，关闭 item 前发 ``content_part.done``，
        #: 否则严格客户端（Codex / OpenAI SDK）会因「delta without active item」
        #: 丢弃整个响应（issue ③）。
        self._message_part_id: str | None = None
        #: 上游 ``reasoning_content`` 对应的 reasoning item 状态。Codex 26.x 桌面版
        #: 启用了 concurrent reasoning summaries，期望响应流带 reasoning 事件
        #: （reasoning_summary_text.* 家族）；缺失时客户端会直接结束会话且不渲染
        #: 文本（2026-08-07 实测：上游返回 reasoning_content，relay 之前丢弃）。
        self._open_reasoning: dict[str, Any] | None = None
        self._reasoning_text: list[str] = []
        self._reasoning_part_id: str | None = None
        self._output_index = 0
        self._done = False
        #: P0-2: set when the upstream sent an *explicit* completion signal.
        #: EOF alone is not a completion signal, and having produced chunks is
        #: not a truncation signal -- only this flag separates the two.
        self._saw_provider_finish = False
        #: U1 / 铁律 2: set when any tool call's arguments failed to validate.
        #: Forces a strict terminal even in compatibility mode.
        self._had_invalid_tool_args = False

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

        if kind in ("reasoning", "reasoning_summary_text.delta"):
            delta = str(chunk.get("delta", chunk.get("text", "")) or "")
            if not delta:
                return frames
            if self._open_reasoning is None:
                idx = self._output_index
                self._output_index += 1
                item_id = "rs_{0}_{1}".format(self.response_id, idx)
                self._open_reasoning = {"id": item_id, "output_index": idx}
                self._reasoning_text = []
                self._reasoning_part_id = "pt_{0}".format(item_id)
                frames.append(
                    await self._emit(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": idx,
                            "item": {
                                "id": item_id,
                                "type": "reasoning",
                                "status": "in_progress",
                                "summary": [],
                            },
                        },
                    )
                )
                frames.append(
                    await self._emit(
                        "response.reasoning_summary_part.added",
                        {
                            "type": "response.reasoning_summary_part.added",
                            "item_id": item_id,
                            "output_index": idx,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": ""},
                        },
                    )
                )
            frames.append(
                await self._emit(
                    "response.reasoning_summary_text.delta",
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "item_id": self._open_reasoning["id"],
                        "output_index": self._open_reasoning["output_index"],
                        "summary_index": 0,
                        "delta": delta,
                    },
                )
            )
            self._reasoning_text.append(delta)
            self.state = "streaming"

        elif kind in ("text", "output_text.delta"):
            delta = str(chunk.get("delta", chunk.get("text", "")) or "")
            if self._open_message is None:
                # OpenAI 顺序：reasoning item 在 message item 前完整结束。
                # 若上游 reasoning_content 已发完而 text 开始，先关闭 reasoning。
                frames.extend(await self._close_reasoning())
                # Codex 26.x concurrent-reasoning 客户端：若请求要求推理但上游
                # 全程未给 reasoning_content，在首个文本前补一个空的 reasoning
                # 生命周期，避免客户端直接结束会话（issue ⑩）。
                frames.extend(await self._ensure_reasoning_lifecycle())
                idx = self._output_index
                self._output_index += 1
                item_id = make_message_item_id(self.response_id, idx)
                self._open_message = {"id": item_id, "output_index": idx}
                self._message_item = dict(self._open_message)
                frames.append(
                    await self._emit(
                        "response.output_item.added",
                        {
                            "type": "response.output_item.added",
                            "output_index": idx,
                            "item": {
                                "id": item_id,
                                "type": "message",
                                "status": "in_progress",
                                "role": "assistant",
                                "content": [],
                            },
                        },
                    )
                )
                # issue ③: 每个文本输出项必须先发 content_part.added（带 part id），
                # 严格客户端才能把后续的 output_text.delta 挂到正确的 part 上。
                self._message_part_id = "pt_{0}".format(item_id)
                frames.append(
                    await self._emit(
                        "response.content_part.added",
                        {
                            "type": "response.content_part.added",
                            "output_index": idx,
                            "item_id": item_id,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
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
            self._message_text.append(delta)
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
                                "id": _tool_item_id(acc),
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
            # Resolve by source index as well: an upstream that never sent an
            # ``id`` has a *synthetic* call id the adapter cannot know, and the
            # index is the only stable join key in that case (§5.3).
            done_acc = self._tools.get(call_id=call_id, source_index=chunk.get("source_index"))
            if done_acc is not None:
                done_acc.append_arguments(str(chunk.get("arguments") or ""))
                valid = done_acc.validate_arguments()
                item_id = _tool_item_id(done_acc)
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
                    # tool call.  U1: remember it so the terminal event cannot
                    # be whitewashed into `completed` by compatibility mode.
                    self._had_invalid_tool_args = True
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

        elif kind == "finish":
            # P0-2: the ONLY positive evidence that the upstream chose to stop.
            # It emits no frame of its own -- the natural-end handler reads the
            # flag and reports a graceful completion instead of a truncation.
            self._saw_provider_finish = True

        return frames

    async def _close_reasoning(self, *, incomplete: bool = False) -> list[bytes]:
        """Close an open reasoning item (summary_text family), if any."""
        frames: list[bytes] = []
        if self._open_reasoning is None:
            return frames
        idx = self._open_reasoning["output_index"]
        item_id = self._open_reasoning["id"]
        full_text = "".join(self._reasoning_text)
        frames.append(
            await self._emit(
                "response.reasoning_summary_text.done",
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": item_id,
                    "output_index": idx,
                    "summary_index": 0,
                    "text": full_text,
                },
            )
        )
        if self._reasoning_part_id is not None:
            frames.append(
                await self._emit(
                    "response.reasoning_summary_part.done",
                    {
                        "type": "response.reasoning_summary_part.done",
                        "item_id": item_id,
                        "output_index": idx,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": full_text},
                    },
                )
            )
            self._reasoning_part_id = None
        frames.append(
            await self._emit(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": {
                        "id": item_id,
                        "type": "reasoning",
                        "status": "incomplete" if incomplete else "completed",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": full_text,
                            }
                        ],
                    },
                },
            )
        )
        self._open_reasoning = None
        return frames

    async def _ensure_reasoning_lifecycle(self) -> list[bytes]:
        """Emit a full (empty) reasoning item lifecycle when the stream never did.

        Codex 26.x 启用了 concurrent reasoning summaries：请求带
        ``reasoning.summary='detailed'`` 时，客户端期望流中至少出现一次
        ``reasoning_summary_part.added`` → ``reasoning_summary_text.delta`` →
        ``reasoning_summary_text.done`` → ``reasoning_summary_part.done`` →
        ``output_item.done``（type=reasoning）。上游（gpt-5.6-sol 经
        work.freemodel.dev）在真实 Codex 请求下不返回任何 ``reasoning_content``
        （2026-08-07 实测 8 个变体全部为 0 帧），此时若流里从未出现 reasoning
        事件，Codex 会因等不到生命周期而**直接结束会话且不渲染任何文本**。

        ``emit_empty_reasoning`` 开启时，在正常完成路径上补一个空 reasoning
        item：文本为空，但生命周期完整，客户端因此能正常渲染后续的 output_text。
        """
        frames: list[bytes] = []
        if not self._config.emit_empty_reasoning:
            return frames
        if self._open_reasoning is not None or self._reasoning_text:
            # 上游已提供过真实 reasoning（无论是否还在流中）→ 无需补。
            return frames
        if self._open_message is not None or self._tools.list_all():
            # message / tool item 已开：此时再插一个 reasoning 会打乱
            # output_index 顺序（OpenAI 要求 reasoning 在 message 前）。
            # 仅在一切输出开始之前补。
            return frames
        idx = self._output_index
        self._output_index += 1
        item_id = "rs_{0}_{1}".format(self.response_id, idx)
        part_id = "pt_{0}".format(item_id)
        self._open_reasoning = {"id": item_id, "output_index": idx}
        self._reasoning_text = []
        self._reasoning_part_id = part_id
        frames.append(
            await self._emit(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": {
                                "id": item_id,
                                "type": "reasoning",
                                "status": "in_progress",
                                "summary": [],
                            },
                },
            )
        )
        frames.append(
            await self._emit(
                "response.reasoning_summary_part.added",
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": item_id,
                    "output_index": idx,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
            )
        )
        frames.append(
            await self._emit(
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": item_id,
                    "output_index": idx,
                    "summary_index": 0,
                    "delta": "",
                },
            )
        )
        frames.extend(await self._close_reasoning(incomplete=False))
        return frames

    async def _close_open_items(self, *, incomplete: bool) -> list[bytes]:
        """Close any still-open items (truncation uses ``incomplete=True``)."""
        frames: list[bytes] = []
        frames.extend(await self._close_reasoning(incomplete=incomplete))
        if self._open_message is not None:
            idx = self._open_message["output_index"]
            item_id = self._open_message["id"]
            full_text = "".join(self._message_text)
            frames.append(
                await self._emit(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "output_index": idx,
                        "item_id": item_id,
                        "content_index": 0,
                        "text": full_text,
                    },
                )
            )
            # issue ③: 关闭文本 part 前发 content_part.done（与 added 同一 part id），
            # 否则严格客户端报 "delta without active item" 并丢弃响应。
            if self._message_part_id is not None:
                frames.append(
                    await self._emit(
                        "response.content_part.done",
                        {
                            "type": "response.content_part.done",
                            "output_index": idx,
                            "item_id": item_id,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": full_text,
                                "annotations": [],
                            },
                        },
                    )
                )
                self._message_part_id = None
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
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": full_text,
                                    "annotations": [],
                                }
                            ],
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
                                "id": _tool_item_id(acc),
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

    def output_items(self) -> list[dict[str, Any]]:
        """The response's ``output`` array, in the order the items were opened.

        The caller persists this on the terminal row so a ``retrieve()`` after
        a stream returns the same items the client just watched arrive.  It is
        reconstructed from the pipeline's own state -- the message text it
        emitted and the tool accumulators it owns -- so it can never disagree
        with the frames that were sent (the item ids are literally the same
        objects, P0-4).

        A tool call whose arguments never validated is reported ``incomplete``
        here for exactly the reason it never got a ``arguments.done`` frame
        (铁律 2): a stored call that looks complete would be replayed as one.
        """
        items: list[tuple[int, dict[str, Any]]] = []
        if self._message_item is not None:
            items.append(
                (
                    int(self._message_item["output_index"]),
                    {
                        "id": self._message_item["id"],
                        "type": "message",
                        "role": "assistant",
                        "status": "completed" if self.state == "completed" else "incomplete",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "".join(self._message_text),
                                "annotations": [],
                            }
                        ],
                    },
                )
            )
        for acc in self._tools.list_all():
            if not acc.item_added:
                continue
            items.append(
                (
                    int(acc.output_index),
                    {
                        "id": _tool_item_id(acc),
                        "type": "function_call",
                        # ``arguments_done`` is the record of what actually
                        # happened during the stream, not a fresh re-parse:
                        # re-validating here could disagree with the frames
                        # already sent (and would mutate the accumulator).
                        "status": "completed" if acc.arguments_done else "incomplete",
                        "call_id": acc.call_id,
                        "name": acc.name,
                        "arguments": acc.arguments,
                    },
                )
            )
        items.sort(key=lambda pair: pair[0])
        return [item for _index, item in items]

    async def _terminal_frames(
        self,
        *,
        reason: TerminalReason,
        strict: bool,
    ) -> list[bytes]:
        """Render the truncation terminal event (criteria ⑥⑦)."""
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
        self.state = status
        self.stats.terminal_reason = reason.value
        self.stats.truncated_streams += 1
        return frames

    async def _completed_frames(self) -> list[bytes]:
        """Graceful ``response.completed`` (T21 criterion ⑤)."""
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
        #: run() 的 config 形参必须同步回 ``self._config``：内部辅助方法
        #: （``_ensure_reasoning_lifecycle`` 等）读 ``self._config``，若不写回，
        #: 调用方通过 ``run(config=...)`` 传入的覆盖（如 emit_empty_reasoning）
        #: 会静默失效（2026-08-07 实测：handler 传了 emit_empty_reasoning=True
        #: 但流里没有任何 reasoning 事件，根因在此）。
        if config is not None:
            self._config = config
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
                    # P0-2: the criterion for a clean EOF is that the upstream
                    # gave an explicit finish signal -- NOT that chunks were
                    # produced.  "Produced chunks then EOF" is the single most
                    # common *successful* completion, and the old criterion
                    # mislabelled every one of them as a truncation.
                    if not self._saw_provider_finish:
                        terminal_reason = (
                            TerminalReason.UPSTREAM_TRUNCATED if produced else TerminalReason.UPSTREAM_CONNECT
                        )
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

        # U1 / 铁律 2: compatibility mode may whitewash a *transport* truncation
        # into `response.completed`, but it must never whitewash a tool call
        # whose arguments the provider declared final and which did not parse.
        # A client that treats such a response as successful executes a mangled
        # tool call -- strictly worse than a visible failure.
        if self._had_invalid_tool_args:
            for frame in await self._terminal_frames(
                reason=terminal_reason or TerminalReason.INVALID_TOOL_ARGUMENTS,
                strict=True,
            ):
                yield frame
        elif terminal_reason is not None:
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
