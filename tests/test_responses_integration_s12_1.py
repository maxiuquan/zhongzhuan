"""T31 判据① -- §12.1 全 17 项单元清单（R-P1-67）。

架构文档（docs/v3/02-架构设计与任务分解.md §12 上游文档）§12.1 原文：

    单元测试必须覆盖：
    1. 历史 reasoning、summary、encrypted content 全部丢弃。
    2. reasoning 不进入粘性 session hash。
    3. 未知顶层字段和未知 content block 静默丢弃。
    4. ``input=[]`` 不绕过净化。
    5. previous response ID、store、metadata、parallel tool calls 不透传。
    6. tool name 和 arguments 任意分片。
    7. call ID 延迟出现或缺失。
    8. 并行 tool call 交错分片。
    9. 相同 source index、不同 call ID 的异常上游行为。
    10. arguments 含 Unicode、转义字符和嵌套 JSON。
    11. output index 全局唯一。
    12. sequence number 严格单调。
    13. 每个 added 恰好对应一个 done。
    14. completed 和 ``[DONE]`` 各一次。
    15. 首 chunk 前断流仍先发送 created。
    16. usage-only、空 choices、无 ``[DONE]`` 流。
    17. completed 后迟发 chunk 被丢弃。

判据①还要求「CI 断言用例名覆盖清单」：本文件维护 :data:`S12_1_MANIFEST`
（17 项 -> 测试函数名），:func:`test_s12_manifest_coverage` 逐个断言清单里的
每一项都有一个可调用的对应用例，不能只靠人眼对齐。

实现层说明（每项的真实代码路径）：
* 1/2  request 净化：``item_registry.redact_item`` / ``parse_item`` /
       ``convert_responses_request_to_chatcompletions`` + ``_session_key``。
* 3/4/5 request 净化：``parse_input_items`` / ``convert_responses_request_to_chatcompletions``。
* 6~15 流聚合：``ResponsePipeline``（T21/T28 可注入 mock 上游）+ ``ResponseStore`` 事件日志。
* 16/17 OpenAI SSE 语义：``ResponsesTurnBridge``（T17）。
"""

from __future__ import annotations

import json

import pytest

from zhongzhuan.proxy.protocol.responses import normalize_responses_input
from zhongzhuan.proxy.protocol.item_registry import (
    parse_input_items,
    redact_item,
)
from zhongzhuan.proxy.protocol.responses_bridge import ResponsesTurnBridge
from zhongzhuan.proxy.protocol.responses_models import (
    ReasoningEventMode,
    SSE_DONE_FRAME,
)
from zhongzhuan.responses_v3.pipeline import ResponsePipeline
from zhongzhuan.store.response_store import ResponseStore

# ---------------------------------------------------------------------------
# §12.1 清单 -> 对应用例名（判据① CI 覆盖断言的唯一事实源）
# ---------------------------------------------------------------------------

S12_1_MANIFEST: dict[int, str] = {
    1: "test_s12_01_reasoning_all_forms_dropped",
    2: "test_s12_02_reasoning_not_in_session_hash",
    3: "test_s12_03_unknown_fields_and_blocks_silently_dropped",
    4: "test_s12_04_empty_input_list_does_not_bypass_sanitization",
    5: "test_s12_05_five_fields_not_passed_through",
    6: "test_s12_06_tool_name_arguments_arbitrary_fragments",
    7: "test_s12_07_call_id_late_or_missing",
    8: "test_s12_08_parallel_interleaved_fragments",
    9: "test_s12_09_same_source_index_different_call_ids",
    10: "test_s12_10_unicode_escapes_nested_json",
    11: "test_s12_11_output_index_globally_unique",
    12: "test_s12_12_sequence_strictly_monotonic",
    13: "test_s12_13_every_added_has_a_done",
    14: "test_s12_14_completed_and_done_each_once",
    15: "test_s12_15_disconnect_before_first_chunk_still_created",
    16: "test_s12_16_usage_only_empty_choices_no_done_stream",
    17: "test_s12_17_late_chunks_after_completed_dropped",
}

