"""Upstream SSE bytes -> :class:`ResponsePipeline` chunk vocabulary (P0-1).

Why this module exists
----------------------
:class:`~zhongzhuan.responses_v3.pipeline.ResponsePipeline` is the sole owner of
the Responses SSE lifecycle, but it deliberately knows nothing about wire
formats: it consumes a tiny, provider-agnostic vocabulary of dicts::

    {"type": "text",           "delta": str}
    {"type": "tool_call",      "call_id": str, "name": str,
                               "arguments": str, "source_index": int}
    {"type": "tool_call_done", "call_id": str, "source_index": int,
                               "arguments": str}
    {"type": "finish"}

Everything upstream-specific -- Chat Completions ``choices[].delta`` framing,
Anthropic ``content_block_delta`` framing, ``[DONE]`` sentinels, tool-call
fragmentation -- is normalised here and nowhere else.  Keeping the translation
in one place is what lets the live stream and the background catch-up stream
produce byte-identical frames: both feed the same pipeline with the same
vocabulary.

Design notes
------------
* Framing is delegated to :class:`~zhongzhuan.proxy.protocol.sse_parser.SSEParser`
  (byte-level, split-point agnostic).  This adapter only interprets the *payload*
  of an already-complete frame, so an upstream that splits a JSON object across
  three TCP segments is handled for free.
* A frame whose ``data`` is not valid JSON is **skipped**, never guessed at.
  Fabricating a chunk from a malformed frame would surface as invented text or
  a mangled tool call downstream (铁律 2).
* ``finish`` is emitted at most once, from the first positive completion signal
  (``finish_reason``, Anthropic ``message_stop`` / ``message_delta.stop_reason``,
  or the ``[DONE]`` sentinel).  P0-2 reads this as "the provider chose to stop",
  which is what separates a clean EOF from a truncation.
* Every tool call that is still open when that signal arrives is closed with
  exactly one ``tool_call_done`` *before* ``finish``.  Chat Completions has no
  per-call terminator of its own -- ``finish_reason`` is the terminator -- and
  without this synthesis the arguments would never be validated and the item
  would be reported as truncated (铁律 2).
* Tool-call arguments are forwarded verbatim as fragments; the adapter never
  parses or validates them.  Validation happens exactly once, at
  ``tool_call_done``, inside the accumulator.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterable, AsyncIterator

from ..proxy.protocol.sse_parser import SseFrame, SSEParser

#: The three wire dialects this adapter understands.
PROTOCOL_OPENAI: str = "openai"
PROTOCOL_ANTHROPIC: str = "anthropic"
PROTOCOL_RESPONSES: str = "responses"

#: Anthropic event names that positively terminate a message.
_ANTHROPIC_STOP_EVENTS: frozenset[str] = frozenset({"message_stop"})

#: Native Responses terminal events.
_NATIVE_TERMINAL_EVENTS: frozenset[str] = frozenset({"response.completed", "response.failed", "response.incomplete"})


def _loads(data: str) -> dict[str, Any] | None:
    """Parse one SSE ``data:`` payload, returning ``None`` when unusable."""
    text = data.strip()
    if not text or text == "[DONE]":
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_or(value: Any, default: int = 0) -> int:
    """Coerce an upstream index to ``int`` without ever raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class UpstreamSSEChunkAdapter:
    """Convert an upstream SSE byte stream into pipeline chunks.

    Instances are single-use and not thread-safe: one adapter per upstream
    response, matching the lifetime of the :class:`SSEParser` it owns.
    """

    __slots__ = ("_protocol", "_parser", "_finished", "_open_tools", "_closed_tools", "_usage")

    def __init__(self, protocol: str = PROTOCOL_OPENAI) -> None:
        self._protocol: str = (protocol or PROTOCOL_OPENAI).strip().lower()
        self._parser: SSEParser = SSEParser()
        self._finished: bool = False
        #: source index -> last known ``call_id`` for calls not yet closed.
        #: Insertion order is the upstream's own order, which is the order the
        #: synthesised ``tool_call_done`` events must follow.
        self._open_tools: dict[int, str] = {}
        #: source indices already closed, so a terminator is never duplicated
        #: (a second ``output_item.done`` would break 铁律 3).
        self._closed_tools: set[int] = set()
        #: Usage captured from the upstream terminal event (native Responses
        #: ``response.completed``) or the final Chat Completions chunk.  Filled
        #: by :meth:`_capture_usage`, read by the caller after the stream is
        #: consumed so request_logs gets real token counts (was always 0/0).
        self._usage: dict[str, Any] = {}

    # -- construction ------------------------------------------------------

    @classmethod
    def for_protocol(cls, outbound_protocol: str, *, native: bool = False) -> "UpstreamSSEChunkAdapter":
        """Pick the adapter dialect for an outbound protocol.

        ``native=True`` means the upstream already speaks Responses SSE, so its
        payload events are mapped back into the vocabulary instead of being
        parsed as Chat Completions.
        """
        if native:
            return cls(PROTOCOL_RESPONSES)
        proto = (outbound_protocol or "").strip().lower()
        if proto == PROTOCOL_ANTHROPIC:
            return cls(PROTOCOL_ANTHROPIC)
        return cls(PROTOCOL_OPENAI)

    # -- properties --------------------------------------------------------

    @property
    def protocol(self) -> str:
        """The dialect this adapter decodes."""
        return self._protocol

    @property
    def saw_finish(self) -> bool:
        """Whether a positive completion signal has already been emitted."""
        return self._finished

    @property
    def usage(self) -> dict[str, Any]:
        """Usage observed from the upstream terminal / final chunk.

        Returns a normalized ``{"input_tokens": int, "output_tokens": int}``
        dict (0 when the upstream never sent usage).  Empty until the stream
        reaches its terminal event, so it must be read only after the caller
        has consumed the full chunk stream.
        """
        return self._usage

    def _capture_usage(self, payload: dict[str, Any]) -> None:
        """Normalize + retain upstream usage so the caller can log real tokens.

        Accepts either the Responses shape (``input_tokens`` / ``output_tokens``
        / ``total_tokens``) or the Chat Completions shape (``prompt_tokens`` /
        ``completion_tokens`` / ``total_tokens``).  Native Responses terminal
        events nest usage inside a ``response`` container; the Chat Completions
        usage chunk carries it at the top level.  The last one seen wins, which
        for Chat Completions is the final chunk carrying ``usage``.
        """
        usage = payload.get("usage")
        if not isinstance(usage, dict) or not usage:
            resp_container = payload.get("response")
            if isinstance(resp_container, dict):
                usage = resp_container.get("usage")
        if not isinstance(usage, dict) or not usage:
            return
        norm: dict[str, Any] = {}
        for in_key in ("input_tokens", "prompt_tokens"):
            if in_key in usage:
                norm["input_tokens"] = _int_or(usage.get(in_key), 0)
                break
        for out_key in ("output_tokens", "completion_tokens"):
            if out_key in usage:
                norm["output_tokens"] = _int_or(usage.get(out_key), 0)
                break
        if "total_tokens" in usage:
            norm["total_tokens"] = _int_or(usage.get("total_tokens"), 0)
        if norm:
            self._usage = norm

    # -- the stream --------------------------------------------------------

    async def iter_chunks(self, byte_stream: AsyncIterable[bytes]) -> AsyncIterator[dict[str, Any]]:
        """Yield pipeline chunks for every complete frame in ``byte_stream``.

        The caller owns ``byte_stream``'s lifetime; this generator never closes
        it, so a single ``aclose()`` from the consumer propagates to the
        transport exactly once (R1 race, see ``_dispatch_v3_create_stream``).
        """
        async for raw in byte_stream:
            if not raw:
                continue
            for frame in self._parser.feed(raw):
                for chunk in self._frame_to_chunks(frame):
                    yield chunk
        for frame in self._parser.flush():
            for chunk in self._frame_to_chunks(frame):
                yield chunk

    # -- frame translation -------------------------------------------------

    def _frame_to_chunks(self, frame: SseFrame) -> list[dict[str, Any]]:
        """Translate one complete SSE frame into zero or more chunks."""
        if frame.is_done_sentinel():
            return self._finish()
        payload = _loads(frame.data)
        if payload is None:
            # Malformed / comment / keep-alive frame: never guess (R-P1-22).
            return []
        if self._protocol == PROTOCOL_ANTHROPIC:
            return self._anthropic_chunks(frame.event_type, payload)
        if self._protocol == PROTOCOL_RESPONSES:
            return self._native_chunks(frame.event_type, payload)
        return self._openai_chunks(payload)

    # -- tool bookkeeping --------------------------------------------------

    def _track_tool(self, source_index: int, call_id: str) -> None:
        """Remember an open tool call (and its latest known ``call_id``)."""
        if source_index in self._closed_tools:
            return
        known = self._open_tools.get(source_index, "")
        self._open_tools[source_index] = call_id or known

    def _close_tool(self, source_index: int) -> list[dict[str, Any]]:
        """Emit the single terminator for one tool call, if still open."""
        if source_index in self._closed_tools or source_index not in self._open_tools:
            return []
        call_id = self._open_tools.pop(source_index)
        self._closed_tools.add(source_index)
        return [
            {
                "type": "tool_call_done",
                "call_id": call_id,
                "source_index": source_index,
                "arguments": "",
            }
        ]

    def _close_all_tools(self) -> list[dict[str, Any]]:
        """Close every still-open tool call, in upstream order."""
        chunks: list[dict[str, Any]] = []
        for source_index in list(self._open_tools):
            chunks.extend(self._close_tool(source_index))
        return chunks

    def _finish(self) -> list[dict[str, Any]]:
        """Close open tools then emit the single ``finish`` marker (P0-2)."""
        if self._finished:
            return []
        self._finished = True
        return self._close_all_tools() + [{"type": "finish"}]

    # -- OpenAI Chat Completions ------------------------------------------

    def _openai_chunks(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Decode one ``chat.completion.chunk`` object."""
        chunks: list[dict[str, Any]] = []
        choices = payload.get("choices")
        if not isinstance(choices, list):
            # Usage-only chunk: ``{"usage": {...}}`` with no choices (sent when
            # ``stream_options.include_usage=true``).  No content to forward,
            # but the tokens must not be lost.
            if "usage" in payload:
                self._capture_usage(payload)
            return chunks
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                text = delta.get("content")
                if isinstance(text, str) and text:
                    chunks.append({"type": "text", "delta": text})
                reasoning = delta.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    chunks.append({"type": "reasoning", "delta": reasoning})
                chunks.extend(self._openai_tool_calls(delta.get("tool_calls")))
            if choice.get("finish_reason"):
                # Chat Completions has no per-call terminator: finish_reason is
                # the only point at which the arguments are known to be final.
                chunks.extend(self._finish())
        # Some upstreams attach ``usage`` on the final chunk *with* choices.
        if "usage" in payload:
            self._capture_usage(payload)
        return chunks

    def _openai_tool_calls(self, tool_calls: Any) -> list[dict[str, Any]]:
        """Decode the ``delta.tool_calls`` array of a Chat chunk.

        Each entry is a *fragment*: ``id`` and ``function.name`` may appear on
        any fragment (or never), and ``function.arguments`` accumulates.  The
        stable join key is ``index``, forwarded as ``source_index`` so a late
        ``call_id`` cannot split one call into two accumulators (§5.3).
        """
        chunks: list[dict[str, Any]] = []
        if not isinstance(tool_calls, list):
            return chunks
        for entry in tool_calls:
            if not isinstance(entry, dict):
                continue
            source_index = _int_or(entry.get("index"), 0)
            function = entry.get("function")
            function = function if isinstance(function, dict) else {}
            call_id = str(entry.get("id") or "")
            self._track_tool(source_index, call_id)
            chunks.append(
                {
                    "type": "tool_call",
                    "call_id": call_id,
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                    "source_index": source_index,
                }
            )
        return chunks

    # -- Anthropic Messages ------------------------------------------------

    def _anthropic_chunks(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Decode one Anthropic Messages streaming event."""
        if event in _ANTHROPIC_STOP_EVENTS:
            return self._finish()
        if event == "message_delta":
            # Anthropic reports final usage on ``message_delta``.  Keep it even
            # when this delta is not a stop signal (multi-stop streams).
            if isinstance(payload.get("usage"), dict):
                self._capture_usage(payload)
            delta = payload.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                return self._finish()
            return []
        if event == "content_block_start":
            return self._anthropic_block_start(payload)
        if event == "content_block_delta":
            return self._anthropic_block_delta(payload)
        if event == "content_block_stop":
            return self._close_tool(_int_or(payload.get("index"), 0))
        return []

    def _anthropic_block_start(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Open a ``tool_use`` content block (text blocks need no bookkeeping)."""
        block = payload.get("content_block")
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            return []
        source_index = _int_or(payload.get("index"), 0)
        call_id = str(block.get("id") or "")
        self._track_tool(source_index, call_id)
        return [
            {
                "type": "tool_call",
                "call_id": call_id,
                "name": str(block.get("name") or ""),
                "arguments": "",
                "source_index": source_index,
            }
        ]

    def _anthropic_block_delta(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Forward a text delta or a tool ``input_json_delta`` fragment."""
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return []
        delta_type = str(delta.get("type") or "")
        if delta_type == "text_delta":
            text = str(delta.get("text") or "")
            return [{"type": "text", "delta": text}] if text else []
        if delta_type == "input_json_delta":
            source_index = _int_or(payload.get("index"), 0)
            return [
                {
                    "type": "tool_call",
                    "call_id": self._open_tools.get(source_index, ""),
                    "name": "",
                    "arguments": str(delta.get("partial_json") or ""),
                    "source_index": source_index,
                }
            ]
        return []

    # -- native Responses passthrough -------------------------------------

    def _native_chunks(self, event: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Decode a *native* Responses SSE event back into the vocabulary.

        A native upstream already emits a lifecycle, but this proxy owns the
        response id and the persistence, so payload events are re-derived and
        the upstream's own ``response.created``/``completed`` bookends are
        dropped -- the pipeline emits exactly one lifecycle (铁律 3).
        """
        etype = str(payload.get("type") or event or "")
        if etype == "response.output_text.delta":
            delta = str(payload.get("delta") or "")
            return [{"type": "text", "delta": delta}] if delta else []
        if etype == "response.function_call_arguments.delta":
            source_index = _int_or(payload.get("output_index"), 0)
            call_id = str(payload.get("call_id") or payload.get("item_id") or "")
            self._track_tool(source_index, call_id)
            chunk: dict[str, Any] = {
                "type": "tool_call",
                "call_id": call_id,
                "name": str(payload.get("name") or ""),
                "arguments": str(payload.get("delta") or ""),
                "source_index": source_index,
            }
            # NATIVE 上游（真 responses API）可能在 function_call item 上带
            # ``namespace``（Codex 26.x MCP 子代理）。透传到 chunk，pipeline 在
            # 发射 output_item 时保留它，Codex 才能路由回 MCP server。
            ns = str(payload.get("namespace") or "")
            if not ns and isinstance(payload.get("item"), dict):
                ns = str(payload["item"].get("namespace") or "")
            if ns:
                chunk["namespace"] = ns
            return [chunk]
        if etype == "response.function_call_arguments.done":
            return self._close_tool(_int_or(payload.get("output_index"), 0))
        if etype in _NATIVE_TERMINAL_EVENTS:
            # ``response.completed`` / ``failed`` / ``incomplete`` carry the
            # usage block (usually nested under ``response``).  Capture it so
            # the caller can write real tokens into request_logs instead of 0/0.
            self._capture_usage(payload)
            return self._finish()
        return []


__all__ = [
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_OPENAI",
    "PROTOCOL_RESPONSES",
    "UpstreamSSEChunkAdapter",
]
