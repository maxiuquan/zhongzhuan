"""T20 tests: idempotency_records persistence (8th category, R-P1-29) + TiDB (R-P0-11 ⑥)."""
from __future__ import annotations

import os

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store


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
async def test_idempotency_record_roundtrip(store):
    """Criterion ④ (8th category): idempotency_records read back correctly."""
    rs = ResponseStore(store)
    await rs.save_idempotency_record(
        workspace_id="t1",
        idempotency_key="k1",
        request_digest="d1",
        response_id="resp_1",
        status_code=200,
        state="done",
    )
    row = await rs.get_idempotency_record("t1", "k1")
    assert row is not None
    response_id, status_code, state = row
    assert response_id == "resp_1" and status_code == 200 and state == "done"
    # tenant isolation on the idempotency keys
    assert await rs.get_idempotency_record("t2", "k1") is None


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("ZHONGZHUAN_TIDB_HOST"),
    reason="needs a live TiDB instance (set ZHONGZHUAN_TIDB_HOST)",
)
async def test_tidb_idempotency_unique_constraint():
    """Criterion ⑥: duplicate (workspace_id, idempotency_key) rejected by TiDB."""
    store = await create_store(default_config())
    try:
        rs = ResponseStore(store)
        await rs.save_idempotency_record(
            workspace_id="t1", idempotency_key="dup", state="in_flight"
        )
        with pytest.raises(Exception):
            await rs.save_idempotency_record(
                workspace_id="t1", idempotency_key="dup", state="in_flight"
            )
    finally:
        await store.close()
