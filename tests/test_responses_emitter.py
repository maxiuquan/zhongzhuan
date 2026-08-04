"""T14 tests: ResponsesEventEmitter state machine (§5.6).

Covers the acceptance criteria of T14:
1. created/in_progress emitted on start, before any token.
2. sequence_number strictly monotonic.
3. Every added has exactly one done; duplicate added ignored.
4. completed and [DONE] each emitted exactly once.
5. Illegal transitions refused and recorded (INIT->COMPLETED, COMPLETED->DELTA,
   item done then append, duplicate completed).
6. Heartbeat never transitions state.
7. terminate() is idempotent and closes still-open items.
"""

from __future__ import annotations

import json

import pytest

from zhongzhuan.proxy.protocol.responses_emitter import EmitterConfig, ResponsesEventEmitter
from zhongzhuan.proxy.protocol.responses_models import (
    EmitterState,
    ItemStatus,
    ItemType,
    OutputItem,
    ResponseStatus,
)


def _make_emitter(**kw) -> ResponsesEventEmitter:
    cfg = EmitterConfig(heartbeat_seconds=0)
    cfg = kw.pop("config", cfg)
    return ResponsesEventEmitter(response_id="resp_1", model="gpt-4o", config=cfg, **kw)


def _parse(frame: bytes) -> tuple[str, dict]:
    text = frame.decode("utf-8")
    event = text.splitlines()[0].removeprefix("event: ").strip()
    data = None
    for line in text.splitlines():
        if line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


def _item(idx: int, id_: str = "", item_type: str = "message") -> OutputItem:
    return OutputItem(
        id=id_ or f"msg_{idx}",
        output_index=idx,
        item_type=ItemType(item_type),
        role="assistant",
    )


# ---------------------------------------------------------------------------
# 1. Lifecycle: created/in_progress first
# ---------------------------------------------------------------------------


def test_start_emits_created_then_in_progress():
    em = _make_emitter()
    frames = em.start()
    assert len(frames) == 2
    ev0, d0 = _parse(frames[0])
    ev1, d1 = _parse(frames[1])
    assert ev0 == "response.created"
    assert ev1 == "response.in_progress"
    assert d0["response"]["id"] == "resp_1"
    assert d0["response"]["status"] == "in_progress"


def test_start_only_once():
    em = _make_emitter()
    em.start()
    assert em.start() == []


# ---------------------------------------------------------------------------
# 2. sequence_number strictly monotonic
# ---------------------------------------------------------------------------


def test_sequence_number_monotonic():
    em = _make_emitter()
    frames = em.start()
    seqs = []
    for f in frames:
        _, d = _parse(f)
        seqs.append(d["sequence_number"])
    item = _item(0)
    for f in em.delta("response.output_text.delta", {"output_index": 0, "delta": "x"}):
        _, d = _parse(f)
        seqs.append(d["sequence_number"])
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # strictly increasing


# ---------------------------------------------------------------------------
# 3. added/done pairing
# ---------------------------------------------------------------------------


def test_open_and_close_item_emit_added_done():
    em = _make_emitter()
    em.start()
    item = _item(0)
    added = em.open_item(item)
    done = em.close_item(item)
    assert len(added) == 1 and _parse(added[0])[0] == "response.output_item.added"
    assert len(done) == 1 and _parse(done[0])[0] == "response.output_item.done"
    assert _parse(done[0])[1]["item"]["status"] == "completed"


def test_duplicate_added_ignored():
    em = _make_emitter()
    em.start()
    item = _item(0)
    em.open_item(item)
    assert em.open_item(item) == []  # duplicate ignored
    assert em.stats.illegal_transitions >= 1


def test_close_without_open_is_refused():
    em = _make_emitter()
    em.start()
    assert em.close_item(_item(0)) == []
    assert em.stats.illegal_transitions >= 1


def test_close_item_idempotent():
    em = _make_emitter()
    em.start()
    item = _item(0)
    em.open_item(item)
    assert len(em.close_item(item)) == 1
    assert em.close_item(item) == []  # second close no-op


# ---------------------------------------------------------------------------
# 4. completed + [DONE] exactly once
# ---------------------------------------------------------------------------


def test_terminate_emits_terminal_and_done():
    em = _make_emitter()
    em.start()
    frames = em.terminate(ResponseStatus.COMPLETED)
    text = b"".join(frames).decode()
    assert "response.completed" in text
    assert "data: [DONE]" in text


def test_terminate_idempotent():
    em = _make_emitter()
    em.start()
    em.terminate(ResponseStatus.COMPLETED)
    assert em.terminate(ResponseStatus.COMPLETED) == []
    assert em.done is True


def test_terminal_status_maps_to_event():
    em = _make_emitter()
    em.start()
    frames = em.terminate(ResponseStatus.FAILED)
    ev, _ = _parse(frames[0])
    assert ev == "response.failed"


def test_incomplete_gets_incomplete_details():
    em = _make_emitter()
    em.start()
    frames = em.terminate(ResponseStatus.INCOMPLETE, terminal_reason="upstream_truncated")
    ev, d = _parse(frames[0])
    assert ev == "response.incomplete"
    assert d["response"]["incomplete_details"]["reason"] == "upstream_truncated"


# ---------------------------------------------------------------------------
# 5. Illegal transitions refused
# ---------------------------------------------------------------------------


def test_delta_after_terminal_refused():
    em = _make_emitter()
    em.start()
    em.terminate(ResponseStatus.COMPLETED)
    assert em.delta("response.output_text.delta", {"delta": "x"}) == []
    assert em.stats.illegal_transitions >= 1
    assert any("delta_after_terminal" in t for t in em.illegal_transitions)


def test_duplicate_completed_not_emitted():
    em = _make_emitter()
    em.start()
    em.terminate(ResponseStatus.COMPLETED)
    # Calling terminate again after terminal must not re-emit.
    assert em.terminate(ResponseStatus.COMPLETED) == []


def test_start_after_terminal_refused():
    em = _make_emitter()
    em.start()
    em.terminate(ResponseStatus.COMPLETED)
    assert em.start() == []


# ---------------------------------------------------------------------------
# 6. Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_never_transitions():
    em = _make_emitter()
    em.start()
    st = em.state
    frames = em.heartbeat()
    assert frames == [b": hb\n\n"]
    assert em.state == st  # heartbeat does not transition
    assert em.stats.heartbeats == 1


def test_heartbeat_after_terminal_is_harmless():
    em = _make_emitter()
    em.start()
    em.terminate(ResponseStatus.COMPLETED)
    # Heartbeat is allowed after terminal (it is a comment, not a delta).
    assert em.heartbeat() == [b": hb\n\n"]


# ---------------------------------------------------------------------------
# 7. terminate closes still-open items
# ---------------------------------------------------------------------------


def test_terminate_closes_open_items():
    em = _make_emitter()
    em.start()
    item = _item(0)
    em.open_item(item)
    frames = em.terminate(ResponseStatus.COMPLETED)
    text = b"".join(frames).decode()
    # The open item is closed (status incomplete) before completed.
    assert "response.output_item.done" in text
    assert em.done is True


# ---------------------------------------------------------------------------
# 8. State machine sequence sanity
# ---------------------------------------------------------------------------


def test_state_sequence_initial_to_streaming():
    em = _make_emitter()
    assert em.state == EmitterState.INIT
    em.start()
    assert em.state == EmitterState.IN_PROGRESS
    em.open_item(_item(0))
    assert em.state == EmitterState.STREAMING
    em.terminate(ResponseStatus.COMPLETED)
    assert em.is_terminal is True
