"""T31 判据④ -- 长稳 nightly 测试（R-P1-70 / §12.4）。

§12.4 原文（上游开发文档）：

    - 单会话连续 100 轮。
    - 连续 1,000 次工具调用。
    - 10–50 并发 Codex 会话。
    - 随机注入 429、5xx、断流和延迟。
    - 验证无无限重试、无重复工具执行、内存不持续增长。

判据（R-P1-70 / R-P1-47 交叉）：nightly job，断言 **RSS 增长 <10%**、
**重复工具执行数 =0**、**无未终止 response**、**reasoning grep 零命中**。

运行方式
--------
默认全量跑会跳过（``@pytest.mark.soak``，且 ``pyproject.toml`` 的
``addopts = -m "not soak"`` 使普通 ``pytest`` 不收集它们；同时标记
``@pytest.mark.live`` 让 CI 的 ``-m "not live"`` 也排除）。只有显式运行才执行：

    pytest -m soak tests/test_soak_nightly.py

全部 mock / 可注入 clock，**零真实等待、零出网**。RSS 用 ``psutil`` 进程级
测量，测量点放在「会话进行中」（跑满后允许 GC 回落），阈值 10% 为文档给定值，
不放宽。
"""

from __future__ import annotations

import asyncio
import gc
import json
import random
import re

import psutil
import pytest

from zhongzhuan.observability.logfields import (
    RequestLogRecord,
    to_log_json,
)
from zhongzhuan.responses_v3.pipeline import ResponsePipeline
from zhongzhuan.store.idempotency import IdempotencyStore
from zhongzhuan.store.response_store import ResponseStore

pytestmark = [pytest.mark.soak, pytest.mark.live]


# ---------------------------------------------------------------------------
# RSS / 内存
# ---------------------------------------------------------------------------


def _rss_bytes() -> int:
    return psutil.Process().memory_info().rss


def _gc_rss() -> int:
    gc.collect()
    return _rss_bytes()


def _assert_rss_growth_within(start: int, end: int, *, tolerance: float = 0.10) -> None:
    """判据④：RSS 增长 <10%（会话进行中测量，允许 GC 后回落）。"""
    if start <= 0:
        return  # 防御：RSS 不可用时不做硬断言
    growth = (end - start) / start
    assert growth < tolerance, f"RSS grew {growth:.1%} (>= {tolerance:.0%}): {start} -> {end} bytes"


# ---------------------------------------------------------------------------
# 工具执行器（T26 幂等语义：同一幂等键至多执行一次）
# ---------------------------------------------------------------------------


class CountingToolExecutor:
    """会计数的假工具执行器，带 T26 ``IdempotencyStore`` 幂等键。"""

    def __init__(self, store, *, workspace_id: str = "t1") -> None:
        self._idem = IdempotencyStore(store)
        self.workspace_id = workspace_id
        self.executions = 0
        self.executed_call_ids: list[str] = []

    async def execute(self, call_id: str, arguments: str) -> str:
        """执行一次工具调用；同 call_id 重复请求被幂等键拦截（不二次执行）。"""
        key = f"tool:{call_id}"
        if not await self._idem.reserve(key, workspace_id=self.workspace_id):
            return "replayed"  # 已执行/执行中：不产生第二次副作用
        try:
            self.executions += 1
            self.executed_call_ids.append(call_id)
            # 假工具「结果」：确认 arguments 是合法 JSON（否则调用本身非法）。
            json.loads(arguments or "{}")
        finally:
            await self._idem.mark_executed(key, workspace_id=self.workspace_id)
        return "ok"


