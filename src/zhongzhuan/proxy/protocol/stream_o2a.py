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

    def done(self) -> bool:
        """Whether the stream is finished (after emitting message_stop)."""
        return self._finished

    async def feed(self, chunk: bytes) -> list[bytes]:
        """Feed a raw OpenAI SSE chunk, return list of Anthropic SSE event bytes.

        A single chunk may contain multiple ``data:`` lines. Partial lines are
        buffered across calls.
        """
        if self._finished:
            return []

        out: list[bytes] = []
        # Append to buffer and split into complete lines.
        self._buffer += chunk
        # SSE events are separated by \n\n; but OpenAI uses "data: ...\n\n".
        # Process line-by-line, accumulating data: lines.
        while b"\n" in self._buffer:
            line_bytes, self._buffer = self._buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
            if not line:
                continue
            if line.startswith(":"):
                # SSE comment / keepalive — ignore.
                continue
            if line.startswith("data:"):
                data_str = line[len("data:"):].lstrip()
                if data_str.strip() == "[DONE]":
                    out.extend(self._finish(finish_reason=None))
                    continue
                try:
                    data = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("StreamO2A: failed to parse SSE data: {}", data_str[:200])
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
            out.extend(self._handle_tool_calls(tool_calls))

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
