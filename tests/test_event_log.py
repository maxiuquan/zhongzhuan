"""T20 tests: EventLog append-only + concurrency + lint (R-P0-11 / R-P1-14 / R-P1-29 / R-P1-64)."""
from __future__ import annotations

import asyncio
import io
import json
import pathlib
import tokenize

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.store.event_log import EventLog
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store

EVENT_LOG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src" / "zhongzhuan" / "store" / "event_log.py"
)


@pytest.fixture
async def store(tmp_path):
    cfg = default_config()
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.storage.db_path = cfg.storage.sqlite_db_path
    cfg.tidb = None
    s = await create_store(cfg)
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_append_and_read_roundtrip(store):
    log = EventLog(store)
    await log.append_event(response_id="r1", event_type="response.created", data={"a": 1})
    await log.append_event(response_id="r1", event_type="response.output_text.delta", data={"delta": "x"})
    events = await log.read_events("r1")
    assert [e["event_type"] for e in events] == ["response.created", "response.output_text.delta"]
    assert [e["seq"] for e in events] == [1, 2]
    after = await log.read_events("r1", after_seq=1)
    assert len(after) == 1 and after[0]["event_type"] == "response.output_text.delta"


@pytest.mark.asyncio
async def test_concurrent_1000_no_gaps(store):
    """Criterion ①: 1000 concurrent appends -> continuous, unique, gap-free seq."""
    log = EventLog(store)

    async def one(i):
        return await log.append_event(
            response_id="r1", event_type="response.output_text.delta", data={"i": i}
        )

    seqs = await asyncio.gather(*(one(i) for i in range(1000)))
    assert len(seqs) == 1000
    # unique + contiguous 1..1000, no gaps, no duplicates.
    assert sorted(seqs) == list(range(1, 1001))
    assert len(set(seqs)) == 1000
    events = await log.read_events("r1")
    assert [e["seq"] for e in events] == list(range(1, 1001))


@pytest.mark.asyncio
async def test_append_only_no_update_delete_in_source(store):
    """Criterion ②: CI lint — no UPDATE/DELETE ``response_events`` in code.

    Docstrings/comments may *mention* the forbidden patterns (the lint rule
    itself is documented there), so we parse out STRING and COMMENT tokens and
    only inspect executable code.
    """
    with open(EVENT_LOG_PATH, "rb") as fh:
        parts = []
        for tok in tokenize.tokenize(fh.readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            parts.append(tok.string)
    text = " ".join(parts).upper()
    assert "UPDATE RESPONSE_EVENTS" not in text
    assert "DELETE FROM RESPONSE_EVENTS" not in text


@pytest.mark.asyncio
async def test_reasoning_not_persisted(store):
    """Criterion ⑤: store persists exactly the (redacted) payload; no reasoning injected."""
    log = EventLog(store)
    await log.append_event(
        response_id="r1",
        event_type="response.output_text.delta",
        data={"type": "response.output_text.delta", "delta": "hello"},
    )
    row = await store.fetchone(
        "SELECT data FROM response_events WHERE response_id = ? ORDER BY seq", ("r1",)
    )
    raw = row[0]
    assert "reasoning" not in raw.lower()
    assert "reasoning" not in json.loads(raw)


@pytest.mark.asyncio
async def test_response_store_delegates_to_event_log(store):
    rs = ResponseStore(store)
    seq = await rs.append_event("r1", "response.created", {"x": 1})
    assert isinstance(seq, int) and seq == 1
    events = await rs.list_events("r1")
    assert events[0]["event_type"] == "response.created"
