"""T20 tests: BatchWriter commit-bounding (R-P1-64 / R-P0-11)."""
from __future__ import annotations

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.store.store import create_store
from zhongzhuan.store.writer_queue import BatchWriter

RESPONSE_EVENTS_COLS = (
    "response_id", "seq", "workspace_id", "event_type", "data", "ts", "expires_at",
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
async def test_batch_commit_le_batches(store):
    """Criterion ③: 1000 rows / max_batch 200 -> flush_count == 5 (<= batches)."""
    w = BatchWriter(
        store, table="response_events", columns=RESPONSE_EVENTS_COLS, max_batch=200
    )
    for i in range(1000):
        await w.add(
            {
                "response_id": "r1",
                "seq": i + 1,
                "workspace_id": "",
                "event_type": "response.output_text.delta",
                "data": '{"i":%d}' % i,
                "ts": i,
                "expires_at": 0,
            }
        )
    await w.close()
    assert w.written == 1000
    assert w.flush_count == 5  # 1000 / 200 == 5 batches, one commit each
    rows = await store.fetchall(
        "SELECT COUNT(*) FROM response_events WHERE response_id = ?", ("r1",)
    )
    assert rows[0][0] == 1000


@pytest.mark.asyncio
async def test_partial_flush_on_close(store):
    w = BatchWriter(
        store, table="response_events", columns=RESPONSE_EVENTS_COLS, max_batch=200
    )
    for i in range(250):  # one full flush (200) + 50 left buffered
        await w.add(
            {
                "response_id": "r2",
                "seq": i + 1,
                "workspace_id": "",
                "event_type": "x",
                "data": "{}",
                "ts": i,
                "expires_at": 0,
            }
        )
    assert w.flush_count == 1  # only the full batch has flushed so far
    await w.close()
    assert w.flush_count == 2  # remaining 50 flushed on close
    assert w.written == 250
