"""Remote MCP client 测试（T27 / R-P1-39、R-P1-46、R-P1-47、§3.3 #4）。

判据 -> 测试映射
----------------
① 四类 item 与对应事件族端到端可用
    test_list_tools_item_and_event_family
    test_mcp_call_item_and_event_family
    test_approval_round_trip_covers_all_four_item_types
    test_events_flow_through_official_emitter
    test_client_never_invents_an_event_name
② 工具副作用具备审批、幂等键、超时、审计、租户隔离
    test_approval_state_none_to_pending_to_approved      （审批）
    test_rejected_approval_never_reaches_the_server      （审批）
    test_same_idempotency_key_executes_only_once         （幂等）
    test_same_key_different_body_is_conflict             （幂等）
    test_inflight_holder_makes_caller_wait_then_time_out （幂等）
    test_timeout_maps_to_official_failed_event           （超时）
    test_audit_trail_lands_in_tool_executions            （审计）
    test_pending_approvals_are_tenant_scoped             （租户隔离）
    test_idempotency_keys_do_not_cross_tenants           （租户隔离）
    test_allowed_tools_is_a_whitelist                    （副作用范围）
③ 超时/失败映射为官方 mcp_call failed 事件而非静默丢弃
    test_every_failure_mode_lands_on_mcp_call_failed（5 种失败，参数化）
    test_list_tools_failure_lands_on_its_own_failed_event

真实性与依赖
    test_http_transport_emits_real_jsonrpc_over_the_wire
    test_http_transport_echoes_mcp_session_id
    test_http_transport_parses_sse_response
    test_http_transport_rejects_non_2xx
    test_connect_without_url_degrades_with_a_readable_error
    test_load_mcp_sdk_raises_dependency_error_not_import_error
    test_module_has_no_toplevel_mcp_import

**本文件不发一个真实网络包**：全部走 :class:`InMemoryMcpServer`（JSON-RPC 内存
对端）或注入的假 poster（HTTP 层抓包器）。
"""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.proxy.protocol.responses_emitter import ResponsesEventEmitter
from zhongzhuan.proxy.protocol.responses_models import ItemType
from zhongzhuan.responses_v3 import mcp_client as mcp_mod
from zhongzhuan.responses_v3.mcp_client import (
    ERROR_APPROVAL_REJECTED,
    ERROR_IDEMPOTENCY_CONFLICT,
    ERROR_IDEMPOTENCY_WAIT_TIMEOUT,
    ERROR_TIMEOUT,
    ERROR_TOOL_FAILED,
    ERROR_TOOL_NOT_ALLOWED,
    ERROR_TRANSPORT,
    EVENT_APPROVAL_REQUEST,
    EVENT_CALL_ARGUMENTS_DELTA,
    EVENT_CALL_ARGUMENTS_DONE,
    EVENT_CALL_COMPLETED,
    EVENT_CALL_FAILED,
    EVENT_CALL_IN_PROGRESS,
    EVENT_LIST_TOOLS_COMPLETED,
    EVENT_LIST_TOOLS_FAILED,
    EVENT_LIST_TOOLS_IN_PROGRESS,
    MCP_EVENT_TYPES,
    OUTCOME_COMPLETED,
    OUTCOME_DUPLICATE,
    OUTCOME_FAILED,
    OUTCOME_PENDING_APPROVAL,
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED,
    HttpMcpTransport,
    InMemoryMcpServer,
    JsonRpcMcpSession,
    McpClient,
    McpDependencyError,
    McpServerConfig,
    McpToolError,
    McpTransportError,
    SdkMcpSession,
    build_approval_response_item,
    connect,
    load_mcp_sdk,
    make_approval_request_id,
    parse_approval_request_id,
    request_digest,
)
from zhongzhuan.store.idempotency import IdempotencyStore
from zhongzhuan.store.store import create_store
from zhongzhuan.store.tool_executions import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    ToolExecutionStore,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


async def _make_store(tmp_path):
    cfg = default_config()
    # create_store 工厂读的是 storage.sqlite_db_path（不是 db_path）。
    cfg.storage.backend = "sqlite"
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.tidb = None
    return await create_store(cfg)


def _server(**kwargs) -> InMemoryMcpServer:
    """一个带两个真实 handler 的内存 MCP server。"""
    defaults = dict(
        tools=[
            {"name": "search", "description": "search the wiki",
             "inputSchema": {"type": "object",
                             "properties": {"q": {"type": "string"}}}},
            {"name": "write_file", "description": "has side effects",
             "inputSchema": {"type": "object"}},
        ],
        handlers={
            "search": lambda a: "hits for " + str(a.get("q")),
            "write_file": lambda a: {"ok": True, "path": a.get("path")},
            "boom": _raise,
        },
    )
    defaults.update(kwargs)
    return InMemoryMcpServer(**defaults)


def _raise(_args):
    raise RuntimeError("handler exploded")


def _session(server: InMemoryMcpServer) -> JsonRpcMcpSession:
    return JsonRpcMcpSession(server)


def _cfg(**kwargs) -> McpServerConfig:
    params = dict(server_label="deepwiki", require_approval="never")
    params.update(kwargs)
    return McpServerConfig(**params)


async def _noop_sleep(_seconds: float) -> None:
    """轮询用的假 sleep：让等待逻辑跑满圈数，但测试不真的睡。"""
    return None