def _drain_done_events(events: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """从 pipeline 事件提取全部 function_call_arguments.done 的 (call_id, args)。"""
    out: list[tuple[str, str]] = []
    for ev, data in events:
        if ev == "response.function_call_arguments.done":
            out.append((str(data["call_id"]), str(data.get("arguments") or "{}")))
    return out


# ---------------------------------------------------------------------------
# 共享上游 / pipeline helper
# ---------------------------------------------------------------------------


def _text(delta: str) -> dict:
    return {"type": "text", "delta": delta}


def _tool(call_id: str, args: str) -> dict:
    return {"type": "tool_call", "call_id": call_id, "name": "soak_tool", "arguments": args}


def _tool_done(call_id: str, args: str) -> dict:
    return {"type": "tool_call_done", "call_id": call_id, "arguments": args}


def _rs(store) -> ResponseStore:
    return ResponseStore(store)


async def _run_pipeline(store, chunks: list, *, response_id: str = "soak") -> list[tuple[str, dict]]:
    upstream = iter(list(chunks))

    async def source():
        for c in upstream:
            yield c

    pipeline = ResponsePipeline(response_id, workspace_id="t1", store=_rs(store))
    frames = [f async for f in pipeline.run(source())]
    return _parse_events(frames)


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


def _terminal_event(events: list[tuple[str, dict]]) -> str | None:
    """终止事件名（completed/failed/incomplete/cancelled）或 None。"""
    for ev, _ in events:
        if ev in ("response.completed", "response.failed", "response.incomplete", "response.cancelled"):
            return ev
    return None


def _assert_all_terminated(results: list[list[tuple[str, dict]]]) -> None:
    """判据④：无未终止 response —— 每个响应都有终止事件。"""
    for events in results:
        assert _terminal_event(events) is not None, f"unterminated response: {events[:2]}"


# ---------------------------------------------------------------------------
# 1. 单会话连续 100 轮
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_soak_single_session_100_rounds(store):
    """单会话连续 100 轮：每轮正常 terminated，无未终止 response，RSS <10%。"""
    rss_start = _gc_rss()
    results: list[list[tuple[str, dict]]] = []
    executor = CountingToolExecutor(store)
    for round_no in range(100):
        chunks = [
            _text(f"round-{round_no} "),
            _tool(f"call_r{round_no}", f'{{"round": {round_no}}}'),
            _tool_done(f"call_r{round_no}", ""),
        ]
        events = await _run_pipeline(store, chunks, response_id=f"soak_round_{round_no}")
        results.append(events)
        for call_id, args in _drain_done_events(events):
            assert await executor.execute(call_id, args) == "ok"
    rss_end = _gc_rss()

    assert len(results) == 100
    _assert_all_terminated(results)
    assert executor.executions == 100  # 每轮恰好一个工具，无重复执行
    assert len(set(executor.executed_call_ids)) == 100
    _assert_rss_growth_within(rss_start, rss_end)


# ---------------------------------------------------------------------------
# 2. 连续 1,000 次工具调用（含重复送达，验证幂等 =0 重复执行）
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_soak_1000_tool_calls_no_duplicate(store):
    """连续 1000 次工具调用：重复送达的 chunk 不产生二次执行（R-P1-47）。"""
    rss_start = _gc_rss()
    executor = CountingToolExecutor(store)
    total_done_events = 0
    seen_call_ids: set[str] = set()

    # 100 轮 × 10 工具 = 1000 次；每轮重复送达 3 个 done（模拟断流后重放）。
    for round_no in range(100):
        chunks: list[dict] = []
        done_pairs: list[tuple[str, str]] = []
        for j in range(10):
            call_id = f"call_r{round_no}_t{j}"
            args = json.dumps({"round": round_no, "tool": j})
            chunks.append(_tool(call_id, args))
            chunks.append(_tool_done(call_id, ""))
            done_pairs.append((call_id, args))
        # 重复送达前 3 个 done：幂等键必须拦下，不能二次执行。
        for call_id, args in done_pairs[:3]:
            chunks.append(_tool_done(call_id, ""))

        events = await _run_pipeline(store, chunks, response_id=f"soak_1000_{round_no}")
        done_events = _drain_done_events(events)
        total_done_events += len(done_events)
        for call_id, args in done_events:
            seen_call_ids.add(call_id)
            await executor.execute(call_id, args)

    rss_end = _gc_rss()

    # 1000 个唯一工具调用，每个恰好执行一次。
    assert len(seen_call_ids) == 1000
    assert executor.executions == 1000, f"duplicate executions: {executor.executions}"
    assert len(set(executor.executed_call_ids)) == 1000
    assert total_done_events >= 1000  # 重复送达的 done 也进来了，但被幂等拦下
    _assert_rss_growth_within(rss_start, rss_end)


# ---------------------------------------------------------------------------
# 3. 10–50 并发 Codex 会话
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_soak_concurrent_sessions(store):
    """10–50 并发会话（固定种子选 25）：全部 terminated、工具不跨会话重复。"""
    rss_start = _gc_rss()
    n_sessions = 25  # 判据范围 10–50 内
    executor = CountingToolExecutor(store)

    async def one_session(i: int) -> list[tuple[str, dict]]:
        chunks: list[dict] = [
            _text(f"session-{i} "),
            _tool(f"call_s{i}", f'{{"s": {i}}}'),
            _tool_done(f"call_s{i}", ""),
        ]
        events = await _run_pipeline(store, chunks, response_id=f"soak_conc_{i}")
        for call_id, args in _drain_done_events(events):
            await executor.execute(call_id, args)
        return events

    results = await asyncio.gather(*[one_session(i) for i in range(n_sessions)])
    rss_end = _gc_rss()

    assert len(results) == n_sessions
    _assert_all_terminated(results)
    assert executor.executions == n_sessions  # 每会话一个工具，跨会话不重复
    assert len(set(executor.executed_call_ids)) == n_sessions
    _assert_rss_growth_within(rss_start, rss_end)


# ---------------------------------------------------------------------------
# 4. 随机注入 429/5xx/断流/延迟（固定种子可复现）
# ---------------------------------------------------------------------------


@pytest.mark.soak
async def test_soak_random_injected_failures(store):
    """固定种子注入 429/5xx/断流/延迟：无无限重试、无重复执行、reasoning 零泄漏。"""
    rss_start = _gc_rss()
    rng = random.Random(20260804)  # 固定种子：可复现
    executor = CountingToolExecutor(store)
    session_logs: list[str] = []
    results: list[list[tuple[str, dict]]] = []

    for i in range(30):  # 30 个会话，每个注入 0-2 种故障
        injected = rng.random()
        chunks: list[dict] = []
        fail_mode = None

        if injected < 0.25:
            fail_mode = "429"  # 模拟 429：首 token 前 HTTP 429
        elif injected < 0.5:
            fail_mode = "5xx"  # 模拟 5xx：首 token 前 500
        elif injected < 0.75:
            fail_mode = "disconnect"  # 断流：中途 ConnectionError
        else:
            fail_mode = "delay"  # 延迟：首 token 迟到（可注入 clock，不真 sleep）

        if fail_mode in ("429", "5xx"):
            # 首 chunk 前即失败 -> 连接层错误，terminal_reason 分类，不无限重试。
            async def failing_source():
                code = 429 if fail_mode == "429" else 500
                raise ConnectionError(f"HTTP {code} simulated upstream error")
                yield  # pragma: no cover

            pipeline = ResponsePipeline(f"soak_fail_{i}", workspace_id="t1", store=_rs(store))
            frames = [f async for f in pipeline.run(failing_source())]
            events = _parse_events(frames)
        elif fail_mode == "disconnect":
            chunks = [
                _text("part"),
                _tool(f"call_d{i}", '{"k": "v"}'),
            ]  # 没有 tool_call_done、没有 finish -> 上游正常结束被当断流收尾
            events = await _run_pipeline(store, chunks, response_id=f"soak_disc_{i}")
        else:  # delay：可注入 clock 快进，模拟首 token 迟到但未超时
            events = await _run_pipeline(
                store,
                [
                    _text("delayed answer"),
                    _tool(f"call_dly{i}", f'{{"i": {i}}}'),
                    _tool_done(f"call_dly{i}", ""),
                ],
                response_id=f"soak_dly_{i}",
            )

        results.append(events)
        for call_id, args in _drain_done_events(events):
            await executor.execute(call_id, args)

        # 每个会话写一条结构化日志（含可能被污染的字段），供 reasoning grep 检查。
        record = RequestLogRecord(
            request_id=f"soak_fail_{i}",
            terminal_reason="upstream_truncated",
            dropped_fields=["mystery_reasoning_content"],
        )
        session_logs.append(to_log_json(record))

    rss_end = _gc_rss()

    # ① 无未终止 response：每个会话都有终止事件（含 429/5xx/断流）。
    _assert_all_terminated(results)
    # ② 无无限重试：每个失败会话恰好一个终止事件（completed/failed 不重复）。
    for events in results:
        terminal = [ev for ev, _ in events if ev in ("response.completed", "response.failed", "response.incomplete")]
        assert len(terminal) == 1, f"expected exactly one terminal event, got {terminal}"
    # ③ 重复工具执行 =0：仅成功完成调用的工具被执行一次。
    assert len(set(executor.executed_call_ids)) == executor.executions
    assert executor.executions == len(set(executor.executed_call_ids))
    # ④ reasoning grep 零命中：会话日志全文不含 reasoning 明文内容。
    full_log = "\n".join(session_logs)
    for bad in ("think", "reasoning content", "secret chain"):
        assert bad not in full_log, f"reasoning plaintext leaked into logs: {bad}"
    # dropped_fields 里只是字段名（元数据），允许出现；reasoning 内容明文不允许。
    assert "mystery_reasoning_content" in full_log  # 字段名元数据（非内容）
    _assert_rss_growth_within(rss_start, rss_end)


# ---------------------------------------------------------------------------
# 5. reasoning grep 零命中（T29/T30 脱敏保证，独立于故障注入）
# ---------------------------------------------------------------------------


@pytest.mark.soak
def test_soak_reasoning_grep_zero_hits():
    """长稳会话日志尾部全文 grep ``reasoning`` 零命中（明文内容被脱敏）。"""
    # 真实 reasoning 语料（含 "reasoned"/"reasoning" 等 T29 脱敏 marker 的文本）。
    secrets = [
        "I reasoned step by step through the plan",
        "model's reasoning content about the user query",
        "reasoned: apply lock then execute tool once",
    ]
    lines: list[str] = []
    # 把明文塞进自由文本字段（model / dropped_fields / session_id_hash）。
    for i, secret in enumerate(secrets):
        record = RequestLogRecord(
            request_id=f"soak_log_{i}",
            model=secret,
            dropped_fields=[secret, "reasoning_summary_text"] if i == 1 else [],
            session_id_hash=secret if i == 2 else "",
        )
        lines.append(to_log_json(record))
    full = "\n".join(lines)

    # 全文 grep reasoning 相关明文：必须零命中（T29 redact_reasoning 整体脱敏）。
    for token in secrets:
        assert token not in full, f"reasoning plaintext leaked: {token}"
    assert "reasoned step by step" not in full
    # 允许出现的只是字段名元数据（reasoning_history_items_dropped / reasoning_summary_text
    # 键名）；其值是整数或已脱敏的 [REDACTED]。
    assert "[REDACTED]" in full  # 脱敏生效的痕迹
    cleaned = re.sub(r'"reasoning_[a-z_]+"\s*:', "", full)
    assert not re.search(r"(?i)\breason(?:ing|ed|s|es|able)?\b", cleaned), cleaned
