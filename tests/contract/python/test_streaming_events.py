"""T37 criterion ③: full streaming event schema through the official SDK.

The v3 skeleton's live streaming pipeline is wired in T24/T28; the SDK contract
here consumes the **native OpenAI Responses SSE** produced by the deterministic
mock upstream (``support.mock_responses_upstream.responses_text_stream``) and
asserts the official event schema is parseable end-to-end through
``AsyncOpenAI.responses.create(..., stream=True)``.

The mock covers the official minimal event set:
``response.created`` → ``response.in_progress`` → ``response.output_item.added``
→ ``response.content_part.added`` → ``response.output_text.delta`` ×N →
``response.output_text.done`` → ``response.content_part.done`` →
``response.output_item.done`` → ``response.completed`` → ``[DONE]``.

Every event carries ``type`` and ``sequence_number`` per the official schema;
the SDK exposes them as typed objects.  A mutated payload (missing a delta or
reordered events) must fail the sequence assertions below -- verified by the
mutation runs in the task report.
"""

from __future__ import annotations

import pytest

from support.mock_responses_upstream import MockUpstream, UpstreamBehavior, responses_text_stream

#: Official minimal event set in the order emitted by the deterministic mock.
EXPECTED_EVENT_TYPES: tuple[str, ...] = (
    "response.created",
    "response.in_progress",
    "response.output_item.added",
    "response.content_part.added",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.delta",
    "response.output_text.done",
    "response.content_part.done",
    "response.output_item.done",
    "response.completed",
)


@pytest.fixture
async def native_stream_client():
    """SDK client wired to a MockUpstream serving native Responses SSE."""
    up = MockUpstream()
    up.set_behavior(UpstreamBehavior(stream_payload=responses_text_stream()))
    await up.start()
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=up.url, api_key="sk-test", max_retries=0, timeout=30.0)
    try:
        yield client, up
    finally:
        await client.close()
        await up.stop()


@pytest.mark.asyncio
async def test_streaming_full_event_schema(native_stream_client):
    """The SDK parses the full official event sequence with sequence numbers."""
    client, _up = native_stream_client

    stream = await client.responses.create(model="gpt-4o", input="hi", stream=True)
    events = [e async for e in stream]

    types = [e.type for e in events]
    assert tuple(types) == EXPECTED_EVENT_TYPES

    # sequence_number is monotonic from 0 on every event (official schema).
    seqs = [e.sequence_number for e in events]
    assert seqs == list(range(len(events)))

    # Delta payloads are surfaced verbatim: pieces are concatenated in order.
    deltas = [e.delta for e in events if e.type == "response.output_text.delta"]
    assert "".join(deltas) == "Hello, world!"


@pytest.mark.asyncio
async def test_streaming_accumulates_final_text(native_stream_client):
    """response.completed carries the assembled text via output/response."""
    client, _up = native_stream_client

    stream = await client.responses.create(model="gpt-4o", input="hi", stream=True)
    events = [e async for e in stream]

    done = [e for e in events if e.type == "response.output_text.done"]
    assert len(done) == 1
    assert done[0].text == "Hello, world!"

    completed = [e for e in events if e.type == "response.completed"]
    assert len(completed) == 1
    response = completed[0].response
    assert response.status == "completed"
    assert response.model == "upstream-model"


@pytest.mark.asyncio
async def test_streaming_typed_event_classes(native_stream_client):
    """The SDK maps each event to its official typed class."""
    from openai.types.responses import (
        ResponseCompletedEvent,
        ResponseContentPartAddedEvent,
        ResponseCreatedEvent,
        ResponseInProgressEvent,
        ResponseOutputItemAddedEvent,
        ResponseOutputItemDoneEvent,
        ResponseTextDeltaEvent,
        ResponseTextDoneEvent,
    )

    client, _up = native_stream_client
    stream = await client.responses.create(model="gpt-4o", input="hi", stream=True)
    events = [e async for e in stream]

    by_type: dict[str, object] = {e.type: e for e in events}
    assert isinstance(by_type["response.created"], ResponseCreatedEvent)
    assert isinstance(by_type["response.in_progress"], ResponseInProgressEvent)
    assert isinstance(by_type["response.output_item.added"], ResponseOutputItemAddedEvent)
    assert isinstance(by_type["response.content_part.added"], ResponseContentPartAddedEvent)
    assert isinstance(by_type["response.output_text.delta"], ResponseTextDeltaEvent)
    assert isinstance(by_type["response.output_text.done"], ResponseTextDoneEvent)
    assert isinstance(by_type["response.output_item.done"], ResponseOutputItemDoneEvent)
    assert isinstance(by_type["response.completed"], ResponseCompletedEvent)