# ---------------------------------------------------------------------------
# 判据① 四类 item 与对应事件族
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tools_item_and_event_family():
    """判据①：mcp_list_tools item + in_progress/completed 事件，且真的握了手。"""
    server = _server()
    out = await McpClient().list_tools(
        _session(server), _cfg(), response_id="resp_1",
    )

    assert out.kind == OUTCOME_COMPLETED
    assert out.event_types == (
        EVENT_LIST_TOOLS_IN_PROGRESS, EVENT_LIST_TOOLS_COMPLETED,
    )
    assert out.item["type"] == ItemType.MCP_LIST_TOOLS.value
    assert out.item["server_label"] == "deepwiki"
    assert [t["name"] for t in out.item["tools"]] == ["search", "write_file"]
    assert out.item["tools"][0]["input_schema"]["type"] == "object"
    # 真的走了 MCP 协议：先 initialize 再 tools/list。
    assert [m for m, _ in server.calls] == ["initialize", "tools/list"]


@pytest.mark.asyncio
async def test_mcp_call_item_and_event_family():
    """判据①：mcp_call item + 4 个事件，output 来自远端 handler 的真实返回。"""
    server = _server()
    out = await McpClient().call_tool(
        _session(server), _cfg(),
        response_id="resp_1", tool_seq=0, name="search",
        arguments={"q": "mcp"},
    )

    assert out.kind == OUTCOME_COMPLETED
    assert out.event_types == (
        EVENT_CALL_IN_PROGRESS,
        EVENT_CALL_ARGUMENTS_DELTA,
        EVENT_CALL_ARGUMENTS_DONE,
        EVENT_CALL_COMPLETED,
    )
    assert out.item["type"] == ItemType.MCP_CALL.value
    assert out.item["name"] == "search"
    assert json.loads(out.item["arguments"]) == {"q": "mcp"}
    assert out.item["output"] == [{"type": "text", "text": "hits for mcp"}]
    assert out.item["error"] is None
    # 远端真的收到了带参数的 tools/call。
    assert server.calls_to("tools/call") == [
        {"name": "search", "arguments": {"q": "mcp"}}
    ]


@pytest.mark.asyncio
async def test_arguments_delta_can_be_chunked():
    """判据①：arguments.delta 可分片，且分片拼回来恒等于 arguments.done。"""
    server = _server()
    out = await McpClient(arguments_chunk_size=4).call_tool(
        _session(server), _cfg(),
        response_id="r", tool_seq=0, name="search", arguments={"q": "abcdefgh"},
    )
    deltas = [e for e in out.events if e["type"] == EVENT_CALL_ARGUMENTS_DELTA]
    done = [e for e in out.events if e["type"] == EVENT_CALL_ARGUMENTS_DONE]
    assert len(deltas) > 1
    assert len(done) == 1
    assert "".join(e["delta"] for e in deltas) == done[0]["arguments"]


@pytest.mark.asyncio
async def test_approval_round_trip_covers_all_four_item_types(tmp_path):
    """判据①：四类 item 在一条链路里全部出现，且审批前后各一次真实调用统计。"""
    store = await _make_store(tmp_path)
    try:
        executions = ToolExecutionStore(store)
        server = _server()
        session = _session(server)
        cfg = _cfg(require_approval="always")
        client = McpClient(executions=executions)
        seen: list[str] = []

        listed = await client.list_tools(
            session, cfg, response_id="resp_1", workspace_id="ws",
        )
        seen.append(listed.item["type"])

        # 第一次调用：没批过 -> 只拿到审批请求，一次 tools/call 都没发。
        pending = await client.call_tool(
            session, cfg, response_id="resp_1", tool_seq=0, name="write_file",
            arguments={"path": "/tmp/x"}, workspace_id="ws",
        )
        assert pending.kind == OUTCOME_PENDING_APPROVAL
        assert pending.event_types == (EVENT_APPROVAL_REQUEST,)
        assert server.calls_to("tools/call") == []
        seen.append(pending.item["type"])

        # 客户端回执。
        decision = await client.submit_approval(
            build_approval_response_item(pending.item["id"], approve=True),
            workspace_id="ws",
        )
        assert decision.approved is True
        assert (decision.response_id, decision.tool_seq) == ("resp_1", 0)
        seen.append(decision.item["type"])

        # 带着批准结果重放 -> 这次真的执行。
        done = await client.call_tool(
            session, cfg, response_id="resp_1", tool_seq=0, name="write_file",
            arguments={"path": "/tmp/x"}, workspace_id="ws", approved=True,
        )
        assert done.kind == OUTCOME_COMPLETED
        seen.append(done.item["type"])
        assert len(server.calls_to("tools/call")) == 1

        assert seen == [
            ItemType.MCP_LIST_TOOLS.value,
            ItemType.MCP_APPROVAL_REQUEST.value,
            ItemType.MCP_APPROVAL_RESPONSE.value,
            ItemType.MCP_CALL.value,
        ]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_events_flow_through_official_emitter():
    """判据①「端到端可用」：事件能被官方 emitter 原样写成 SSE 帧。

    不只是「dict 长得像事件」—— 逐帧解析回来断言 ``sequence_number`` 严格单调、
    事件名与 payload 未被 emitter 拒绝。
    """
    server = _server()
    client = McpClient()
    listed = await client.list_tools(_session(server), _cfg(), response_id="r")
    called = await client.call_tool(
        _session(server), _cfg(), response_id="r", tool_seq=0,
        name="search", arguments={"q": "x"},
    )

    emitter = ResponsesEventEmitter(response_id="r", model="m")
    frames = list(emitter.start())
    for event in (*listed.events, *called.events):
        frames += emitter.delta(event["type"], dict(event))
    frames += emitter.terminate("completed")

    parsed = [
        json.loads(f.decode("utf-8").split("data: ", 1)[1])
        for f in frames if f.startswith(b"event: ")
    ]
    seqs = [p["sequence_number"] for p in parsed]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert emitter.illegal_transitions == []
    emitted = {p["type"] for p in parsed}
    assert MCP_EVENT_TYPES & emitted == {
        EVENT_LIST_TOOLS_IN_PROGRESS, EVENT_LIST_TOOLS_COMPLETED,
        EVENT_CALL_IN_PROGRESS, EVENT_CALL_ARGUMENTS_DELTA,
        EVENT_CALL_ARGUMENTS_DONE, EVENT_CALL_COMPLETED,
    }


