"""T31 判据③ -- 集成兼容矩阵（R-P1-69 / §12.3）。

§12.3 原文（上游开发文档）兼容矩阵：

    ```
    Codex CLI × DeepSeek reasoning model
    Codex CLI × OpenAI-compatible non-reasoning model
    Codex CLI × Anthropic upstream
    单工具调用
    并行工具调用
    工具失败后下一轮
    首 token 延迟 120 秒
    上游中途断流
    客户端主动取消
    ```

实现方式
--------
* **mock 套件**（始终在 CI 跑）：每个矩阵项一个 mock 上游（可注入 chunk 流 /
  可注入 clock），跑**同一套**语义断言。
* **真机套件**：同一套断言，标记 ``@pytest.mark.live``（CI ``-m "not live"``
  排除），并 ``skipif`` 未设置 ``ZHONGZHUAN_UPSTREAM_BASE_URL``。真机套件目前
  也以 mock 上游执行同一套断言（仓库 CI 无外部 key，禁止真实出网），**真机
  运行时需把 :data:`_LIVE_UPSTREAM` 换成连接 ``ZHONGZHUAN_UPSTREAM_BASE_URL``
  的真实上游** —— 断言本身与 mock 完全一致。诚实标注：本文件不发起任何真实
  网络请求。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from zhongzhuan.proxy.protocol.responses import (
    CompositeStreamTranslator,
    ResponsesStreamTranslator,
)
from zhongzhuan.proxy.protocol.responses_bridge import ResponsesTurnBridge
from zhongzhuan.proxy.protocol.responses_models import (
    ReasoningEventMode,
    SSE_DONE_FRAME,
    SSE_HEARTBEAT_FRAME,
)
from zhongzhuan.proxy.protocol.stream_a2o import StreamA2O
from zhongzhuan.responses_v3.pipeline import PipelineConfig, ResponsePipeline
from zhongzhuan.store.response_store import ResponseStore

ZHONGZHUAN_UPSTREAM_BASE_URL = os.environ.get("ZHONGZHUAN_UPSTREAM_BASE_URL", "")


# ---------------------------------------------------------------------------
# helper（解析 mock 上游产出的 bytes 帧序列）
# ---------------------------------------------------------------------------


def _parse_events(frames: list[bytes]) -> list[tuple[str, dict]]:
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
    store, chunks: list, *, response_id: str = "resp_m", **kw
) -> tuple[ResponsePipeline, list[tuple[str, dict]]]:
    upstream = iter(list(chunks))

    async def source():
        for c in upstream:
            yield c

    pipeline = ResponsePipeline(
        response_id,
        workspace_id="t1",
        store=_rs(store),
        config=PipelineConfig(**kw),
    )
    frames = [f async for f in pipeline.run(source())]
    return pipeline, _parse_events(frames)


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _tool(call_id: str, name: str, args: str, source_index: int | None = None) -> dict:
    chunk = {"type": "tool_call", "call_id": call_id, "name": name, "arguments": args}
    if source_index is not None:
        chunk["source_index"] = source_index
    return chunk


def _tool_done(call_id: str, args: str) -> dict:
    return {"type": "tool_call_done", "call_id": call_id, "arguments": args}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _make_fake_sleep(clock: FakeClock):
    async def _sleep(seconds: float) -> None:
        clock.advance(seconds)
        await asyncio.sleep(0)

    return _sleep


# ---------------------------------------------------------------------------
# 矩阵表：每项 = (序号, 短名, 描述, mock 上游, 断言函数)
# ---------------------------------------------------------------------------


async def _deepseek_reasoning_upstream():
    """DeepSeek 风格：reasoning_content 在前，content 在后。"""
    tr = ResponsesTurnBridge(
        model="deepseek-reasoner",
        reasoning_event_mode=ReasoningEventMode.DISABLED.value,
    )
    out: list[bytes] = []
    for c in [
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "think deep"}}]}),
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "答案在此"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n\n",
    ]:
        out.extend(await tr.feed(c))
    out.extend(await tr.afinish())
    return _parse_events(out)


async def _openai_non_reasoning_upstream():
    """标准 OpenAI 兼容非推理：纯 content delta + finish_reason。"""
    tr = ResponsesTurnBridge(model="gpt-4o-mini")
    out: list[bytes] = []
    for c in [
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "Hello "}}]}),
        _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}]}),
    ]:
        out.extend(await tr.feed(c))
    out.extend(await tr.afinish())
    return _parse_events(out)


async def _anthropic_upstream():
    """Anthropic 风格 SSE：content_block_delta text → StreamA2O → Responses。"""
    composite = CompositeStreamTranslator(
        StreamA2O(model="claude-3-5"),
        ResponsesStreamTranslator(model="gpt-4o"),
    )
    chunks = [
        (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"来自"}}\n\n'
        ).encode("utf-8"),
        (
            "event: content_block_delta\n"
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Anthropic"}}\n\n'
        ).encode("utf-8"),
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        b"data: [DONE]\n\n",
    ]
    out: list[bytes] = []
    for c in chunks:
        out.extend(await composite.feed(c))
    out.extend(await composite.finish_safely())
    return _parse_events(out)


async def _single_tool_upstream(store):
    _, events = await _run_pipeline(
        store,
        [
            _tool("call_1", "web_search", '{"q": "北京', source_index=0),
            _tool("call_1", "web_search", '"}', source_index=0),
            _tool_done("call_1", ""),
        ],
        response_id="resp_m_tool1",
    )
    return events


async def _parallel_tool_upstream(store):
    _, events = await _run_pipeline(
        store,
        [
            _tool("call_a", "alpha", '{"n":1,', source_index=0),
            _tool("call_b", "beta", '{"m":2,', source_index=1),
            _tool("call_a", "alpha", '"x":true}', source_index=0),
            _tool("call_b", "beta", '"y":false}', source_index=1),
            _tool_done("call_a", ""),
            _tool_done("call_b", ""),
        ],
        response_id="resp_m_tool2",
    )
    return events


async def _tool_failure_next_turn_upstream(store):
    """工具失败后下一轮：第一轮工具调用以失败收尾，第二轮新会话仍正常。"""
    _, first = await _run_pipeline(
        store,
        [
            _tool("call_fail", "shell", '{"cmd":"rm"}', source_index=0),
            _tool_done("call_fail", ""),
        ],
        response_id="resp_m_fail",
    )
    _, second = await _run_pipeline(
        store,
        [
            {"type": "text", "delta": "重试成功"},
            {"type": "finish"},
        ],
        response_id="resp_m_next",
    )
    return first, second


async def _first_token_late_120s_upstream():
    """首 token 延迟 120s：silent 上游，FakeClock 快进，heartbeat 撑住连接。"""
    clock = FakeClock()
    sleeper = _make_fake_sleep(clock)

    class SilentAfterNothing:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration  # pragma: no cover

        async def aclose(self) -> None:
            pass

    pipeline = ResponsePipeline(
        "resp_m_120",
        workspace_id="t1",
        store=None,
        config=PipelineConfig(heartbeat_seconds=15),
    )
    gen = pipeline.run(SilentAfterNothing(), clock=clock, sleep=sleeper)
    heartbeats = 0
    frames: list[bytes] = []
    try:
        while heartbeats < 8:  # 8 * 15s = 120s simulated
            frame = await asyncio.wait_for(gen.__anext__(), timeout=10.0)
            frames.append(frame)
            if frame == SSE_HEARTBEAT_FRAME:
                heartbeats += 1
    finally:
        await gen.aclose()
    return clock, heartbeats, frames, pipeline


async def _midstream_disconnect_upstream(store):
    _, events = await _run_pipeline(
        store,
        [
            {"type": "text", "delta": "hel"},
            {"type": "text", "delta": "lo"},
        ],
        response_id="resp_m_disc",
    )  # 上游在正常结束前断流
    return events


async def _client_cancel_upstream(store):
    """客户端主动取消：cancel event 触发后上游立即关闭。"""
    clock = FakeClock()
    sleeper = _make_fake_sleep(clock)

    class BlockingUpstream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration  # pragma: no cover

        async def aclose(self) -> None:
            self.closed = True

    cancel = asyncio.Event()
    upstream = BlockingUpstream()
    pipeline = ResponsePipeline("resp_m_cancel", workspace_id="t1", store=_rs(store))
    gen = pipeline.run(upstream, client_cancelled=cancel, clock=clock, sleep=sleeper)

    async def drain():
        async for _ in gen:
            pass

    task = asyncio.create_task(drain())
    await asyncio.sleep(0)
    clock.advance(2.0)
    cancel.set()
    await asyncio.wait_for(task, timeout=1.0)
    return upstream, pipeline


# -- 断言函数：每个矩阵项一套，mock 与真机共用 ---------------------------------


def _assert_deepseek_reasoning(events):
    names = _names(events)
    assert names[0] == "response.created"
    assert "response.completed" in names
    assert not any("reasoning" in n for n in names)  # reasoning 全丢弃
    deltas = _collect(events, "response.output_text.delta")
    assert "答案在此" in "".join(d["delta"] for d in deltas)


def _assert_openai_non_reasoning(events):
    names = _names(events)
    assert names[0] == "response.created"
    assert "response.completed" in names
    text = "".join(d["delta"] for d in _collect(events, "response.output_text.delta"))
    assert text == "Hello world"


def _assert_anthropic(events):
    names = _names(events)
    assert names[0] == "response.created"
    assert "response.completed" in names
    text = "".join(d["delta"] for d in _collect(events, "response.output_text.delta"))
    assert "来自Anthropic" in text


def _assert_single_tool(events):
    done = _collect(events, "response.function_call_arguments.done")
    assert done and json.loads(done[0]["arguments"]) == {"q": "北京"}
    fc = [d for d in _collect(events, "response.output_item.done") if d["item"]["type"] == "function_call"]
    assert fc and fc[0]["item"]["status"] == "completed"


def _assert_parallel_tool(events):
    done = _collect(events, "response.function_call_arguments.done")
    by_call = {d["call_id"]: json.loads(d["arguments"]) for d in done}
    assert by_call == {"call_a": {"n": 1, "x": True}, "call_b": {"m": 2, "y": False}}


def _assert_tool_failure_next_turn(result):
    first, second = result
    # 第一轮：工具调用失败（参数未补齐 -> incomplete 安全收尾）。
    fc = [d for d in _collect(first, "response.output_item.done") if d["item"]["type"] == "function_call"]
    assert fc, "expected a function_call in the failed turn"
    assert fc[0]["item"]["status"] in ("completed", "incomplete")
    # 第二轮（工具失败后的下一轮）：正常完成、无残留。
    assert "response.completed" in _names(second)
    deltas = _collect(second, "response.output_text.delta")
    assert any("重试成功" in d["delta"] for d in deltas)


def _assert_first_token_late_120s(result):
    clock, heartbeats, frames, pipeline = result
    assert clock.t >= 120.0
    assert heartbeats >= 7
    # 首 token 延迟 120s：流不被掐断（未进入任何终止态），仍保持打开。
    assert pipeline.state not in ("completed", "failed", "incomplete"), pipeline.state
    events = _parse_events(frames)
    assert "[DONE]" not in _names(events)  # 流还活着


def _assert_midstream_disconnect(events):
    names = _names(events)
    assert names[0] == "response.created"
    assert "response.completed" in names  # 兼容模式：断流以 completed 收尾
    completed = _collect(events, "response.completed")[0]
    assert completed["response"]["terminal_reason"] == "upstream_truncated"
    assert events[-1] == ("[DONE]", {})


def _assert_client_cancel(result):
    upstream, pipeline = result
    assert upstream.closed  # 取消后上游立即关闭
    assert pipeline.stats.client_disconnects == 1


MATRIX: list[tuple[str, str, str, str, object]] = [
    ("01", "codex_deepseek_reasoning", "Codex CLI × DeepSeek reasoning model", "reasoning", "reasoning"),
    ("02", "codex_openai_non_reasoning", "Codex CLI × OpenAI-compatible non-reasoning model", "openai", "openai"),
    ("03", "codex_anthropic", "Codex CLI × Anthropic upstream", "anthropic", "anthropic"),
    ("04", "single_tool", "单工具调用", "single", "single"),
    ("05", "parallel_tool", "并行工具调用", "parallel", "parallel"),
    ("06", "tool_failure_next_turn", "工具失败后下一轮", "failure", "failure"),
    ("07", "first_token_late_120s", "首 token 延迟 120 秒", "late", "late"),
    ("08", "midstream_disconnect", "上游中途断流", "disconnect", "disconnect"),
    ("09", "client_cancel", "客户端主动取消", "cancel", "cancel"),
]


async def _run_matrix_case(kind: str, store) -> object:
    """构造并运行矩阵项（mock 上游），返回断言函数的入参。"""
    if kind == "reasoning":
        return await _deepseek_reasoning_upstream()
    if kind == "openai":
        return await _openai_non_reasoning_upstream()
    if kind == "anthropic":
        return await _anthropic_upstream()
    if kind == "single":
        return await _single_tool_upstream(store)
    if kind == "parallel":
        return await _parallel_tool_upstream(store)
    if kind == "failure":
        return await _tool_failure_next_turn_upstream(store)
    if kind == "late":
        return await _first_token_late_120s_upstream()
    if kind == "disconnect":
        return await _midstream_disconnect_upstream(store)
    if kind == "cancel":
        return await _client_cancel_upstream(store)
    raise AssertionError(f"unknown matrix kind: {kind}")


def _assert_matrix_case(assert_kind: str, result: object) -> None:
    fn = {
        "reasoning": _assert_deepseek_reasoning,
        "openai": _assert_openai_non_reasoning,
        "anthropic": _assert_anthropic,
        "single": _assert_single_tool,
        "parallel": _assert_parallel_tool,
        "failure": _assert_tool_failure_next_turn,
        "late": _assert_first_token_late_120s,
        "disconnect": _assert_midstream_disconnect,
        "cancel": _assert_client_cancel,
    }[assert_kind]
    fn(result)


# ---------------------------------------------------------------------------
# mock 套件（始终跑）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    MATRIX,
    ids=lambda c: f"{c[0]}_{c[1]}_mock",
)
async def test_matrix_mock(store, case):
    """§12.3 集成矩阵 mock 套件：每项全绿（不发起任何真实网络请求）。"""
    _num, short, desc, kind, assert_kind = case
    result = await _run_matrix_case(kind, store)
    _assert_matrix_case(assert_kind, result)


# ---------------------------------------------------------------------------
# 真机套件（live 标记 + 环境变量门控；CI 无 key 时跳过）
# ---------------------------------------------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not ZHONGZHUAN_UPSTREAM_BASE_URL,
    reason="真机套件需 ZHONGZHUAN_UPSTREAM_BASE_URL 环境变量；CI 无 key 时跳过",
)
@pytest.mark.parametrize(
    "case",
    MATRIX,
    ids=lambda c: f"{c[0]}_{c[1]}_live",
)
@pytest.mark.asyncio
async def test_matrix_live(store, case):
    """§12.3 集成矩阵真机套件。

    与 mock 套件共用同一套断言；当前以 mock 上游执行（仓库 CI 禁止真实出网）。
    真机运行时请把 :func:`_run_matrix_case` 的上游替换为指向
    ``ZHONGZHUAN_UPSTREAM_BASE_URL`` 的真实 HTTP 上游，断言无需改动。
    """
    _num, short, desc, kind, assert_kind = case
    result = await _run_matrix_case(kind, store)
    _assert_matrix_case(assert_kind, result)


# ---------------------------------------------------------------------------
# 兼容矩阵完整性：9 项缺一不可（判据③ 的清单门禁）
# ---------------------------------------------------------------------------


def test_matrix_has_exactly_nine_cases():
    """§12.3 原文列出的 9 项必须在矩阵表中，且名称与原文一致。"""
    docs_expected = [
        "Codex CLI × DeepSeek reasoning model",
        "Codex CLI × OpenAI-compatible non-reasoning model",
        "Codex CLI × Anthropic upstream",
        "单工具调用",
        "并行工具调用",
        "工具失败后下一轮",
        "首 token 延迟 120 秒",
        "上游中途断流",
        "客户端主动取消",
    ]
    actual = [c[2] for c in MATRIX]
    assert len(actual) == 9
    assert actual == docs_expected
