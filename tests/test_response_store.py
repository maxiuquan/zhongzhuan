"""T16 tests: ResponseStore persistence (v004 tables, §4.2.2)."""

from __future__ import annotations

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store


@pytest.fixture
async def store(tmp_path):
    cfg = default_config()
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.storage.db_path = cfg.storage.sqlite_db_path  # keep alias in sync
    cfg.tidb = None
    s = await create_store(cfg)
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_create_and_get_response(store):
    rs = ResponseStore(store)
    await rs.create_response(
        response_id="resp_1",
        workspace_id="t1",
        model="gpt-4o",
        status="in_progress",
        previous_response_id="",
        request={"model": "gpt-4o", "input": "hi"},
    )
    rec = await rs.get_response("resp_1", workspace_id="t1")
    assert rec is not None
    assert rec.response_id == "resp_1"
    assert rec.status == "in_progress"
    assert rec.request["input"] == "hi"


@pytest.mark.asyncio
async def test_get_response_tenant_isolation(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    # Different tenant cannot see it.
    assert await rs.get_response("resp_1", workspace_id="t2") is None


@pytest.mark.asyncio
async def test_update_status_and_usage(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    await rs.update_status(
        "resp_1",
        "completed",
        terminal_reason="normal_finish",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        output=[{"id": "msg_1", "type": "output_text"}],
    )
    rec = await rs.get_response("resp_1", workspace_id="t1")
    assert rec.status == "completed"
    assert rec.terminal_reason == "normal_finish"
    assert rec.usage["completion_tokens"] == 5
    assert rec.output[0]["type"] == "output_text"
    assert rec.completed_at > 0


@pytest.mark.asyncio
async def test_delete_response(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    assert await rs.delete_response("resp_1", workspace_id="t1") is True
    assert await rs.get_response("resp_1", workspace_id="t1") is None


@pytest.mark.asyncio
async def test_set_cancelled(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    await rs.set_cancelled("resp_1", workspace_id="t1")
    rec = await rs.get_response("resp_1", workspace_id="t1")
    assert rec.cancelled is True
    assert rec.status == "cancelled"


@pytest.mark.asyncio
async def test_save_and_list_input_items(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    await rs.save_input_items(
        "resp_1",
        [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "function_call_output", "call_id": "c1", "output": "42"},
        ],
    )
    items = await rs.list_input_items("resp_1")
    assert len(items) == 2
    assert items[0]["type"] == "message"
    assert items[1]["type"] == "function_call_output"


@pytest.mark.asyncio
async def test_save_and_list_output_items(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    await rs.save_output_items(
        "resp_1",
        [
            {"id": "msg_1", "type": "message", "role": "assistant"},
            {"id": "fc_1", "type": "function_call"},
        ],
    )
    items = await rs.list_output_items("resp_1")
    assert len(items) == 2
    assert items[0]["type"] == "message"


@pytest.mark.asyncio
async def test_event_log_append_only(store):
    rs = ResponseStore(store)
    await rs.create_response(response_id="resp_1", workspace_id="t1")
    await rs.append_event("resp_1", "response.created", {"seq": 1})
    await rs.append_event("resp_1", "response.output_text.delta", {"delta": "x"})
    events = await rs.list_events("resp_1")
    assert [e["event_type"] for e in events] == ["response.created", "response.output_text.delta"]
    # seq strictly increasing
    seqs = [e["seq"] for e in events]
    assert seqs == [1, 2]
    # catch-up after_seq
    after = await rs.list_events("resp_1", after_seq=1)
    assert len(after) == 1
    assert after[0]["event_type"] == "response.output_text.delta"


@pytest.mark.asyncio
async def test_state_chain_loop_detection(store):
    rs = ResponseStore(store)
    await rs.save_state_chain("resp_1", "resp_2", depth=1, workspace_id="t1")
    await rs.save_state_chain("resp_2", "resp_1", depth=2, workspace_id="t1")
    assert await rs.get_previous_response_id("resp_1") == "resp_2"
    assert await rs.get_previous_response_id("resp_2") == "resp_1"
    assert await rs.chain_depth("resp_2") == 2


@pytest.mark.asyncio
async def test_background_task_lifecycle(store):
    rs = ResponseStore(store)
    await rs.create_task(task_id="task_1", response_id="resp_1", workspace_id="t1")
    # Lease the queued task.
    assert await rs.lease_task("task_1", lease_seconds=60) is True
    await rs.update_task_status("task_1", "in_progress")
    task = await rs.get_task("task_1", workspace_id="t1")
    assert task["status"] == "in_progress"
    assert task["lease_until"] > 0
    # Cancel request.
    await rs.request_cancel("task_1")
    task = await rs.get_task("task_1", workspace_id="t1")
    assert task["cancel_requested"] == 1


@pytest.mark.asyncio
async def test_tool_execution_idempotency(store):
    rs = ResponseStore(store)
    assert await rs.has_execution("idem_1") is False
    await rs.record_tool_execution(
        execution_id="exec_1",
        response_id="resp_1",
        workspace_id="t1",
        call_id="c1",
        tool_name="get_weather",
        idempotency_key="idem_1",
    )
    assert await rs.has_execution("idem_1") is True