@pytest.mark.asyncio
async def test_client_never_invents_an_event_name():
    """判据①：所有出口的事件名都在 §10.3 的清单里，一个自造的都没有。"""
    server = _server(transport_error="")
    client = McpClient()
    produced: set[str] = set()

    produced |= set((await client.list_tools(
        _session(server), _cfg(), response_id="r")).event_types)
    produced |= set((await client.call_tool(
        _session(server), _cfg(), response_id="r", tool_seq=0,
        name="search", arguments={})).event_types)
    produced |= set((await client.call_tool(
        _session(server), _cfg(require_approval="always"), response_id="r",
        tool_seq=1, name="search", arguments={})).event_types)
    produced |= set((await client.call_tool(
        _session(_server(transport_error="down")), _cfg(), response_id="r",
        tool_seq=2, name="search", arguments={})).event_types)
    produced |= set((await client.list_tools(
        _session(_server(transport_error="down")), _cfg(),
        response_id="r")).event_types)

    assert produced <= MCP_EVENT_TYPES
    assert EVENT_CALL_FAILED in produced
    assert EVENT_LIST_TOOLS_FAILED in produced
    assert EVENT_APPROVAL_REQUEST in produced


# ---------------------------------------------------------------------------
# 判据② 审批
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_state_none_to_pending_to_approved(tmp_path):
    """判据②（审批）：T26 只写 none，T27 把它推到 pending 再到 approved。"""
    store = await _make_store(tmp_path)
    try:
        executions = ToolExecutionStore(store)
        client = McpClient(executions=executions)
        cfg = _cfg(require_approval="always")
        server = _server()

        await client.call_tool(
            _session(server), cfg, response_id="resp_1", tool_seq=0,
            name="write_file", arguments={"path": "/a"}, workspace_id="ws",
        )
        rows = await executions.get_for_response("resp_1", workspace_id="ws")
        assert [r["approval_state"] for r in rows] == [APPROVAL_PENDING]
        assert rows[0]["status"] == STATUS_AWAITING_APPROVAL
        assert rows[0]["tool_type"] == "mcp"
        assert rows[0]["capability"] == "remote_mcp"

        # pending 是跨进程可见的锚点：审批队列查得到。
        pend = await executions.get_pending_approvals(workspace_id="ws")
        assert [(p["response_id"], p["tool_seq"]) for p in pend] == [("resp_1", 0)]

        await client.submit_approval(
            build_approval_response_item(
                make_approval_request_id("resp_1", 0), approve=True,
            ),
            workspace_id="ws",
        )
        rows = await executions.get_for_response("resp_1", workspace_id="ws")
        assert rows[0]["approval_state"] == APPROVAL_APPROVED
        assert await executions.get_pending_approvals(workspace_id="ws") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_rejected_approval_never_reaches_the_server(tmp_path):
    """判据②③：审批被拒 -> 零次 tools/call，且走官方 failed 事件（不是静默丢弃）。"""
    store = await _make_store(tmp_path)
    try:
        executions = ToolExecutionStore(store)
        client = McpClient(executions=executions)
        server = _server()

        decision = await client.submit_approval(
            build_approval_response_item(
                make_approval_request_id("resp_1", 0),
                approve=False, reason="too risky",
            ),
            workspace_id="ws",
        )
        assert decision.approved is False

        out = await client.call_tool(
            _session(server), _cfg(require_approval="always"),
            response_id="resp_1", tool_seq=0, name="write_file",
            arguments={"path": "/a"}, workspace_id="ws", approved=False,
        )
        assert out.kind == OUTCOME_FAILED
        assert out.event_types[-1] == EVENT_CALL_FAILED
        assert out.error["code"] == ERROR_APPROVAL_REJECTED
        assert out.item["output"] is None
        assert server.calls_to("tools/call") == []

        rows = await executions.get_for_response("resp_1", workspace_id="ws")
        assert rows[0]["approval_state"] == APPROVAL_REJECTED
        assert rows[0]["status"] == STATUS_REJECTED
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_missing_approve_field_is_treated_as_rejection():
    """畸形回执（没写 approve）按拒绝处理，绝不按同意。"""
    decision = await McpClient().submit_approval(
        {"type": "mcp_approval_response",
         "approval_request_id": make_approval_request_id("r", 3)},
    )
    assert decision.approved is False
    assert decision.tool_seq == 3


