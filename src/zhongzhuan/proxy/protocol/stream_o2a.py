"""OpenAI SSE -> Anthropic SSE state machine translation.

Feeds on raw OpenAI SSE chunks (``data: {json}\\n\\n``) and produces Anthropic
SSE event bytes (``event: <type>\\ndata: {json}\\n\\n``).

State machine: ``INIT`` -> ``TEXT_BLOCK`` -> ``TOOL_BLOCK`` -> ``DONE``.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from loguru import logger


# OpenAI finish_reason -> Anthropic stop_reason
MAP_FINISH_REASON_O2A: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "function_call": "tool_use",
}

# State constants
INIT = "INIT"          # message_start not yet emitted
STARTED = "STARTED"    # message_start emitted, no content block open yet
TEXT_BLOCK = "TEXT_BLOCK"
TOOL_BLOCK = "TOOL_BLOCK"
DONE = "DONE"


def _sse_event(event: str, data: dict) -> bytes:
    """Serialize a single Anthropic SSE event to bytes."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


class StreamO2A:
    """OpenAI SSE -> Anthropic SSE translator.

    Usage::

        sm = StreamO2A(model="claude-sonnet-4-5")
        for chunk in upstream_chunks:
            for out in await sm.feed(chunk):
                write_to_client(out)
        # sm.done() is True after [DONE] or finish_reason processed.
    """

    def __init__(self, model: str = "") -> None:
        self.model = model
        self.state: str = INIT
        self._message_id: str = f"msg_{uuid.uuid4().hex[:24]}"
        self._current_index: int = 0
        # Track tool_use blocks by OpenAI tool_call index -> Anthropic content
        # block index. OpenAI delta.tool_calls[].index is the per-call index.
        self._tool_index_map: dict[int, int] = {}
        self._next_block_index: int = 0
        self._output_chars: int = 0  # rough estimate of output tokens
        self._finished: bool = False
        self._buffer: bytes = b""  # incomplete SSE line buffer
        # Diagnostic counters: track what we actually saw from upstream.
        # reasoning_content is the "thinking" field some providers (e.g. agnes)
        # emit before the final answer — Anthropic has no equivalent in our
        # current translation, so we drop it. Counting it lets us warn when a
        # "empty" client reply was actually caused by content being routed
        # into reasoning_content only.
        self._reasoning_chars: int = 0
        self._content_chars: int = 0
        self._tool_call_count: int = 0

    def done(self) -> bool:
        """Whether the stream is finished (after emitting message_stop)."""
        return self._finished

    def finish_safely(self) -> list[bytes]:
        """Synthesize closing events if the stream hasn't finished yet.

        Called by the handler when the upstream HTTP body ends but the
        translator never saw [DONE] or finish_reason (e.g. the final SSE
        event was malformed and dropped, which happens with upstreams that
        pack multiple events per HTTP chunk and split a JSON payload across
        TCP segment boundaries). Without this, the Anthropic client would
        hang waiting for a message_stop that never arrives.

        Idempotent: if already finished, returns [].
        """
        if self._finished:
            return []
        # Treat as end_turn since we have no real finish_reason from upstream.
        return self._finish(finish_reason=None)

    async def feed(self, chunk: bytes) -> list[bytes]:
        """Feed a raw OpenAI SSE chunk, return list of Anthropic SSE event bytes.

        Uses SSE event-boundary parsing: events are separated by a blank line
        (``\\n\\n``). A single event's ``data:`` line may be split across
        multiple raw TCP chunks, so we accumulate bytes until a full event
        boundary (blank line) is present before attempting to parse it.

        This is more robust than line-based parsing for upstream providers
        (e.g. agnes-2.0-flash) that pack multiple SSE events into one HTTP
        chunk and split a single event's JSON payload across TCP segments.
        """
        if self._finished:
            return []

        out: list[bytes] = []
        self._buffer += chunk
        # Split on \n\n (SSE event boundary). Anything after the last \n\n is
        # a partial event — keep it in the buffer until the next chunk.
        while b"\n\n" in self._buffer:
            event_bytes, self._buffer = self._buffer.split(b"\n\n", 1)
            # An event may contain multiple "data:" lines (for multi-line
            # values); OpenAI uses single-line data, so just concatenate.
            data_lines: list[str] = []
            for raw_line in event_bytes.split(b"\n"):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
                # event:, id:, retry: etc. — ignore for OpenAI compat.
            if not data_lines:
                continue
            data_str = "\n".join(data_lines)
            if data_str.strip() == "[DONE]":
                out.extend(self._finish(finish_reason=None))
                continue
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                # Should not happen after event-boundary split — if it does,
                # the upstream sent a malformed event. Log and drop.
                logger.warning(
                    "StreamO2A: failed to parse SSE event: {}", data_str[:200]
                )
                continue
            out.extend(self._handle_openai_chunk(data))
        return out

    def _handle_openai_chunk(self, data: dict) -> list[bytes]:
        """Process one parsed OpenAI chunk dict. Returns Anthropic SSE bytes."""
        out: list[bytes] = []
        choices = data.get("choices") or []
        if not choices:
            # Could be a usage-only chunk; ignore for now.
            return out
        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        # If this is the first chunk with role, emit message_start + ping.
        if self.state == INIT:
            out.extend(self._emit_message_start(data))
            out.append(_sse_event("ping", {}))
            self.state = STARTED

        # Handle text content delta.
        content_delta = delta.get("content")
        if content_delta:
            self._output_chars += len(str(content_delta))
            self._content_chars += len(str(content_delta))
            if self.state == STARTED:
                # Open text block at index 0.
                self._current_index = 0
                self._next_block_index = 1
                out.append(_sse_event("content_block_start", {
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }))
                self.state = TEXT_BLOCK
            if self.state == TEXT_BLOCK:
                out.append(_sse_event("content_block_delta", {
                    "index": 0,
                    "delta": {"type": "text_delta", "text": str(content_delta)},
                }))
            elif self.state == TOOL_BLOCK:
                # Text after tool calls — open a new text block.
                self._current_index = self._next_block_index
                self._next_block_index += 1
                out.append(_sse_event("content_block_start", {
                    "index": self._current_index,
                    "content_block": {"type": "text", "text": ""},
                }))
                out.append(_sse_event("content_block_delta", {
                    "index": self._current_index,
                    "delta": {"type": "text_delta", "text": str(content_delta)},
                }))
                self.state = TEXT_BLOCK

        # Handle tool_calls delta.
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            self._tool_call_count += len(tool_calls)
            out.extend(self._handle_tool_calls(tool_calls))

        # Track reasoning_content: some providers (e.g. agnes, deepseek-r1)
        # emit delta.reasoning_content for the model's internal "thinking"
        # before the final answer. Anthropic has no equivalent in our current
        # translation (we'd need a thinking content block), so we drop it.
        # Count it here so the empty-completion warning can distinguish
        # "upstream really returned nothing" from "upstream only sent thinking".
        reasoning_delta = delta.get("reasoning_content")
        if reasoning_delta:
            self._reasoning_chars += len(str(reasoning_delta))

        # Handle finish_reason or [DONE].
        if finish_reason:
            out.extend(self._finish(finish_reason=finish_reason))

        return out

    def _emit_message_start(self, first_chunk: dict) -> list[bytes]:
        """Emit the Anthropic ``message_start`` event."""
        msg_id = first_chunk.get("id") or self._message_id
        model = self.model or first_chunk.get("model", "")
        return [_sse_event("message_start", {
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        })]

    def _handle_tool_calls(self, tool_calls: list[dict]) -> list[bytes]:
        """Handle OpenAI delta.tool_calls array. Returns Anthropic SSE bytes."""
        out: list[bytes] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            oai_idx = tc.get("index", 0)
            fn = tc.get("function") or {}
            # First time we see this tool_call index?
            if oai_idx not in self._tool_index_map:
                # Close any open text block first.
                if self.state == TEXT_BLOCK:
                    out.append(_sse_event("content_block_stop", {"index": 0}))
                elif self.state == TOOL_BLOCK:
                    # Close previous tool block.
                    out.append(_sse_event(
                        "content_block_stop",
                        {"index": self._current_index},
                    ))
                # Allocate a new content block index for this tool_use.
                block_index = self._next_block_index
                self._next_block_index += 1
                self._tool_index_map[oai_idx] = block_index
                self._current_index = block_index
                self.state = TOOL_BLOCK
                tool_id = tc.get("id") or f"toolu_{uuid.uuid4().hex[:22]}"
                tool_name = fn.get("name", "")
                out.append(_sse_event("content_block_start", {
                    "index": block_index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": tool_name,
                        "input": {},
                    },
                }))
                # If arguments chunk came in the same delta, emit it.
                args_partial = fn.get("arguments")
                if args_partial:
                    out.append(_sse_event("content_block_delta", {
                        "index": block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_partial,
                        },
                    }))
            else:
                block_index = self._tool_index_map[oai_idx]
                args_partial = fn.get("arguments")
                if args_partial:
                    out.append(_sse_event("content_block_delta", {
                        "index": block_index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args_partial,
                        },
                    }))
        return out

    def _finish(self, finish_reason: str | None) -> list[bytes]:
        """Emit closing events: content_block_stop, message_delta, message_stop."""
        if self._finished:
            return []

        # Detect empty completion: upstream returned no text/tool content delta.
        # This usually means upstream returned an empty stream (e.g. only [DONE],
        # or chunks with empty delta.content), which silently produces an
        # Anthropic message with no content blocks — the client sees a
        # "successful but empty" reply and the user thinks nothing happened.
        # Log loudly so the root cause (upstream empty stream) is visible.
        # Distinguish three sub-cases so the operator can act:
        #   - content=0, reasoning=0: upstream really returned nothing
        #   - content=0, reasoning>0: model only produced thinking, no answer
        #     (common with reasoning models hitting content_filter or wrong
        #     max_tokens targeting content not thinking)
        #   - content>0: not actually empty — should not fire (sanity check)
        if self._content_chars == 0 and self._tool_call_count == 0:
            if self._reasoning_chars > 0:
                logger.warning(
                    "StreamO2A: empty completion but upstream sent {} chars of "
                    "reasoning_content (thinking) — model produced only thinking, "
                    "no final answer. finish_reason={}, state={}. "
                    "Check upstream max_tokens / reasoning_effort / content_filter.",
                    self._reasoning_chars, finish_reason, self.state,
                )
            else:
                logger.warning(
                    "StreamO2A: empty completion — finish_reason={}, state={}, "
                    "no content/tool/reasoning delta received from upstream. "
                    "Likely upstream returned empty stream (check upstream model/"
                    "key/max_tokens or content_filter).",
                    finish_reason, self.state,
                )

        out: list[bytes] = []
        # If we never saw a chunk with choices (e.g. only [DONE] arrived),
        # emit message_start + ping first so the Anthropic stream is well-formed.
        if self.state == INIT:
            out.extend(self._emit_message_start({}))
            out.append(_sse_event("ping", {}))
            self.state = STARTED
        # Close any open content block.
        if self.state in (TEXT_BLOCK, TOOL_BLOCK):
            out.append(_sse_event("content_block_stop", {"index": self._current_index}))
        # Determine stop_reason.
        if finish_reason is None:
            # [DONE] without explicit finish_reason — assume end_turn.
            stop_reason = "end_turn"
        else:
            stop_reason = MAP_FINISH_REASON_O2A.get(finish_reason, "end_turn")
        # Rough output token estimate: chars / 4.
        output_tokens = max(1, self._output_chars // 4)
        out.append(_sse_event("message_delta", {
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }))
        out.append(_sse_event("message_stop", {}))
        self.state = DONE
        self._finished = True
        return out
