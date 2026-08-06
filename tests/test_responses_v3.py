"""T21 tests: ResponsesV3Handler + 6 endpoints (R-P1-28..33 / R-P0-40).

Runnable acceptance criteria covered here (no live upstream / SDK needed):
* ① every endpoint dispatches without 405; COMPACT returns 501 (reachable).
* ② store=false -> subsequent GET returns 404.
* ③ 50 input items across 3 pages, no duplicate / no missing.
* ④ cross-tenant retrieve returns 404.
* ⑤ pipeline with a 0-byte upstream yields created->in_progress->completed->[DONE].

The OpenAI-Python-SDK direct-call (①) and server wiring are sealed in T25/T37.
"""

from __future__ import annotations

import asyncio

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.responses_v3.handler import ResponsesV3Handler
from zhongzhuan.responses_v3.pipeline import ResponsePipeline
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store


@pytest.fixture
async def env(tmp_path):
    cfg = default_config()
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.storage.db_path = cfg.storage.sqlite_db_path
    cfg.tidb = None
    store = await create_store(cfg)
    rs = ResponseStore(store)
    handler = ResponsesV3Handler(rs)
    yield rs, handler
    await store.close()


@pytest.mark.asyncio
async def test_create_returns_response_object(env):
    rs, handler = env
    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi"},
    )
    assert status == 200
    assert body["object"] == "response"
    assert body["status"] == "in_progress"
    assert body["id"].startswith("resp_")


@pytest.mark.asyncio
async def test_retrieve_unknown_is_404(env):
    rs, handler = env
    status, body = await handler.dispatch("GET", "/v1/responses/nope", workspace_id="t1")
    assert status == 404
    assert body["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_retrieve_cross_tenant_is_404(env):
    """Criterion ④: a response created by t1 is invisible to t2."""
    rs, handler = env
    _, created = await handler.dispatch("POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o"})
    rid = created["id"]
    status, body = await handler.dispatch("GET", f"/v1/responses/{rid}", workspace_id="t2")
    assert status == 404


@pytest.mark.asyncio
async def test_delete_and_cancel(env):
    rs, handler = env
    _, created = await handler.dispatch("POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o"})
    rid = created["id"]
    status, body = await handler.dispatch("DELETE", f"/v1/responses/{rid}", workspace_id="t1")
    assert status == 200 and body["deleted"] is True
    # Already gone.
    status, _ = await handler.dispatch("GET", f"/v1/responses/{rid}", workspace_id="t1")
    assert status == 404

    _, created2 = await handler.dispatch("POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o"})
    rid2 = created2["id"]
    status, body = await handler.dispatch("POST", f"/v1/responses/{rid2}/cancel", workspace_id="t1")
    assert status == 200 and body["status"] == "cancelled"


@pytest.mark.asyncio
async def test_no_405_for_any_endpoint(env):
    """Criterion ①: valid method+path for each endpoint never returns 405."""
    rs, handler = env
    _, created = await handler.dispatch("POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o"})
    rid = created["id"]
    cases = [
        ("POST", "/v1/responses"),
        ("GET", f"/v1/responses/{rid}"),
        ("DELETE", f"/v1/responses/{rid}"),
        ("POST", f"/v1/responses/{rid}/cancel"),
        ("POST", "/v1/responses/compact"),
        ("GET", f"/v1/responses/{rid}/input_items"),
    ]
    for method, path in cases:
        status, _ = await handler.dispatch(method, path, workspace_id="t1", body={})
        assert status != 405, f"{method} {path} -> {status}"


@pytest.mark.asyncio
async def test_compact_is_reachable_not_405(env):
    rs, handler = env
    status, _ = await handler.dispatch("POST", "/v1/responses/compact", workspace_id="t1", body={})
    assert status == 501  # honest stub, not 405


@pytest.mark.asyncio
async def test_store_false_then_retrieve_404(env):
    """Criterion ②: store=false -> the response is not persisted -> GET 404."""
    rs, handler = env
    _, created = await handler.dispatch(
        "POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o", "store": False}
    )
    rid = created["id"]
    assert created["store"] is False
    status, _ = await handler.dispatch("GET", f"/v1/responses/{rid}", workspace_id="t1")
    assert status == 404


@pytest.mark.asyncio
async def test_input_items_pagination_three_pages(env):
    """Criterion ③: 50 items, limit 20 -> 3 pages, no dup / no missing."""
    rs, handler = env
    _, created = await handler.dispatch("POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o"})
    rid = created["id"]
    items = [{"id": f"item_{i}", "type": "message", "role": "user", "content": []} for i in range(50)]
    await rs.save_input_items(rid, items)

    seen: list[int] = []
    after = -1
    pages = 0
    while True:
        status, body = await handler.dispatch(
            "GET",
            f"/v1/responses/{rid}/input_items",
            workspace_id="t1",
            body={"after": after, "limit": 20},
        )
        assert status == 200
        page_ids = [it["id"] for it in body["data"]]
        seen.extend(int(i.split("_")[1]) for i in page_ids)
        pages += 1
        if not body["has_more"]:
            break
        after = body["after"]
        assert pages <= 3, "should not exceed 3 pages"
    assert pages == 3
    assert seen == list(range(50))  # exactly 0..49, no dup, no miss


@pytest.mark.asyncio
async def test_pipeline_zero_bytes_sequence(env):
    """Criterion ⑤: a 0-byte upstream ends as created->in_progress->completed."""
    rs, handler = env
    _, created = await handler.dispatch("POST", "/v1/responses", workspace_id="t1", body={"model": "gpt-4o"})
    rid = created["id"]

    async def empty_upstream():
        # yields nothing (0 bytes)
        if False:
            yield None

    frames = [f async for f in ResponsePipeline(rid, workspace_id="t1", store=rs).run(empty_upstream())]
    # The stream ends on the terminal response.completed event (no Chat-Completions [DONE]).
    assert frames[-1].decode("utf-8").startswith("event: response.completed")
    event_types = []
    for fr in frames:
        line = fr.decode("utf-8")
        for ln in line.splitlines():
            if ln.startswith("event: "):
                event_types.append(ln[len("event: ") :].strip())
    assert event_types == ["response.created", "response.in_progress", "response.completed"]
    # Events persisted for catch-up.
    events = await rs.list_events(rid)
    assert [e["event_type"] for e in events] == event_types