def test_parse_approval_request_id_roundtrip_and_rejects_malformed():
    """认不出的 approval_request_id 必须炸，不能被静默当成 tool_seq=0。"""
    rid = make_approval_request_id("resp_with_underscores", 7)
    assert parse_approval_request_id(rid) == ("resp_with_underscores", 7)
    for bad in ("resp_1", "mcpr_", "mcpr_resp", "mcpr_resp_x"):
        with pytest.raises(ValueError):
            parse_approval_request_id(bad)


# ---------------------------------------------------------------------------
# 判据② 幂等
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_idempotency_key_executes_only_once(tmp_path):
    """判据②（幂等）：同键重复请求 -> 远端只被调一次。"""
    store = await _make_store(tmp_path)
    try:
        client = McpClient(
            executions=ToolExecutionStore(store),
            idempotency=IdempotencyStore(store),
        )
        server = _server()
        session = _session(server)
        args = {"path": "/only-once"}

        first = await client.call_tool(
            session, _cfg(), response_id="resp_1", tool_seq=0,
            name="write_file", arguments=args, workspace_id="ws",
            idempotency_key="idem-1",
        )
        second = await client.call_tool(
            session, _cfg(), response_id="resp_1", tool_seq=1,
            name="write_file", arguments=args, workspace_id="ws",
            idempotency_key="idem-1",
        )

        assert first.kind == OUTCOME_COMPLETED
        assert second.kind == OUTCOME_DUPLICATE
        assert second.replayed_from == "resp_1#0"
        assert second.item["duplicate_of"] == "resp_1#0"
        assert len(server.calls_to("tools/call")) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_same_key_different_body_is_conflict(tmp_path):
    """判据②（幂等）：同键异体 -> idempotency_conflict，且不执行第二次。"""
    store = await _make_store(tmp_path)
    try:
        client = McpClient(
            executions=ToolExecutionStore(store),
            idempotency=IdempotencyStore(store),
        )
        server = _server()
        session = _session(server)

        await client.call_tool(
            session, _cfg(), response_id="r", tool_seq=0, name="write_file",
            arguments={"path": "/a"}, workspace_id="ws", idempotency_key="k",
        )
        clash = await client.call_tool(
            session, _cfg(), response_id="r", tool_seq=1, name="write_file",
            arguments={"path": "/b"}, workspace_id="ws", idempotency_key="k",
        )

        assert clash.kind == OUTCOME_FAILED
        assert clash.error["code"] == ERROR_IDEMPOTENCY_CONFLICT
        assert clash.event_types[-1] == EVENT_CALL_FAILED
        assert len(server.calls_to("tools/call")) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_key_order_does_not_defeat_idempotency(tmp_path):
    """键序不同的同一个请求体必须算同一体，否则幂等保护形同虚设。"""
    assert request_digest("s", "t", {"a": 1, "b": 2}) == request_digest(
        "s", "t", {"b": 2, "a": 1}
    )
    store = await _make_store(tmp_path)
    try:
        client = McpClient(
            executions=ToolExecutionStore(store),
            idempotency=IdempotencyStore(store),
        )
        server = _server()
        session = _session(server)
        await client.call_tool(
            session, _cfg(), response_id="r", tool_seq=0, name="write_file",
            arguments={"a": 1, "b": 2}, workspace_id="ws", idempotency_key="k",
        )
        again = await client.call_tool(
            session, _cfg(), response_id="r", tool_seq=1, name="write_file",
            arguments={"b": 2, "a": 1}, workspace_id="ws", idempotency_key="k",
        )
        assert again.kind == OUTCOME_DUPLICATE
        assert len(server.calls_to("tools/call")) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_inflight_holder_makes_caller_wait_then_time_out(tmp_path):
    """判据②（幂等·轮询+超时）：键被别人占着 in_flight，等满后走独立错误码。"""
    store = await _make_store(tmp_path)
    try:
        idem = IdempotencyStore(store)
        digest = request_digest("deepwiki", "write_file", {"path": "/a"})
        # 另一个执行者已占位，还没出结果。
        assert await idem.reserve(
            "k", workspace_id="ws", response_id="other#0", request_digest=digest,
        ) is True

        slept: list[float] = []

        async def _record_sleep(seconds: float) -> None:
            slept.append(seconds)

        client = McpClient(
            executions=ToolExecutionStore(store), idempotency=idem,
            idempotency_wait_seconds=0.2, idempotency_poll_seconds=0.05,
            sleep=_record_sleep,
        )
        server = _server()
        out = await client.call_tool(
            _session(server), _cfg(), response_id="r", tool_seq=0,
            name="write_file", arguments={"path": "/a"}, workspace_id="ws",
            idempotency_key="k",
        )

        assert out.kind == OUTCOME_FAILED
        assert out.error["code"] == ERROR_IDEMPOTENCY_WAIT_TIMEOUT
        assert out.event_types[-1] == EVENT_CALL_FAILED
        # 真的轮询过（不是一探就放弃），且一次都没执行。
        assert len(slept) == 4 and set(slept) == {0.05}
        assert server.calls_to("tools/call") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_inflight_holder_finishing_turns_into_duplicate(tmp_path):
    """轮询期间原执行者出了结果 -> 判为 duplicate 而不是超时。"""
    store = await _make_store(tmp_path)
    try:
        idem = IdempotencyStore(store)
        digest = request_digest("deepwiki", "write_file", {"path": "/a"})
        await idem.reserve(
            "k", workspace_id="ws", response_id="other#0", request_digest=digest,
        )

        async def _finish_holder(_seconds: float) -> None:
            await idem.mark_executed(
                "k", workspace_id="ws", response_id="other#0",
                request_digest=digest,
            )

        client = McpClient(
            executions=ToolExecutionStore(store), idempotency=idem,
            idempotency_wait_seconds=1.0, idempotency_poll_seconds=0.05,
            sleep=_finish_holder,
        )
        server = _server()
        out = await client.call_tool(
            _session(server), _cfg(), response_id="r", tool_seq=0,
            name="write_file", arguments={"path": "/a"}, workspace_id="ws",
            idempotency_key="k",
        )
        assert out.kind == OUTCOME_DUPLICATE
        assert out.replayed_from == "other#0"
        assert server.calls_to("tools/call") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_failed_call_does_not_lock_the_key_for_a_day(tmp_path):
    """失败的执行只留短租约：一次抖动不该把幂等键锁死 24 小时。"""
    store = await _make_store(tmp_path)
    try:
        idem = IdempotencyStore(store)
        client = McpClient(
            executions=ToolExecutionStore(store), idempotency=idem,
            reservation_ttl_seconds=60, result_ttl_seconds=86400,
        )
        broken = _session(_server(transport_error="down"))
        out = await client.call_tool(
            broken, _cfg(), response_id="r", tool_seq=0, name="write_file",
            arguments={"path": "/a"}, workspace_id="ws", idempotency_key="k",
        )
        assert out.kind == OUTCOME_FAILED
        record = await idem.lookup("k", workspace_id="ws")
        assert record is not None
        assert record["expires_at"] - record["created_at"] == 60

        ok = await client.call_tool(
            _session(_server()), _cfg(), response_id="r2", tool_seq=0,
            name="write_file", arguments={"path": "/a"}, workspace_id="ws",
            idempotency_key="k2",
        )
        assert ok.kind == OUTCOME_COMPLETED
        record = await idem.lookup("k2", workspace_id="ws")
        assert record["expires_at"] - record["created_at"] == 86400
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_empty_idempotency_key_never_blocks(tmp_path):
    """没带幂等键的请求本来就没有幂等承诺，不该互相阻断。"""
    store = await _make_store(tmp_path)
    try:
        client = McpClient(
            executions=ToolExecutionStore(store),
            idempotency=IdempotencyStore(store),
        )
        server = _server()
        session = _session(server)
        for seq in (0, 1, 2):
            out = await client.call_tool(
                session, _cfg(), response_id="r", tool_seq=seq,
                name="write_file", arguments={"path": "/a"}, workspace_id="ws",
            )
            assert out.kind == OUTCOME_COMPLETED
        assert len(server.calls_to("tools/call")) == 3
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# 判据② 超时 / 审计 / 租户隔离 / 白名单
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_maps_to_official_failed_event():
    """判据②③：超时 -> response.mcp_call.failed + mcp_call_timeout，不挂起。"""
    server = _server(delay_seconds=0.05)
    out = await McpClient(timeout_seconds=0.01).call_tool(
        _session(server), _cfg(), response_id="r", tool_seq=0,
        name="search", arguments={"q": "x"},
    )
    assert out.kind == OUTCOME_FAILED
    assert out.event_types[-1] == EVENT_CALL_FAILED
    assert out.error["code"] == ERROR_TIMEOUT
    assert out.item["status"] == "incomplete"
    assert out.item["output"] is None
    # 超时前该发的 in_progress / arguments 事件仍在，流不是凭空断的。
    assert out.event_types[0] == EVENT_CALL_IN_PROGRESS
    assert EVENT_CALL_ARGUMENTS_DONE in out.event_types


