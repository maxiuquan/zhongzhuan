"""Minimal Responses streaming pipeline skeleton (T21 / R-P1-32, R-P0-40).

Drives the canonical event sequence for a response stream on top of an
injectable upstream.  The full upstream translation / tool loop / budget
integration lands in T24/T28; this skeleton proves the
``created -> in_progress -> completed -> [DONE]`` ordering required by T21
criterion ⑤ (an upstream that produces 0 bytes must end in a graceful
``completed`` + ``[DONE]``, never hang or 500).

The pipeline also persists every emitted event through :class:`EventLog` so the
catch-up stream (T24) can replay it.  Sequence numbers are allocated from 0
here; in production the canonical ``seq`` comes from ``ResponsesEventEmitter``
(T16).
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterable

from ..proxy.protocol.responses_models import SSE_DONE_FRAME
from ..store.response_store import ResponseStore


def sse_frame(event_type: str, data: dict[str, Any]) -> bytes:
    """Render one SSE frame.

    Public because the catch-up stream (T24) must produce **byte-identical**
    frames to the live stream -- sharing this function is what makes that a
    property of the code rather than a comment (R-P1-36).
    """
    return (
        f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


#: Historical private alias kept for existing call sites inside this module.
_sse = sse_frame


class ResponsePipeline:
    """Yield the SSE frames for a single response stream."""

    def __init__(
        self,
        response_id: str,
        *,
        workspace_id: str = "",
        store: ResponseStore | None = None,
    ) -> None:
        self.response_id = response_id
        self.workspace_id = workspace_id
        self._store = store
        self._seq = 0

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

    async def run(self, upstream: AsyncIterable[Any]) -> AsyncIterable[bytes]:
        """Stream the canonical sequence, translating nothing yet."""
        yield await self._emit(
            "response.created",
            {"type": "response.created", "response": {"id": self.response_id, "status": "in_progress"}},
        )
        yield await self._emit(
            "response.in_progress",
            {"type": "response.in_progress", "response": {"id": self.response_id, "status": "in_progress"}},
        )
        produced = False
        async for _chunk in upstream:
            produced = True
            # Skeleton: real chunk -> delta translation is T24/T28.
        if not produced:
            yield await self._emit(
                "response.completed",
                {"type": "response.completed", "response": {"id": self.response_id, "status": "completed"}},
            )
        yield SSE_DONE_FRAME


__all__ = ["ResponsePipeline", "sse_frame"]
