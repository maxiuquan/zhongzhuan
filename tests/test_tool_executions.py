"""Tests for ToolExecutionStore (T26 / R-P1-46、R-P1-47).

判据映射见各测试 docstring：并行 3 工具、往返持久化、幂等保留 created_at、
approval 往返、非法 approval_state 报错、tool_seq=-1 过滤、待审批租户隔离。
"""
from __future__ import annotations

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.proxy.protocol.responses_models import Capability
from zhongzhuan.responses_v3.hosted_tools import HostedToolRecognizer
from zhongzhuan.store.store import create_store
from zhongzhuan.store.tool_executions import (
    APPROVAL_APPROVED,
    APPROVAL_NONE,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ToolExecutionStore,
    execution_id_for,
)


async def _make_store(tmp_path):
    cfg = default_config()
    # create_store 工厂读的是 storage.sqlite_db_path（不是 db_path）。
    cfg.storage.backend = "sqlite"
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.tidb = None  # 强制 SQLite
    return await create_store(cfg)


def _specs(types):
    payload = {"tools": [{"type": t} for t in types]}
    return HostedToolRecognizer().recognize(payload)


@pytest.mark.asyncio
async def test_parallel_three_hosted_tools_persisted(tmp_path):
    """判据①（并行 3 工具）：一次请求里 3 个 hosted tool 各留一行，按 seq 升序。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        specs = _specs(["web_search", "file_search", "code_interpreter"])
        await HostedToolRecognizer().persist("resp_1", "ws", specs, store)
        rows = await store.get_for_response("resp_1", workspace_id="ws")
        assert len(rows) == 3
        assert [r["tool_seq"] for r in rows] == [0, 1, 2]
        assert [r["tool_type"] for r in rows] == [
            "web_search", "file_search", "code_interpreter",
        ]
        # 能力映射被持久化。
        assert rows[0]["capability"] == Capability.WEB_SEARCH.value
        assert rows[2]["capability"] == Capability.CODE_INTERPRETER.value
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_round_trip_persist_fields(tmp_path):
    """判据①：写后读出字段完整，默认 approval_state 归一为 none，status 为 recognized。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        specs = _specs(["image_generation"])
        await HostedToolRecognizer().persist("resp_2", "ws", specs, store)
        rows = await store.get_for_response("resp_2", workspace_id="ws")
        assert len(rows) == 1
        r = rows[0]
        assert r["response_id"] == "resp_2"
        assert r["workspace_id"] == "ws"
        assert r["tool_seq"] == 0
        assert r["tool_type"] == "image_generation"
        assert r["capability"] == Capability.IMAGE_GENERATION.value
        assert r["status"] == "recognized"
        # v004 的空字符串 approval 对外归一为显式 none。
        assert r["approval_state"] == APPROVAL_NONE
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_record_is_idempotent_keeps_first_created_at(tmp_path):
    """判据①/R-P1-47：重复 (response_id, tool_seq) 覆盖整行，但保留首次 created_at。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        # 先放一条已知 created_at 的历史行，模拟「之前已经记录过」。
        await s.execute(
            "INSERT INTO tool_executions "
            "(execution_id, response_id, workspace_id, call_id, tool_name, "
            " idempotency_key, status, approval, result_digest, "
            " created_at, updated_at, expires_at, tool_seq, tool_type, capability) "
            "VALUES (?, ?, ?, '', '', ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
            (execution_id_for("resp_3", 0), "resp_3", "ws", "", "recognized",
             APPROVAL_NONE, 1000, 1000, 0, 0, "image_generation",
             Capability.IMAGE_GENERATION.value),
        )
        # 同 (response_id, tool_seq) 二次记录 —— 不应抹掉首次时间。
        await store.record(
            response_id="resp_3",
            workspace_id="ws",
            tool_seq=0,
            tool_type="image_generation",
            capability=Capability.IMAGE_GENERATION.value,
        )
        rows = await store.get_for_response("resp_3", workspace_id="ws")
        assert len(rows) == 1
        assert rows[0]["created_at"] == 1000  # 首次时间被保留
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_set_approval_round_trip(tmp_path):
    """判据（审批往返）：set_approval 写下的状态能被读回。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        specs = _specs(["mcp", "web_search"])
        await HostedToolRecognizer().persist("resp_4", "ws", specs, store)

        await store.set_approval("resp_4", 0, APPROVAL_APPROVED)
        await store.set_approval("resp_4", 1, APPROVAL_REJECTED)
        rows = await store.get_for_response("resp_4", workspace_id="ws")
        by_seq = {r["tool_seq"]: r["approval_state"] for r in rows}
        assert by_seq[0] == APPROVAL_APPROVED
        assert by_seq[1] == APPROVAL_REJECTED

        # 推进状态（set_status 不受审批白名单约束）。
        await store.set_status("resp_4", 0, "dispatched")
        rows = await store.get_for_response("resp_4", workspace_id="ws")
        assert rows[0]["status"] == "dispatched"
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_invalid_approval_state_raises_value_error(tmp_path):
    """判据（非法 approval_state）：写入阶段即报错，而非静默落库。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        with pytest.raises(ValueError):
            await store.record(
                response_id="resp_5", workspace_id="ws", tool_seq=0,
                tool_type="mcp", capability=Capability.REMOTE_MCP.value,
                approval_state="not_a_real_state",
            )
        with pytest.raises(ValueError):
            await store.set_approval("resp_5", 0, "also_bogus")
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_get_for_response_filters_legacy_function_call_rows(tmp_path):
    """判据（隔离旧行）：v004 风格的 function call 行 tool_seq=-1 不应混入 hosted 视图。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        # 一条 v004 风格 function call 行。
        await s.execute(
            "INSERT INTO tool_executions "
            "(execution_id, response_id, workspace_id, call_id, tool_name, "
            " idempotency_key, status, approval, result_digest, "
            " created_at, updated_at, expires_at, tool_seq, tool_type, capability) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (execution_id_for("resp_6", -1), "resp_6", "ws", "call_1", "get_weather",
             "", "pending", "", "", 1, 1, 0, -1, "", ""),
        )
        # 一条本次的 hosted tool 行。
        specs = _specs(["web_search"])
        await HostedToolRecognizer().persist("resp_6", "ws", specs, store)

        rows = await store.get_for_response("resp_6", workspace_id="ws")
        assert len(rows) == 1
        assert rows[0]["tool_seq"] == 0
        assert rows[0]["tool_type"] == "web_search"
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_get_pending_approvals_isolated_by_workspace(tmp_path):
    """判据（租户隔离）：待审批清单只返回本 workspace 的 pending 行。"""
    s = await _make_store(tmp_path)
    try:
        store = ToolExecutionStore(s)
        # workspace A 两个 hosted tool，都置 pending。
        specs_a = _specs(["web_search", "file_search"])
        await HostedToolRecognizer().persist("rA", "A", specs_a, store)
        await store.set_approval("rA", 0, APPROVAL_PENDING)
        await store.set_approval("rA", 1, APPROVAL_PENDING)
        # workspace A 还有一个 none 状态的工具，不应出现在待审批清单。
        specs_a2 = _specs(["code_interpreter"])
        await HostedToolRecognizer().persist("rA2", "A", specs_a2, store)

        # workspace B 一个 pending。
        specs_b = _specs(["web_search"])
        await HostedToolRecognizer().persist("rB", "B", specs_b, store)
        await store.set_approval("rB", 0, APPROVAL_PENDING)

        a_pending = await store.get_pending_approvals(workspace_id="A")
        assert len(a_pending) == 2
        assert {r["response_id"] for r in a_pending} == {"rA"}
        assert all(r["approval_state"] == APPROVAL_PENDING for r in a_pending)

        b_pending = await store.get_pending_approvals(workspace_id="B")
        assert len(b_pending) == 1
        assert b_pending[0]["response_id"] == "rB"
    finally:
        await s.close()