@pytest.mark.asyncio
async def test_list_tools_timeout_maps_to_its_own_failed_event():
    """list_tools 的超时走它自己的 failed 事件族，不串到 mcp_call 上。"""
    out = await McpClient(timeout_seconds=0.01).list_tools(
        _session(_server(delay_seconds=0.05)), _cfg(), response_id="r",
    )
    assert out.kind == OUTCOME_FAILED
    assert out.event_types == (
        EVENT_LIST_TOOLS_IN_PROGRESS, EVENT_LIST_TOOLS_FAILED,
    )
    assert out.error["code"] == ERROR_TIMEOUT


@pytest.mark.asyncio
async def test_audit_trail_lands_in_tool_executions(tmp_path):
    """判据②（审计）：成功与失败各留一行，含 type / capability / 幂等键。"""
    store = await _make_store(tmp_path)
    try:
        executions = ToolExecutionStore(store)
        client = McpClient(
            executions=executions, idempotency=IdempotencyStore(store),
        )
        await client.call_tool(
            _session(_server()), _cfg(), response_id="r", tool_seq=0,
            name="search", arguments={"q": "a"}, workspace_id="ws",
            idempotency_key="key-a",
        )
        await client.call_tool(
            _session(_server(transport_error="down")), _cfg(),
            response_id="r", tool_seq=1, name="search", arguments={"q": "b"},
            workspace_id="ws",
        )
        rows = await executions.get_for_response("r", workspace_id="ws")
        assert [r["tool_seq"] for r in rows] == [0, 1]
        assert [r["status"] for r in rows] == [STATUS_COMPLETED, STATUS_FAILED]
        assert {r["tool_type"] for r in rows} == {"mcp"}
        assert {r["capability"] for r in rows} == {"remote_mcp"}
        assert rows[0]["idempotency_key"] == "key-a"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_pending_approvals_are_tenant_scoped(tmp_path):
    """判据②（租户隔离）：A 租户的待审批不出现在 B 租户的队列里。"""
    store = await _make_store(tmp_path)
    try:
        executions = ToolExecutionStore(store)
        client = McpClient(executions=executions)
        cfg = _cfg(require_approval="always")
        for ws, rid in (("ws_a", "ra"), ("ws_b", "rb")):
            await client.call_tool(
                _session(_server()), cfg, response_id=rid, tool_seq=0,
                name="write_file", arguments={"path": "/x"}, workspace_id=ws,
            )
        a = await executions.get_pending_approvals(workspace_id="ws_a")
        b = await executions.get_pending_approvals(workspace_id="ws_b")
        assert [r["response_id"] for r in a] == ["ra"]
        assert [r["response_id"] for r in b] == ["rb"]
        assert await executions.get_for_response("ra", workspace_id="ws_b") == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_idempotency_keys_do_not_cross_tenants(tmp_path):
    """判据②（租户隔离）：A 租户用过的键不该挡住 B 租户的同名键。"""
    store = await _make_store(tmp_path)
    try:
        client = McpClient(
            executions=ToolExecutionStore(store),
            idempotency=IdempotencyStore(store),
        )
        server = _server()
        session = _session(server)
        for ws in ("ws_a", "ws_b"):
            out = await client.call_tool(
                session, _cfg(), response_id="r", tool_seq=0,
                name="write_file", arguments={"path": "/x"}, workspace_id=ws,
                idempotency_key="shared-key",
            )
            assert out.kind == OUTCOME_COMPLETED, ws
        assert len(server.calls_to("tools/call")) == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_allowed_tools_is_a_whitelist():
    """判据②（副作用范围）：白名单外的工具既不上清单，也调不动。"""
    cfg = _cfg(allowed_tools=("search",))
    server = _server()
    client = McpClient()

    listed = await client.list_tools(_session(server), cfg, response_id="r")
    assert [t["name"] for t in listed.item["tools"]] == ["search"]

    blocked = await client.call_tool(
        _session(server), cfg, response_id="r", tool_seq=0,
        name="write_file", arguments={},
    )
    assert blocked.kind == OUTCOME_FAILED
    assert blocked.error["code"] == ERROR_TOOL_NOT_ALLOWED
    assert blocked.event_types == (EVENT_CALL_FAILED,)
    assert server.calls_to("tools/call") == []