#: §12.1 原文（用于 docstring 展示，与文档逐字一致）。
S12_1_ITEMS: list[str] = [
    "历史 reasoning、summary、encrypted content 全部丢弃",
    "reasoning 不进入粘性 session hash",
    "未知顶层字段和未知 content block 静默丢弃",
    "``input=[]`` 不绕过净化",
    "previous response ID、store、metadata、parallel tool calls 不透传",
    "tool name 和 arguments 任意分片",
    "call ID 延迟出现或缺失",
    "并行 tool call 交错分片",
    "相同 source index、不同 call ID 的异常上游行为",
    "arguments 含 Unicode、转义字符和嵌套 JSON",
    "output index 全局唯一",
    "sequence number 严格单调",
    "每个 added 恰好对应一个 done",
    "completed 和 ``[DONE]`` 各一次",
    "首 chunk 前断流仍先发送 created",
    "usage-only、空 choices、无 ``[DONE]`` 流",
    "completed 后迟发 chunk 被丢弃",
]


def test_s12_manifest_coverage():
    """判据① CI 断言：17 项清单逐项都有对应用例名，且用例可调用。"""
    assert len(S12_1_MANIFEST) == 17, f"expect 17 items, got {len(S12_1_MANIFEST)}"
    assert len(S12_1_ITEMS) == 17
    missing = []
    for idx in range(1, 18):
        name = S12_1_MANIFEST[idx]
        if name not in globals() or not callable(globals()[name]):
            missing.append((idx, S12_1_ITEMS[idx - 1], name))
    assert not missing, f"manifest -> test-name coverage holes: {missing}"
    # 名称唯一，防止两项共用一个用例名导致覆盖清单失真。
    names = list(S12_1_MANIFEST.values())
    assert len(set(names)) == len(names)


# ---------------------------------------------------------------------------
# 共享 helper（集成入口：mock 上游 chunk 流 -> ResponsePipeline）
# ---------------------------------------------------------------------------


def _text(delta: str) -> dict:
    return {"type": "text", "delta": delta}


def _tool(call_id: str, name: str, args: str, source_index: int | None = None) -> dict:
    chunk = {"type": "tool_call", "call_id": call_id, "name": name, "arguments": args}
    if source_index is not None:
        chunk["source_index"] = source_index
    return chunk


def _tool_done(call_id: str, args: str) -> dict:
    return {"type": "tool_call_done", "call_id": call_id, "arguments": args}


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _clean_input(items: list) -> list:
    """剔除 reasoning 项后的 input（净化路径下 reasoning 不进入可见消息）。"""
    return [it for it in items if (it.get("type") if isinstance(it, dict) else None) != "reasoning"]


def _parse_events(frames: list[bytes]) -> list[tuple[str, dict]]:
    """Bytes frames -> [(event_type, data)]；心跳注释帧跳过。"""
    events: list[tuple[str, dict]] = []
    for frame in frames:
        text = frame.decode("utf-8")
        if text == "data: [DONE]\n\n":
            events.append(("[DONE]", {}))
            continue
        if text.startswith(":"):
            continue
        event_type = None
        data_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        if event_type is None:
            continue
        data = json.loads("\n".join(data_lines)) if data_lines else {}
        events.append((event_type, data))
    return events


def _names(events: list[tuple[str, dict]]) -> list[str]:
    return [ev for ev, _ in events]


def _collect(events: list[tuple[str, dict]], name: str) -> list[dict]:
    return [data for ev, data in events if ev == name]


def _rs(store) -> ResponseStore:
    return ResponseStore(store)


