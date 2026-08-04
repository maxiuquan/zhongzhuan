"""Catch-up replay for detached / reconnecting clients (T24 / R-P1-36).

A background response outlives the connection that created it, so the client
needs a way to ask "give me everything that happened, starting after event
N".  :class:`CatchupStream` is that endpoint's engine.

The property R-P1-36 demands is *identity*, not similarity: the sequence a
reconnecting client replays must match the live stream event for event, with
the same ``sequence_number`` on each.  This module gets that for free by
construction rather than by translation --

* there is exactly **one** event log (``response_events``); the live pipeline
  writes into it as it streams, and catch-up reads back out of it, so the
  ``seq`` a replayed event carries *is* the ``seq`` the live event carried;
* frames are rendered with :func:`~.pipeline.sse_frame`, the same function the
  live path uses, so the bytes are identical too.

The alternative design -- a separate "replay log" written after the fact --
would need the two writers to agree forever on ordering, numbering and
formatting.  Sharing one log makes divergence impossible instead of unlikely.

``[DONE]`` is deliberately **not** appended: whether the replay is the end of
the stream or a prelude to live tailing is the caller's decision, and emitting
a terminator here would make the tailing case malformed.
"""

from __future__ import annotations

from typing import Any, AsyncIterable

from ..store.response_store import ResponseStore
from .pipeline import sse_frame


class CatchupStream:
    """Replay a response's persisted event log as SSE frames."""

    def __init__(self, store: ResponseStore) -> None:
        self._store = store

    async def replay(
        self,
        response_id: str,
        *,
        after_seq: int = -1,
    ) -> AsyncIterable[bytes]:
        """Yield every stored event after ``after_seq``, in ``seq`` order."""
        for event in await self._store.list_events(response_id, after_seq=after_seq):
            yield sse_frame(str(event["event_type"]), dict(event["data"]))

    async def events(
        self,
        response_id: str,
        *,
        after_seq: int = -1,
    ) -> list[dict[str, Any]]:
        """The raw ``[{seq, event_type, data}, ...]`` behind :meth:`replay`."""
        return await self._store.list_events(response_id, after_seq=after_seq)

    async def last_seq(self, response_id: str) -> int:
        """Highest persisted ``seq``, or ``-1`` when nothing was logged yet."""
        events = await self._store.list_events(response_id)
        return int(events[-1]["seq"]) if events else -1


__all__ = ["CatchupStream"]