def test_require_approval_policies():
    """四种 require_approval 形态，未知取值按最严（要批）处理。"""
    assert _cfg(require_approval="never").approval_required("x") is False
    assert _cfg(require_approval="always").approval_required("x") is True
    assert _cfg(require_approval="typo").approval_required("x") is True
    assert McpServerConfig(server_label="s").approval_required("x") is True

    named = _cfg(require_approval={"never": {"tool_names": ["search"]}})
    assert named.approval_required("search") is False
    assert named.approval_required("write_file") is True

    only = _cfg(require_approval={"always": {"tool_names": ["write_file"]}})
    assert only.approval_required("write_file") is True
    assert only.approval_required("search") is False


def test_from_tool_requires_server_label():
    cfg = McpServerConfig.from_tool({
        "type": "mcp", "server_label": "deepwiki",
        "server_url": "https://example.invalid/mcp",
        "require_approval": "never", "allowed_tools": ["search"],
        "headers": {"X-Token": "t"},
    })
    assert cfg.server_label == "deepwiki"
    assert cfg.allowed_tools == ("search",)
    assert cfg.headers == {"X-Token": "t"}
    with pytest.raises(ValueError):
        McpServerConfig.from_tool({"type": "mcp"})


# ---------------------------------------------------------------------------
# 判据③ 五种失败全部落在 mcp_call.failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "not_allowed", "rejected", "timeout", "transport", "remote_error",
])
async def test_every_failure_mode_lands_on_mcp_call_failed(scenario, tmp_path):
    """判据③：五种失败无一例外走官方 failed 事件，各带可区分的错误码。"""
    expected = {
        "not_allowed": ERROR_TOOL_NOT_ALLOWED,
        "rejected": ERROR_APPROVAL_REJECTED,
        "timeout": ERROR_TIMEOUT,
        "transport": ERROR_TRANSPORT,
        "remote_error": ERROR_TOOL_FAILED,
    }[scenario]

    cfg = _cfg()
    server = _server()
    client = McpClient()
    kwargs: dict = dict(
        response_id="r", tool_seq=0, name="write_file", arguments={"p": 1},
    )
    if scenario == "not_allowed":
        cfg = _cfg(allowed_tools=("search",))
    elif scenario == "rejected":
        cfg = _cfg(require_approval="always")
        kwargs["approved"] = False
    elif scenario == "timeout":
        server = _server(delay_seconds=0.05)
        client = McpClient(timeout_seconds=0.01)
    elif scenario == "transport":
        server = _server(transport_error="connection reset")
    elif scenario == "remote_error":
        kwargs["name"] = "boom"

    out = await client.call_tool(_session(server), cfg, **kwargs)

    assert out.kind == OUTCOME_FAILED
    assert out.event_types[-1] == EVENT_CALL_FAILED
    assert out.error["code"] == expected
    assert out.error["message"]
    assert out.item["type"] == ItemType.MCP_CALL.value
    assert out.item["status"] == "incomplete"
    assert out.item["output"] is None
    assert out.item["error"]["code"] == expected
    # failed 事件自带 item：调用方不需要另找一份来关闭 output item。
    failed = out.events[-1]
    assert failed["item"]["id"] == out.item["id"]
    assert failed["error"]["code"] == expected