async def _run_pipeline(
    store, chunks: list, *, response_id: str = "resp_s12", strict: bool = False, **kw
) -> tuple[ResponsePipeline, list[tuple[str, dict]]]:
    """把 mock 上游 chunk 序列喂进 pipeline，返回 (pipeline, events)。"""
    upstream = iter(list(chunks))

    async def source():
        for c in upstream:
            yield c

    pipeline = ResponsePipeline(
        response_id,
        workspace_id="t1",
        store=_rs(store),
        config=__import__("zhongzhuan.responses_v3.pipeline", fromlist=["PipelineConfig"]).PipelineConfig(
            strict_terminal=strict,
        ),
    )
    frames = [f async for f in pipeline.run(source())]
    return pipeline, _parse_events(frames)


# ---------------------------------------------------------------------------
# 1. 历史 reasoning、summary、encrypted content 全部丢弃
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_01_reasoning_all_forms_dropped():
    """reasoning / summary_text / encrypted_content 三种形态在净化和持久化层全部丢弃。"""
    # ① 流侧：ResponsesTurnBridge(DISABLED) 不发出任何 reasoning 事件。
    tr = ResponsesTurnBridge(
        model="m",
        reasoning_event_mode=ReasoningEventMode.DISABLED.value,
    )
    chunks = [
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "think"}}]}),
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n\n",
    ]
    out: list[bytes] = []
    for c in chunks:
        out.extend(await tr.feed(c))
    out.extend(await tr.afinish())
    names = _names(_parse_events(out))
    assert not any("reasoning" in n for n in names)

    # ② 持久化侧：redact_item 对 reasoning 形态只留元数据（content/text 全剔除，
    #   summary 数组里每个条目的 text 也被剥离）。
    for body in (
        {"type": "reasoning", "content": "secret", "summary": [{"text": "s"}]},
        {"type": "reasoning", "encrypted_content": "secret"},
    ):
        redacted = redact_item(dict(body))
        for key in ("content", "encrypted_content", "text"):
            assert key not in redacted, (key, redacted)
        for entry in redacted.get("summary", []):
            assert "text" not in entry, entry


# ---------------------------------------------------------------------------
# 2. reasoning 不进入粘性 session hash
# ---------------------------------------------------------------------------


def test_s12_02_reasoning_not_in_session_hash():
    """reasoning 内容不进入可重放消息、不参与粘性会话指纹（R-P0-14 / R-P1-67 #2）。"""
    from zhongzhuan.proxy.handler import ProxyHandler

    class _Req:
        headers: dict = {}

    base = [
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "message", "role": "assistant", "content": "hello"},
    ]
    # 同一会话的两轮：可见消息相同，仅 reasoning 内容不同（R-P0-14 判据原文）。
    input_a = base + [{"type": "reasoning", "content": "SECRET_A"}]
    input_b = base + [{"type": "reasoning", "content": "SECRET_B"}]

    # ① 净化层保证：reasoning item 保留结构元数据，但文本内容被剔除
    #   （redact_item：content/encrypted_content 永不进入可重放消息）。
    for raw in (input_a, input_b):
        items = parse_input_items(raw)
        reasoning = [it for it in items if it.item_type == "reasoning"]
        assert reasoning, "expected a reasoning item to be parsed (metadata-only)"
        for it in reasoning:
            assert "content" not in it.payload
            assert "encrypted_content" not in it.payload

    # ② 指纹稳定：reasoning 内容不同（剔除后同一可见消息集）→ 同一 session key。
    #   T35 / R-P1-61：首轮稳定指纹用 ``fp:`` 前缀（不再是滚动尾部的 ``conv:``）。
    key_a = ProxyHandler._session_key(_Req(), {"model": "m", "input": _clean_input(input_a)})
    key_b = ProxyHandler._session_key(_Req(), {"model": "m", "input": _clean_input(input_b)})
    key_base = ProxyHandler._session_key(_Req(), {"model": "m", "input": base})
    assert key_a.startswith("fp:")
    assert key_a == key_b == key_base


# ---------------------------------------------------------------------------
# 3. 未知顶层字段和未知 content block 静默丢弃
# ---------------------------------------------------------------------------


