"""Anthropic SSE -> OpenAI SSE state machine translation.

Feeds on raw Anthropic SSE chunks (``event: <type>\\ndata: {json}\\n\\n``) and
produces OpenAI SSE chunk bytes (``data: {json}\\n\\n``).

State machine: ``INIT`` -> ``TEXT_OPEN`` -> ``DONE``.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from loguru import logger


# Anthropic stop_reason -> OpenAI finish_reason
MAP_STOP_REASON_A2O: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "stop_sequence": "stop",
}

# State constants
INIT = "INIT"
TEXT_OPEN = "TEXT_OPEN"
DONE = "DONE"


def _openai_chunk(
    *,
    chunk_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason: str | None = None,
) -> bytes:
    """Serialize a single OpenAI chat.completion.chunk SSE line to bytes."""
    data = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }
    payload = json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def _done_marker() -> bytes:
    return b"data: [DONE]\n\n"


class StreamA2O:
    """Anthropic SSE -> OpenAI SSE translator.

    Usage::

        sm = StreamA2O(model="gpt-4o")
        for chunk in upstream_chunks:
            for out in await sm.feed(chunk):
                write_to_client(out)
        # sm.done() is True after emitting [DONE].
    """

    def __init__(self, model: str = "") -> None:
        self.model = model
        self.state: str = INIT
        self._chunk_id: str = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        self._created: int = int(time.time())
        # Mapping Anthropic content_block index -> OpenAI tool_calls index.
        self._tool_block_index: int | None = None
        self._tool_oai_index: int = 0
        # Map Anthropic block index -> OpenAI tool index.
        self._block_to_oai_tool_index: dict[int, int] = {}
        self._stop_reason: str | None = None
        self._finished: bool = False
        self._buffer: bytes = b""
        # Pending event type for the next data: line (Anthropic uses
        # "event: <type>\ndata: {...}\n\n").
        self._pending_event: str | None = None

    def done(self) -> bool:
        """Whether the stream is finished (after emitting [DONE])."""
        return self._finished

    def finish_safely(self) -> list[bytes]:
        """Synthesize closing events if the stream hasn't finished yet.

        Called by the handler when the upstream HTTP body ends but the
        translator never saw message_stop (e.g. the final SSE event was
        malformed and dropped). Without this, the OpenAI client would hang
        waiting for [DONE] that never arrives.

        Idempotent: if already finished, returns [].
        """
        if self._finished:
            return []
        out: list[bytes] = []
        # Synthesize a closing choice chunk if we never emitted a terminal
        # finish_reason (e.g. the upstream body ended without message_stop).
        finish = self._stop_reason or "stop"
        out.append(_openai_chunk(
            chunk_id=self._chunk_id,
            created=self._created,
            model=self.model,
            delta={},
            finish_reason=finish,
        ))
        out.append(_done_marker())
        self._finished = True
        return out

    async def feed(self, chunk: bytes) -> list[bytes]:
        """Feed a raw Anthropic SSE chunk, return list of OpenAI SSE chunk bytes.

        A single chunk may contain multiple events. Partial lines are buffered
        across calls.
        """
        if self._finished:
            return []

        out: list[bytes] = []
        self._buffer += chunk
        # Split on newlines and reconstruct events.
        while b"\n" in self._buffer:
            line_bytes, self._buffer = self._buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
            if not line:
                # Empty line — event boundary. Reset pending event.
                # (Anthropic SSE: each event is "event: T\ndata: J\n\n".)
                self._pending_event = None
                continue
            if line.startswith(":"):
                # SSE comment / keepalive.
                continue
            if line.startswith("event:"):
                self._pending_event = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                data_str = line[len("data:"):].lstrip()
                try:
                    data = json.loads(data_str)
                except (json.JSONDecodeError, ValueError):
                    logger.warning("StreamA2O: failed to parse SSE data: {}", data_str[:200])
                    self._pending_event = None
                    continue
                event_type = self._pending_event
                out.extend(self._handle_event(event_type, data))
                self._pending_event = None
        return out

    def _handle_event(self, event_type: str | None, data: dict) -> list[bytes]:
        """Dispatch one Anthropic SSE event. Returns OpenAI SSE bytes list."""
        if event_type is None:
            return []
        if event_type == "message_start":
            return self._on_message_start(data)
        if event_type == "content_block_start":
            return self._on_content_block_start(data)
        if event_type == "content_block_delta":
            return self._on_content_block_delta(data)
        if event_type == "content_block_stop":
            # No-op for OpenAI — block boundaries are implicit.
            return []
        if event_type == "message_delta":
            # Record stop_reason; don't emit yet (emit on message_stop).
            delta = data.get("delta") or {}
            sr = delta.get("stop_reason")
            if sr:
                self._stop_reason = sr
            return []
        if event_type == "message_stop":
            return self._on_message_stop()
        if event_type == "ping":
            return []
        if event_type == "error":
            # Pass through as an error chunk + [DONE].
            err = data.get("error") or {}
            msg = err.get("message", "upstream stream error")
            logger.warning("StreamA2O: upstream error event: {}", msg)
            return self._finish_with_error(msg)
        # Unknown event — ignore.
        return []

    def _on_message_start(self, data: dict) -> list[bytes]:
        """Emit the first OpenAI chunk with role:assistant."""
        if self.state != INIT:
            return []
        msg = data.get("message") or {}
        msg_id = msg.get("id") or self._chunk_id
        # Use the upstream message id as the OpenAI chunk id for traceability.
        self._chunk_id = msg_id
        chunk = _openai_chunk(
            chunk_id=self._chunk_id,
            created=self._created,
            model=self.model or msg.get("model", ""),
            delta={"role": "assistant", "content": ""},
            finish_reason=None,
        )
        self.state = TEXT_OPEN
        return [chunk]

    def _on_content_block_start(self, data: dict) -> list[bytes]:
        """Handle content_block_start for text or tool_use blocks."""
        out: list[bytes] = []
        index = data.get("index", 0)
        block = data.get("content_block") or {}
        btype = block.get("type")
        if btype == "text":
            # No emit — OpenAI first delta already has role. Text deltas come
            # via content_block_delta.
            pass
        elif btype == "tool_use":
            tool_id = block.get("id", "")
            tool_name = block.get("name", "")
            oai_tool_index = self._tool_oai_index
            self._tool_oai_index += 1
            self._block_to_oai_tool_index[index] = oai_tool_index
            self._tool_block_index = index
            out.append(_openai_chunk(
                chunk_id=self._chunk_id,
                created=self._created,
                model=self.model,
                delta={
                    "tool_calls": [{
                        "index": oai_tool_index,
                        "id": tool_id,
                        "type": "function",
                        "function": {"name": tool_name, "arguments": ""},
                    }]
                },
                finish_reason=None,
            ))
        return out

    def _on_content_block_delta(self, data: dict) -> list[bytes]:
        """Handle text_delta and input_json_delta."""
        out: list[bytes] = []
        index = data.get("index", 0)
        delta = data.get("delta") or {}
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text", "")
            if text:
                out.append(_openai_chunk(
                    chunk_id=self._chunk_id,
                    created=self._created,
                    model=self.model,
                    delta={"content": text},
                    finish_reason=None,
                ))
        elif dtype == "input_json_delta":
            partial = delta.get("partial_json", "")
            oai_tool_index = self._block_to_oai_tool_index.get(index, 0)
            if partial:
                out.append(_openai_chunk(
                    chunk_id=self._chunk_id,
                    created=self._created,
                    model=self.model,
                    delta={
                        "tool_calls": [{
                            "index": oai_tool_index,
                            "function": {"arguments": partial},
                        }]
                    },
                    finish_reason=None,
                ))
        return out

    def _on_message_stop(self) -> list[bytes]:
        """Emit the final OpenAI chunk with finish_reason + [DONE]."""
        if self._finished:
            return []
        stop_reason = self._stop_reason or "end_turn"
        finish_reason = MAP_STOP_REASON_A2O.get(stop_reason, "stop")
        out = [_openai_chunk(
            chunk_id=self._chunk_id,
            created=self._created,
            model=self.model,
            delta={},
            finish_reason=finish_reason,
        )]
        out.append(_done_marker())
        self.state = DONE
        self._finished = True
        return out

    def _finish_with_error(self, message: str) -> list[bytes]:
        """Emit a final chunk with finish_reason=stop + [DONE] on stream error."""
        if self._finished:
            return []
        out = [_openai_chunk(
            chunk_id=self._chunk_id,
            created=self._created,
            model=self.model,
            delta={"content": f"\n[stream error: {message}]"},
            finish_reason=None,
        )]
        out.append(_openai_chunk(
            chunk_id=self._chunk_id,
            created=self._created,
            model=self.model,
            delta={},
            finish_reason="stop",
        ))
        out.append(_done_marker())
        self.state = DONE
        self._finished = True
        return out