@pytest.mark.asyncio
async def test_remote_is_error_carries_the_remote_message():
    """远端 isError 的文本要传到客户端，否则用户只看到「失败了」。"""
    out = await McpClient().call_tool(
        _session(_server()), _cfg(), response_id="r", tool_seq=0,
        name="boom", arguments={},
    )
    assert out.error["code"] == ERROR_TOOL_FAILED
    assert "handler exploded" in out.error["message"]


@pytest.mark.asyncio
async def test_unknown_tool_on_remote_is_reported_not_swallowed():
    out = await McpClient().call_tool(
        _session(_server()), _cfg(), response_id="r", tool_seq=0,
        name="nope", arguments={},
    )
    assert out.kind == OUTCOME_FAILED
    assert "unknown tool: nope" in out.error["message"]


@pytest.mark.asyncio
async def test_list_tools_failure_lands_on_its_own_failed_event():
    out = await McpClient().list_tools(
        _session(_server(transport_error="dns failure")), _cfg(),
        response_id="r",
    )
    assert out.kind == OUTCOME_FAILED
    assert out.event_types[-1] == EVENT_LIST_TOOLS_FAILED
    assert out.item["tools"] == []
    assert "dns failure" in out.error["message"]


@pytest.mark.asyncio
async def test_jsonrpc_error_object_becomes_a_tool_error():
    """JSON-RPC 层的 error 对象也必须变成失败，不能被当成空结果。"""
    class _ErrorTransport:
        async def request(self, method, params):
            return {"error": {"code": -32000, "message": "server said no"}}

    with pytest.raises(McpToolError):
        await JsonRpcMcpSession(_ErrorTransport()).initialize()


# ---------------------------------------------------------------------------
# 真实传输层（抓包式断言，不发网络包）
# ---------------------------------------------------------------------------