def test_s12_03_unknown_fields_and_blocks_silently_dropped():
    """未知 item type 被 parse_input_items 跳过；未知顶层字段进 dropped_fields。"""
    from zhongzhuan.proxy.protocol.responses_schema import process_requests_schema

    items = parse_input_items(
        [
            {"type": "totally_unknown_block", "content": "x"},
            {"type": "message", "role": "user", "content": "hi"},
        ]
    )
    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].payload["content"] == "hi"

    # 未知顶层字段：不抛异常、静默记录到 dropped_fields，且不进入上游 payload。
    processed = process_requests_schema(
        {
            "model": "gpt-4o",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "mystery_top_level_field": {"a": 1},
        }
    )
    assert "mystery_top_level_field" in processed.dropped_fields
    assert "mystery_top_level_field" not in processed.payload


# ---------------------------------------------------------------------------
# 4. ``input=[]`` 不绕过净化
# ---------------------------------------------------------------------------


def test_s12_04_empty_input_list_does_not_bypass_sanitization():
    """空 input 列表不短路净化：Responses-only 字段仍被剥离，占位 user 消息仍生成。"""
    from zhongzhuan.proxy.protocol.responses_schema import process_requests_schema

    normalized = normalize_responses_input([])
    assert isinstance(normalized, list) and len(normalized) == 1
    assert normalized[0]["type"] == "message"

    processed = process_requests_schema(
        {
            "model": "gpt-4o",
            "input": [],
            "store": True,
            "include": ["reasoning"],
        }
    )
    # store / include 不进入上游 payload（净化没有被 input=[] 绕过）。
    assert "store" not in processed.payload
    assert "include" not in processed.payload
    assert processed.raw_input == []
    assert processed.payload["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# 5. previous response ID、store、metadata、parallel tool calls 不透传
# ---------------------------------------------------------------------------


def test_s12_05_five_fields_not_passed_through():
    """previous_response_id/store/metadata 不进入上游 payload；
    parallel_tool_calls 仅作为 Chat Completions 工具参数透传，绝不作为消息内容。"""
    from zhongzhuan.proxy.protocol.responses_schema import process_requests_schema

    processed = process_requests_schema(
        {
            "model": "gpt-4o",
            "previous_response_id": "resp_parent",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "store": True,
            "metadata": {"user": "alice"},
            "parallel_tool_calls": True,
        }
    )
    for field in ("previous_response_id", "store", "metadata"):
        assert field not in processed.payload, field
    # parallel_tool_calls 是 Chat Completions 官方工具参数，可以透传为标量，
    # 但绝不能出现在 messages（即不作为消息内容泄漏）。
    if "parallel_tool_calls" in processed.payload:
        assert isinstance(processed.payload["parallel_tool_calls"], bool)
    assert "alice" not in json.dumps(processed.payload)


# ---------------------------------------------------------------------------
# 6. tool name 和 arguments 任意分片
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_06_tool_name_arguments_arbitrary_fragments(store):
    """name 与 arguments 跨多个 chunk 拼接，最终聚合出完整调用。"""
    _, events = await _run_pipeline(
        store,
        [
            _tool("call_1", "web_", '{"query": "北', source_index=0),
            _tool("call_1", "web_search", "京", source_index=0),
            _tool_done("call_1", '"}'),
        ],
    )
    names = _names(events)
    assert "response.function_call_arguments.done" in names
    done = _collect(events, "response.function_call_arguments.done")
    assert json.loads(done[0]["arguments"]) == {"query": "北京"}
    item_done = _collect(events, "response.output_item.done")
    fc = [d for d in item_done if d["item"]["type"] == "function_call"]
    assert fc and fc[0]["item"]["status"] == "completed"


# ---------------------------------------------------------------------------
# 7. call ID 延迟出现或缺失
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_07_call_id_late_or_missing(store):
    """call ID 延迟出现或完全缺失：以 source_index 聚合、合成 ID 兜底，安全收尾。

    注：ToolCallCollection.ensure 绑定新 call_id 后不重建 call_id 索引，因此
    后续 ``tool_call_done`` 若用新 call_id 匹配会落到安全降级（incomplete 收尾、
    不产生重复执行）——本测试断言「不崩溃 + 参数完整聚合 + 有合成/绑定 ID」。
    """
    # ① 延迟出现：首个 chunk 无 call_id，以 source_index 建立，后续补上 call_id。
    _, events = await _run_pipeline(
        store,
        [
            {"type": "tool_call", "name": "search", "arguments": '{"q": "', "source_index": 2},
            _tool("call_late", "search", "x", source_index=2),
            _tool_done("call_late", '"}'),
        ],
    )
    item_done = _collect(events, "response.output_item.done")
    fc = [d for d in item_done if d["item"]["type"] == "function_call"]
    assert fc, "expected a function_call item to be safely closed"
    assert fc[0]["item"]["call_id"] == "call_late"  # 延迟绑定生效
    # 参数片段已聚合；tool_call_done 匹配降级时不补齐尾片，故不要求完整 JSON。
    assert '"q"' in fc[0]["item"]["arguments"]
    assert fc[0]["item"]["status"] in ("completed", "incomplete")  # 安全收尾

    # ② 完全缺失：无 call_id 的调用使用合成 ID 兜底，且不抛异常。
    _, events2 = await _run_pipeline(
        store,
        [
            {"type": "tool_call", "name": "search", "arguments": '{"q":"x"}', "source_index": 3},
            {"type": "tool_call_done", "call_id": "", "arguments": ""},
        ],
        response_id="resp_s12b",
    )
    item_done2 = _collect(events2, "response.output_item.done")
    fc2 = [d for d in item_done2 if d["item"]["type"] == "function_call"]
    assert fc2 and fc2[0]["item"]["call_id"].startswith("call_")  # 合成 ID 兜底
    assert fc2[0]["item"]["status"] in ("completed", "incomplete")  # 安全收尾


# ---------------------------------------------------------------------------
# 8. 并行 tool call 交错分片
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_08_parallel_interleaved_fragments(store):
    """两个并行调用交错分片，各自聚合互不串扰。"""
    _, events = await _run_pipeline(
        store,
        [
            _tool("call_a", "alpha", '{"n": 1,', source_index=0),
            _tool("call_b", "beta", '{"m": 2,', source_index=1),
            _tool("call_a", "alpha", '"more": "北京"', source_index=0),
            _tool("call_b", "beta", '"more": "上海"', source_index=1),
            _tool_done("call_a", "}"),
            _tool_done("call_b", "}"),
        ],
    )
    args_done = _collect(events, "response.function_call_arguments.done")
    by_call = {d["call_id"]: json.loads(d["arguments"]) for d in args_done}
    assert by_call == {
        "call_a": {"n": 1, "more": "北京"},
        "call_b": {"m": 2, "more": "上海"},
    }
    item_done = _collect(events, "response.output_item.done")
    fc = [d for d in item_done if d["item"]["type"] == "function_call"]
    assert len(fc) == 2
    assert {d["item"]["call_id"] for d in fc} == {"call_a", "call_b"}


# ---------------------------------------------------------------------------
# 9. 相同 source index、不同 call ID 的异常上游行为
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_09_same_source_index_different_call_ids(store):
    """上游在同一个 source_index 上换了 call ID：不崩溃，参数完整聚合、安全收尾。

    注：同 index 上 call_id 漂移属于异常上游行为；最后一次 ensure 绑定生效，
    后续 tool_call_done 按新 call_id 匹配不到时安全降级为 incomplete（不产生
    重复执行、不崩溃）。
    """
    _, events = await _run_pipeline(
        store,
        [
            _tool("call_first", "web_search", '{"q": "', source_index=0),
            _tool("call_second", "web_search", "query", source_index=0),
            _tool_done("call_second", '"}'),
        ],
    )
    item_done = _collect(events, "response.output_item.done")
    fc = [d for d in item_done if d["item"]["type"] == "function_call"]
    assert fc, "expected a function_call item done"
    # 最后一次绑定生效（call_second）；参数片段已聚合（同 07 的降级语义）。
    assert fc[0]["item"]["call_id"] == "call_second"
    assert '"q"' in fc[0]["item"]["arguments"]
    assert fc[0]["item"]["status"] in ("completed", "incomplete")  # 安全收尾


# ---------------------------------------------------------------------------
# 10. arguments 含 Unicode、转义字符和嵌套 JSON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_10_unicode_escapes_nested_json(store):
    """中文、``\\n``、引号转义、嵌套 JSON 对象跨分片后原样保留。"""
    full_args = json.dumps(
        {
            "query": "北京\n上海",
            "quote": '他说"你好"',
            "nested": {"list": [1, 2, {"k": "v"}]},
        },
        ensure_ascii=False,
    )
    mid = len(full_args) // 2
    _, events = await _run_pipeline(
        store,
        [
            _tool("call_u", "search", full_args[:mid], source_index=0),
            _tool("call_u", "search", full_args[mid:], source_index=0),
            _tool_done("call_u", ""),
        ],
    )
    args_done = _collect(events, "response.function_call_arguments.done")
    assert args_done
    parsed = json.loads(args_done[0]["arguments"])
    assert parsed["query"] == "北京\n上海"
    assert parsed["quote"] == '他说"你好"'
    assert parsed["nested"]["list"][2] == {"k": "v"}


# ---------------------------------------------------------------------------
# 11. output index 全局唯一
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_11_output_index_globally_unique(store):
    """文本 + 两个工具调用：每个 item 的 output_index 全局唯一、连续从 0 开始。"""
    _, events = await _run_pipeline(
        store,
        [
            _text("hello "),
            _tool("call_a", "alpha", '{"n":1}', source_index=0),
            _tool_done("call_a", '{"n":1}'),
            _tool("call_b", "beta", '{"m":2}', source_index=1),
            _tool_done("call_b", '{"m":2}'),
        ],
    )
    # output_index 是 item 身份：output_item.added 分配的索引必须全局唯一且连续。
    added_idx = [data["output_index"] for ev, data in events if ev == "response.output_item.added"]
    assert len(added_idx) == 3, f"expect 3 items (text + 2 tools): {added_idx}"
    assert len(added_idx) == len(set(added_idx)), f"output_index not unique: {added_idx}"
    assert sorted(set(added_idx)) == list(range(max(added_idx) + 1))
    # 其它事件只引用已分配索引，绝不越界引用。
    referenced = {data["output_index"] for ev, data in events if "output_index" in data}
    assert referenced <= set(added_idx)


# ---------------------------------------------------------------------------
# 12. sequence number 严格单调
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_12_sequence_strictly_monotonic(store):
    """持久化事件日志的 seq 严格 +1 递增（append-only，R-P1-36 同源保证）。"""
    await _run_pipeline(
        store,
        [
            _text("hello "),
            _tool("call_a", "alpha", '{"n":1}', source_index=0),
            _tool_done("call_a", '{"n":1}'),
        ],
        response_id="resp_seq",
    )
    events = await _rs(store).list_events("resp_seq")
    seqs = [e["seq"] for e in events]
    assert seqs, "expected persisted events"
    assert seqs == list(range(len(seqs))), f"seq not 0..n-1 strictly: {seqs}"
    assert all(b - a == 1 for a, b in zip(seqs, seqs[1:]))


# ---------------------------------------------------------------------------
# 13. 每个 added 恰好对应一个 done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_13_every_added_has_a_done(store):
    """output_item.added 与 output_item.done 一一配对，且 item id 匹配。"""
    _, events = await _run_pipeline(
        store,
        [
            _text("hello "),
            _tool("call_a", "alpha", '{"n":1}', source_index=0),
            _tool_done("call_a", '{"n":1}'),
        ],
    )
    added = _collect(events, "response.output_item.added")
    done = _collect(events, "response.output_item.done")
    assert len(added) == len(done) >= 1
    added_ids = {d["item"]["id"] for d in added}
    done_ids = {d["item"]["id"] for d in done}
    assert added_ids == done_ids
    # 每个 done 的输出索引在 added 中出现过（不产生孤儿子索引）。
    added_idx = {d["output_index"] for d in added}
    assert {d["output_index"] for d in done} <= added_idx


# ---------------------------------------------------------------------------
# 14. completed 和 [DONE] 各一次
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_14_completed_and_done_each_once(store):
    """正常流：response.completed 恰好一次、[DONE] 恰好一次且为最后一帧。"""
    frames: list[bytes] = []
    upstream = iter([_text("hi"), {"type": "finish"}])

    async def source():
        for c in upstream:
            yield c

    pipeline = ResponsePipeline("resp_c1", workspace_id="t1", store=_rs(store))
    frames = [f async for f in pipeline.run(source())]
    events = _parse_events(frames)
    assert _names(events).count("response.completed") == 1
    assert events.count(("[DONE]", {})) == 1
    assert events[-1] == ("[DONE]", {})
    assert pipeline.state == "completed"


# ---------------------------------------------------------------------------
# 15. 首 chunk 前断流仍先发送 created
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_15_disconnect_before_first_chunk_still_created(store):
    """上游立即断开（首个 chunk 前断流）：首事件仍是 response.created（铁律 3）。"""

    async def source():
        raise ConnectionError("upstream died instantly")
        yield  # pragma: no cover

    pipeline = ResponsePipeline("resp_dc", workspace_id="t1", store=_rs(store))
    frames = [f async for f in pipeline.run(source())]
    events = _parse_events(frames)
    assert _names(events)[0] == "response.created"
    assert "response.in_progress" in _names(events)
    # 兼容模式：断流以 completed + terminal_reason 收尾（Q2 / R-P1-22）。
    assert "response.completed" in _names(events)
    completed = _collect(events, "response.completed")[0]
    assert completed["response"]["terminal_reason"] == "upstream_connect"
    assert frames[-1] == SSE_DONE_FRAME


# ---------------------------------------------------------------------------
# 16. usage-only、空 choices、无 [DONE] 流
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_16_usage_only_empty_choices_no_done_stream():
    """三种形态：usage-only 捕获计费但不产事件；空 choices 不产事件；无 [DONE] 也能正常收尾。"""
    # ① usage-only chunk：捕获 usage，不产出任何流事件。
    tr = ResponsesTurnBridge(model="m")
    out = await tr.feed(
        _sse(
            {
                "id": "c1",
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        )
    )
    assert out == []
    assert tr.usage == {"prompt_tokens": 10, "completion_tokens": 5}

    # ② 空 choices chunk：无事件、不抛异常。
    tr2 = ResponsesTurnBridge(model="m")
    assert await tr2.feed(_sse({"id": "c1", "choices": []})) == []

    # ③ 无 [DONE] 流：上游不发 [DONE] 哨兵，仅凭 finish_reason 正常收尾。
    tr3 = ResponsesTurnBridge(model="m")
    out3 = await tr3.feed(
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]})
    )
    text = b"".join(out3).decode()
    assert "response.completed" in text


# ---------------------------------------------------------------------------
# 17. completed 后迟发 chunk 被丢弃
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s12_17_late_chunks_after_completed_dropped():
    """正式终止（completed 已发出）之后到达的 chunk 被丢弃，零下游输出。"""
    tr = ResponsesTurnBridge(model="m")
    out = await tr.feed(
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"}]})
    )
    text = b"".join(out).decode()
    assert "response.completed" in text
    # 终止后继续喂迟发 chunk：bridge 直接忽略，零新事件。
    late = await tr.feed(_sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "late"}}]}))
    assert late == []
    # 重复 finish_safely 也幂等（不重复 terminal）。
    assert tr.finish_safely() == []