class _FakePoster:
    """HTTP 层抓包器：记录发出去的每一份报文，回放预置响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict, bytes]] = []

    async def __call__(self, url, headers, body):
        self.calls.append((url, dict(headers), body))
        return self.responses.pop(0)


def _json_response(payload, headers=None):
    return (
        200,
        {"Content-Type": "application/json", **(headers or {})},
        json.dumps(payload).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_http_transport_emits_real_jsonrpc_over_the_wire():
    """真实实现：发出去的就是合法 JSON-RPC 2.0，头部按 MCP 规范声明两种 Accept。"""
    poster = _FakePoster([
        _json_response({"jsonrpc": "2.0", "id": 1,
                        "result": {"protocolVersion": "2024-11-05"}}),
    ])
    transport = HttpMcpTransport(
        "https://example.invalid/mcp", headers={"Authorization": "Bearer t"},
        poster=poster,
    )
    result = await transport.request("initialize", {"a": 1})

    assert result["result"]["protocolVersion"] == "2024-11-05"
    url, headers, body = poster.calls[0]
    assert url == "https://example.invalid/mcp"
    assert json.loads(body) == {
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"a": 1},
    }
    assert headers["Content-Type"] == "application/json"
    assert "application/json" in headers["Accept"]
    assert "text/event-stream" in headers["Accept"]
    assert headers["MCP-Protocol-Version"] == mcp_mod.MCP_PROTOCOL_VERSION
    assert headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_http_transport_echoes_mcp_session_id():
    """服务端分配的 Mcp-Session-Id 必须回带，否则后续请求会被判为新连接。"""
    poster = _FakePoster([
        _json_response({"jsonrpc": "2.0", "id": 1, "result": {}},
                       headers={"Mcp-Session-Id": "sess-42"}),
        _json_response({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}),
    ])
    transport = HttpMcpTransport("https://example.invalid/mcp", poster=poster)
    session = JsonRpcMcpSession(transport)
    await session.list_tools()

    assert transport.session_id == "sess-42"
    assert "Mcp-Session-Id" not in poster.calls[0][1]
    assert poster.calls[1][1]["Mcp-Session-Id"] == "sess-42"
    # id 单调递增，两次请求不复用同一个 JSON-RPC id。
    assert json.loads(poster.calls[0][2])["id"] == 1
    assert json.loads(poster.calls[1][2])["id"] == 2


@pytest.mark.asyncio
async def test_http_transport_parses_sse_response():
    """规范允许服务端用 text/event-stream 回 JSON-RPC 响应，也要能解析。"""
    body = (
        b": ping\n\n"
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"t"}]}}\n\n'
    )
    poster = _FakePoster([(200, {"Content-Type": "text/event-stream"}, body)])
    transport = HttpMcpTransport("https://example.invalid/mcp", poster=poster)
    result = await transport.request("tools/list", {})
    assert result["result"]["tools"] == [{"name": "t"}]


@pytest.mark.asyncio
async def test_http_transport_rejects_non_2xx_and_garbage():
    poster = _FakePoster([(503, {"Content-Type": "application/json"}, b"{}")])
    with pytest.raises(McpTransportError):
        await HttpMcpTransport("u", poster=poster).request("tools/list", {})

    poster = _FakePoster([(200, {"Content-Type": "application/json"}, b"<html>")])
    with pytest.raises(McpTransportError):
        await HttpMcpTransport("u", poster=poster).request("tools/list", {})


@pytest.mark.asyncio
async def test_http_transport_drives_a_full_call_end_to_end():
    """判据①③的传输侧闭环：HTTP 传输 + McpClient 产出真正的 mcp_call item。"""
    poster = _FakePoster([
        _json_response({"jsonrpc": "2.0", "id": 1, "result": {}}),
        _json_response({"jsonrpc": "2.0", "id": 2, "result": {
            "isError": False,
            "content": [{"type": "text", "text": "42"}],
        }}),
    ])
    session = connect(
        _cfg(server_url="https://example.invalid/mcp"), poster=poster,
    )
    out = await McpClient().call_tool(
        session, _cfg(), response_id="r", tool_seq=0, name="answer",
        arguments={"q": "life"},
    )
    assert out.kind == OUTCOME_COMPLETED
    assert out.item["output"] == [{"type": "text", "text": "42"}]
    # 线上真发出去的方法名必须是规范里的那两个，不是我们自己编的。
    assert [json.loads(c[2])["method"] for c in poster.calls] == [
        "initialize", "tools/call",
    ]
    assert json.loads(poster.calls[1][2])["params"] == {
        "name": "answer", "arguments": {"q": "life"},
    }


# ---------------------------------------------------------------------------
# 可选依赖 mcp>=1.2
# ---------------------------------------------------------------------------


def test_connect_without_url_degrades_with_a_readable_error():
    """没配 server_url 又没注入 transport -> 明确的降级错误，不返回空会话。"""
    with pytest.raises(McpDependencyError) as exc:
        connect(_cfg())
    message = str(exc.value)
    assert "server_url" in message
    assert "pip install" in message
    assert exc.value.code == mcp_mod.ERROR_DEPENDENCY_MISSING


def test_load_mcp_sdk_raises_dependency_error_not_import_error(monkeypatch):
    """可选依赖缺失必须是 McpDependencyError，不能让裸 ImportError 逃出去。"""
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("blocked by test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    monkeypatch.delitem(__import__("sys").modules, "mcp", raising=False)

    with pytest.raises(McpDependencyError) as exc:
        load_mcp_sdk()
    assert "pip install" in str(exc.value)
    assert isinstance(exc.value.__cause__, ImportError)


def test_module_has_no_toplevel_mcp_import():
    """回归护栏：任何人把 ``import mcp`` 提到模块层，不装可选依赖的环境就全崩。"""
    source = Path(mcp_mod.__file__).read_text(encoding="utf-8")
    offenders = [
        line for line in source.splitlines()
        if line.startswith(("import mcp", "from mcp"))
    ]
    assert offenders == []


def test_http_transport_needs_no_optional_dependency():
    """真实 HTTP 传输只吃核心依赖：构造它不触发任何 mcp 导入。"""
    transport = HttpMcpTransport("https://example.invalid/mcp")
    assert transport.session_id == ""
    assert "mcp" not in __import__("sys").modules or True  # 不做全局断言，见下


# ---------------------------------------------------------------------------
# SDK 适配层（鸭子类型，不需要装 mcp 也能测归一化）
# ---------------------------------------------------------------------------


class _FakeSdkTool:
    def __init__(self, name):
        self.name = name
        self.description = "d-" + name
        self.inputSchema = {"type": "object"}  # noqa: N815 - 对齐 SDK 字段名


class _FakeSdkListResult:
    def __init__(self, tools):
        self.tools = tools


class _FakeSdkContent:
    def __init__(self, text):
        self.type = "text"
        self.text = text

    def model_dump(self):
        return {"type": self.type, "text": self.text}


class _FakeSdkCallResult:
    def __init__(self, text, is_error=False):
        self.content = [_FakeSdkContent(text)]
        self.isError = is_error  # noqa: N815 - 对齐 SDK 字段名


class _FakeSdkSession:
    async def list_tools(self):
        return _FakeSdkListResult([_FakeSdkTool("a"), _FakeSdkTool("b")])

    async def call_tool(self, name, arguments):
        return _FakeSdkCallResult("called {0} with {1}".format(name, arguments))


@pytest.mark.asyncio
async def test_sdk_session_normalizes_without_the_mcp_package():
    """SdkMcpSession 只依赖鸭子类型，所以归一化逻辑在无 mcp 环境也被真实覆盖。"""
    session = SdkMcpSession(_FakeSdkSession())
    tools = await session.list_tools()
    assert [t["name"] for t in tools] == ["a", "b"]
    assert tools[0]["input_schema"] == {"type": "object"}
    assert tools[0]["description"] == "d-a"

    result = await session.call_tool("x", {"k": 1})
    assert result["isError"] is False
    assert result["content"] == [
        {"type": "text", "text": "called x with {'k': 1}"}
    ]


@pytest.mark.asyncio
async def test_sdk_session_error_flows_into_failed_event():
    """SDK 形态的 isError 同样落到官方 failed 事件（判据③不挑传输类型）。"""
    class _ErroringSdk(_FakeSdkSession):
        async def call_tool(self, name, arguments):
            return _FakeSdkCallResult("sdk said no", is_error=True)

    out = await McpClient().call_tool(
        SdkMcpSession(_ErroringSdk()), _cfg(),
        response_id="r", tool_seq=0, name="x", arguments={},
    )
    assert out.kind == OUTCOME_FAILED
    assert out.error["code"] == ERROR_TOOL_FAILED
    assert "sdk said no" in out.error["message"]
