"""T24 tests: background worker, lease/recovery, cancel, catch-up, budgets.

Acceptance mapping
------------------
① R-P1-34  state machine + sub-second enqueue ......... test_enqueue_*, test_five_terminal_states
② R-P1-35  cancel closes upstream -> ``cancelled`` .... test_cancel_closes_upstream_and_marks_cancelled
③ R-P1-36  catch-up == live, seq for seq .............. test_catchup_matches_live_sequence_numbers
④ R-P1-37  recover exactly once, then ``failed`` ...... test_recovery_runs_exactly_once_then_failed
⑤ R-P1-38  five background budget reasons ............. test_bg_budget_*
⑥ R-P1-34  concurrent claim -> exactly one winner ..... test_concurrent_claim_only_one_wins
"""

from __future__ import annotations

import asyncio
import time

import pytest
from loguru import logger

from zhongzhuan.responses_v3.background import BackgroundWorker
from zhongzhuan.responses_v3.budget import ExecutionBudget
from zhongzhuan.responses_v3.catchup import CatchupStream
from zhongzhuan.responses_v3.pipeline import ResponsePipeline, sse_frame
from zhongzhuan.store.background_jobs import (
    MAX_RECOVERY_ATTEMPTS,
    BackgroundJobStore,
)
from zhongzhuan.store.response_store import ResponseStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeUpstream:
    """Closable async iterable standing in for the real provider stream.

    It is an ``AsyncIterable`` *object* (not a generator) precisely so the
    worker's ``cancel_upstream`` has something observable to close -- an async
    generator's ``aclose`` would be invisible to the test.
    """

    def __init__(self, chunks, *, on_chunk=None, raises: Exception | None = None):
        self._chunks = list(chunks)
        self._on_chunk = on_chunk
        self._raises = raises
        self._iter = None
        self.closed = False
        self.consumed: list = []
        self.iterations = 0

    def __aiter__(self):
        self._iter = iter(self._chunks)
        self.iterations += 1
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        if self._raises is not None:
            raise self._raises
        try:
            chunk = next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None
        if self._on_chunk is not None:
            await self._on_chunk(len(self.consumed))
        if self.closed:
            raise StopAsyncIteration
        self.consumed.append(chunk)
        return chunk

    async def aclose(self) -> None:
        self.closed = True


class StepClock:
    """Monotonic-shaped clock returning a scripted sequence of readings."""

    def __init__(self, readings):
        self._readings = list(readings)
        self._last = self._readings[-1] if self._readings else 0.0

    def __call__(self) -> float:
        if self._readings:
            self._last = self._readings.pop(0)
        return float(self._last)


def text(delta: str = "hi", tokens: int = 1) -> dict:
    return {"type": "output_text.delta", "delta": delta, "tokens": tokens}


async def empty_upstream():
    """Zero-chunk async generator (the T21 pipeline's happy path)."""
    return
    yield  # pragma: no cover


@pytest.fixture
def rs(store):
    return ResponseStore(store)


async def _seed(worker: BackgroundWorker, response_id: str, **kwargs):
    """Enqueue one background response and return its record."""
    return await worker.enqueue(
        response_id=response_id,
        workspace_id="t1",
        model="gpt-4o",
        request={"model": "gpt-4o", "input": "hi", "background": True},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ① R-P1-34 -- enqueue returns immediately, five terminal states exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_returns_queued_under_one_second(rs):
    worker = BackgroundWorker(rs)
    started = time.monotonic()
    record = await asyncio.wait_for(_seed(worker, "resp_q"), timeout=1.0)
    assert time.monotonic() - started < 1.0
    assert record is not None
    assert record.status == "queued"
    assert record.background is True
    job = await rs.jobs.get_job("resp_q", workspace_id="t1")
    assert job["status"] == "queued"
    assert job["attempt"] == 0


@pytest.mark.asyncio
async def test_queued_event_emitted_only_for_background(rs):
    """``response.queued`` precedes ``response.created`` and only for background."""
    worker = BackgroundWorker(rs)
    await _seed(worker, "resp_qe")
    events = await rs.list_events("resp_qe")
    assert [e["event_type"] for e in events] == ["response.queued"]
    assert events[0]["data"]["response"]["status"] == "queued"

    # A synchronous stream (the T21 pipeline) never emits ``response.queued``.
    pipeline = ResponsePipeline("resp_sync", store=rs)
    async for _frame in pipeline.run(empty_upstream()):
        pass
    sync_events = [e["event_type"] for e in await rs.list_events("resp_sync")]
    assert "response.queued" not in sync_events
    assert sync_events[0] == "response.created"


@pytest.mark.asyncio
async def test_five_terminal_states(rs):
    worker = BackgroundWorker(rs)

    # completed -- a normal run.
    await _seed(worker, "resp_ok")
    assert await worker.run_job("resp_ok", upstream=FakeUpstream([text()])) == "completed"

    # failed -- the executor raised (R-P0-32: attributed, never laundered).
    await _seed(worker, "resp_bad")
    boom = FakeUpstream([], raises=RuntimeError("upstream exploded"))
    assert await worker.run_job("resp_bad", upstream=boom) == "failed"

    # incomplete -- a budget ceiling tripped the circuit breaker.
    tight = ExecutionBudget(max_tool_rounds=1, max_wall_time_seconds=3600)
    await _seed(worker, "resp_inc", budget=tight)
    rounds = FakeUpstream([{"type": "tool_round"}, {"type": "tool_round"}])
    assert await worker.run_job("resp_inc", upstream=rounds, budget=tight) == "incomplete"

    # cancelled -- the flag was raised before the first round boundary.
    await _seed(worker, "resp_cancel")
    await rs.jobs.request_cancel("resp_cancel")
    stream = FakeUpstream([text(), text()])
    assert await worker.run_job("resp_cancel", upstream=stream) == "cancelled"

    # expired -- the job's TTL lapsed before anyone claimed it.
    await _seed(worker, "resp_exp", expires_at=int(time.time()) - 5)
    assert await worker.run_job("resp_exp", upstream=FakeUpstream([text()])) == "expired"

    expected = {
        "resp_ok": "completed",
        "resp_bad": "failed",
        "resp_inc": "incomplete",
        "resp_cancel": "cancelled",
        "resp_exp": "expired",
    }
    for response_id, status in expected.items():
        record = await rs.get_response(response_id, workspace_id="t1")
        assert record is not None, response_id
        assert record.status == status, response_id
        events = await rs.list_events(response_id)
        assert events, response_id

    # The job rows landed on the same five terminal states.
    for task_id, status in expected.items():
        job = await rs.jobs.get_job(task_id, workspace_id="t1")
        assert job["status"] == status, task_id


@pytest.mark.asyncio
async def test_heartbeat_renews_lease_while_running(rs):
    """The lease is pushed forward while the job is alive (R-P1-34)."""
    worker = BackgroundWorker(rs, heartbeat_seconds=0.01, lease_seconds=120)
    await _seed(worker, "resp_hb")

    async def wait_a_bit(_index):
        await asyncio.sleep(0.05)

    stream = FakeUpstream([text(), text()], on_chunk=wait_a_bit)
    assert await worker.run_job("resp_hb", upstream=stream) == "completed"
    job = await rs.jobs.get_job("resp_hb", workspace_id="t1")
    assert job["lease_until"] >= int(time.time())


# ---------------------------------------------------------------------------
# ② R-P1-35 -- cancel closes the upstream and lands on ``cancelled``
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_closes_upstream_and_marks_cancelled(rs):
    worker = BackgroundWorker(rs)
    await _seed(worker, "resp_c1")

    async def cancel_after_first(index):
        if index == 1:
            await worker.cancel("resp_c1")

    stream = FakeUpstream([text("a"), text("b"), text("c")], on_chunk=cancel_after_first)
    status = await worker.run_job("resp_c1", upstream=stream)

    assert status == "cancelled"
    assert stream.closed is True, "cancel must close the upstream, not just flag it"
    assert len(stream.consumed) == 1, "no chunk is consumed after the cancel"
    assert await rs.jobs.is_cancel_requested("resp_c1") is True
    record = await rs.get_response("resp_c1", workspace_id="t1")
    assert record.status == "cancelled"
    assert record.terminal_reason == "cancelled_by_client"
    job = await rs.jobs.get_job("resp_c1", workspace_id="t1")
    assert job["status"] == "cancelled"
    types = [e["event_type"] for e in await rs.list_events("resp_c1")]
    assert types[0] == "response.queued"
    assert types[-1] == "response.cancelled"


# ---------------------------------------------------------------------------
# ③ R-P1-36 -- catch-up replays the live sequence, event for event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catchup_matches_live_sequence_numbers(rs):
    pipeline = ResponsePipeline("resp_live", workspace_id="t1", store=rs)
    live_frames = [frame async for frame in pipeline.run(empty_upstream())]

    live_events = await rs.list_events("resp_live")
    catchup = CatchupStream(rs)
    replayed_events = await catchup.events("resp_live")
    replayed_frames = [frame async for frame in catchup.replay("resp_live")]

    # Same events, same order, same sequence numbers -- one log, two readers.
    assert [e["seq"] for e in live_events] == [e["seq"] for e in replayed_events]
    for live, replayed in zip(live_events, replayed_events):
        assert live["seq"] == replayed["seq"]
        assert live["event_type"] == replayed["event_type"]
        assert live["data"] == replayed["data"]

    # Monotonic, gapless.
    seqs = [e["seq"] for e in replayed_events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    # Byte-identical frames: every emitted frame (including the terminal
    # response.completed) is also a logged event, so the catch-up replay
    # reproduces the live stream exactly.
    assert replayed_frames == live_frames
    assert replayed_frames == [sse_frame(e["event_type"], e["data"]) for e in live_events]


@pytest.mark.asyncio
async def test_catchup_after_seq_resumes_midstream(rs):
    worker = BackgroundWorker(rs)
    await _seed(worker, "resp_cu")
    await worker.run_job("resp_cu", upstream=FakeUpstream([text("a"), text("b")]))

    catchup = CatchupStream(rs)
    everything = await catchup.events("resp_cu")
    assert len(everything) > 2
    resumed = await catchup.events("resp_cu", after_seq=everything[1]["seq"])
    assert resumed == everything[2:]
    assert await catchup.last_seq("resp_cu") == everything[-1]["seq"]


# ---------------------------------------------------------------------------
# ④ R-P1-37 -- recover exactly once, then fail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recovery_runs_exactly_once_then_failed(rs):
    jobs = rs.jobs
    await rs.create_response(response_id="resp_r", workspace_id="t1", background=True)
    await jobs.create_job(task_id="task_r", response_id="resp_r", workspace_id="t1")

    t0 = 1_000_000
    # First run.
    assert await jobs.claim_job(60, now=t0) == "task_r"
    job = await jobs.get_job("task_r", workspace_id="t1")
    assert job["attempt"] == 1
    assert job["status"] == "in_progress"
    assert job["lease_until"] == t0 + 60

    # kill -9: no heartbeat, so the lease simply lapses.
    t1 = t0 + 61
    assert await jobs.claim_job(60, now=t1) == "task_r", "the one allowed recovery"
    job = await jobs.get_job("task_r", workspace_id="t1")
    assert job["attempt"] == MAX_RECOVERY_ATTEMPTS

    # kill -9 again: attempts are exhausted, so the job is failed, not retried.
    t2 = t1 + 61
    assert await jobs.claim_job(60, now=t2) is None
    job = await jobs.get_job("task_r", workspace_id="t1")
    assert job["status"] == "failed"
    assert job["attempt"] == MAX_RECOVERY_ATTEMPTS

    # The reason lands on the response (the job table has no such column).
    record = await rs.get_response("resp_r", workspace_id="t1")
    assert record.status == "failed"
    assert record.terminal_reason == "recovery_exhausted"


@pytest.mark.asyncio
async def test_no_double_side_effects_after_recovery(rs):
    """A job is handed out at most ``MAX_RECOVERY_ATTEMPTS`` times, ever."""
    jobs = rs.jobs
    await rs.create_response(response_id="resp_s", workspace_id="t1", background=True)
    await jobs.create_job(task_id="task_s", response_id="resp_s", workspace_id="t1")

    claims: list[str] = []
    now = 2_000_000
    for _ in range(5):
        claimed = await jobs.claim_job(60, now=now)
        if claimed is not None:
            claims.append(claimed)
        now += 61  # every attempt "crashes" and lets the lease lapse

    assert claims == ["task_s", "task_s"], "recovered exactly once"
    assert (await jobs.get_job("task_s", workspace_id="t1"))["status"] == "failed"


@pytest.mark.asyncio
async def test_live_lease_blocks_recovery(rs):
    """A heartbeating worker keeps its job invisible to the recovery path."""
    jobs = rs.jobs
    await jobs.create_job(task_id="task_l", response_id="resp_l", workspace_id="t1")
    t0 = 3_000_000
    assert await jobs.claim_job(60, now=t0) == "task_l"
    assert await jobs.renew_lease("task_l", 60, now=t0 + 30) is True
    # The lease was renewed, so at t0+61 the job is still owned.
    assert await jobs.claim_job(60, now=t0 + 61) is None
    assert (await jobs.get_job("task_l", workspace_id="t1"))["status"] == "in_progress"
    # An expired lease can no longer be renewed -- the holder lost it.
    assert await jobs.renew_lease("task_l", 60, now=t0 + 200) is False


@pytest.mark.asyncio
async def test_renew_lease_stops_on_terminal_status(rs):
    jobs = rs.jobs
    await jobs.create_job(task_id="task_t", response_id="resp_t", workspace_id="t1")
    assert await jobs.renew_lease("task_t", 60) is True
    await jobs.mark_terminal("task_t", "completed")
    assert await jobs.renew_lease("task_t", 60) is False
    assert await jobs.renew_lease("nope", 60) is False


# ---------------------------------------------------------------------------
# ⑤ R-P1-38 -- five distinct background budget reasons
# ---------------------------------------------------------------------------


async def _run_budget_case(rs, response_id, *, budget, chunks, **worker_kwargs):
    """Run one budget scenario and return the resulting response record."""
    worker = BackgroundWorker(rs, budget=budget, **worker_kwargs)
    await _seed(worker, response_id, budget=budget)
    status = await worker.run_job(
        response_id,
        upstream=FakeUpstream(chunks),
        budget=budget,
    )
    assert status == "incomplete"
    return await rs.get_response(response_id, workspace_id="t1")


def _call(name: str) -> dict:
    return {"type": "tool_call", "name": name, "arguments": {"q": name}}


@pytest.mark.asyncio
async def test_bg_budget_max_tool_rounds(rs):
    record = await _run_budget_case(
        rs,
        "bg_rounds",
        budget=ExecutionBudget(max_tool_rounds=1, max_wall_time_seconds=3600),
        chunks=[{"type": "tool_round"}, {"type": "tool_round"}],
    )
    assert record.incomplete_details["reason"] == "max_tool_rounds"
    assert record.terminal_reason == "max_tool_rounds"


@pytest.mark.asyncio
async def test_bg_budget_max_total_tool_calls(rs):
    record = await _run_budget_case(
        rs,
        "bg_calls",
        budget=ExecutionBudget(max_total_tool_calls=1, max_wall_time_seconds=3600),
        chunks=[_call("alpha"), _call("beta")],
    )
    assert record.incomplete_details["reason"] == "max_total_tool_calls"


@pytest.mark.asyncio
async def test_bg_budget_max_output_budget(rs):
    record = await _run_budget_case(
        rs,
        "bg_output",
        budget=ExecutionBudget(max_output_tokens_total=1, max_wall_time_seconds=3600),
        chunks=[text("a", 1), text("b", 1)],
    )
    assert record.incomplete_details["reason"] == "max_output_budget"


@pytest.mark.asyncio
async def test_bg_budget_max_wall_time(rs):
    record = await _run_budget_case(
        rs,
        "bg_wall",
        budget=ExecutionBudget(max_wall_time_seconds=1),
        chunks=[text("a"), text("b")],
        clock=StepClock([0.0, 100.0]),
    )
    assert record.incomplete_details["reason"] == "max_response_time"


@pytest.mark.asyncio
async def test_bg_budget_exhausted(rs):
    """The background envelope is its own ceiling, with its own reason."""
    record = await _run_budget_case(
        rs,
        "bg_envelope",
        budget=ExecutionBudget(max_wall_time_seconds=3600),  # generic ceilings wide open
        chunks=[_call("alpha"), _call("beta")],
        max_background_calls=1,
    )
    assert record.incomplete_details["reason"] == "background_budget_exhausted"


@pytest.mark.asyncio
async def test_bg_no_progress_loops_also_trip(rs):
    """R-P0-28 still applies inside a background job (identical call / failure)."""
    repeat = ExecutionBudget(max_identical_call_repeats=1, max_wall_time_seconds=3600)
    record = await _run_budget_case(
        rs,
        "bg_repeat",
        budget=repeat,
        chunks=[_call("alpha"), _call("alpha")],
    )
    assert record.incomplete_details["reason"] == "repeated_tool_call"

    failure = ExecutionBudget(max_identical_call_repeats=1, max_wall_time_seconds=3600)
    record = await _run_budget_case(
        rs,
        "bg_fail",
        budget=failure,
        chunks=[
            {"type": "tool_result", "signature": "sig-1", "failed": True},
            {"type": "tool_result", "signature": "sig-1", "failed": True},
        ],
    )
    assert record.incomplete_details["reason"] == "repeated_tool_failure"


@pytest.mark.asyncio
async def test_incomplete_details_reason_present(rs):
    """All five ceilings produce ``incomplete`` + a non-null, distinct reason."""
    cases = [
        (
            "p_rounds",
            ExecutionBudget(max_tool_rounds=1, max_wall_time_seconds=3600),
            [{"type": "tool_round"}, {"type": "tool_round"}],
            {},
        ),
        (
            "p_calls",
            ExecutionBudget(max_total_tool_calls=1, max_wall_time_seconds=3600),
            [_call("alpha"), _call("beta")],
            {},
        ),
        (
            "p_output",
            ExecutionBudget(max_output_tokens_total=1, max_wall_time_seconds=3600),
            [text("a", 1), text("b", 1)],
            {},
        ),
        (
            "p_wall",
            ExecutionBudget(max_wall_time_seconds=1),
            [text("a"), text("b")],
            {"clock": StepClock([0.0, 100.0])},
        ),
        (
            "p_envelope",
            ExecutionBudget(max_wall_time_seconds=3600),
            [_call("alpha"), _call("beta")],
            {"max_background_calls": 1},
        ),
    ]
    reasons = []
    for response_id, budget, chunks, kwargs in cases:
        record = await _run_budget_case(
            rs,
            response_id,
            budget=budget,
            chunks=chunks,
            **kwargs,
        )
        assert record.status == "incomplete", response_id
        assert record.incomplete_details.get("reason") is not None, response_id
        reasons.append(record.incomplete_details["reason"])

    assert len(set(reasons)) == 5, "each ceiling must be individually diagnosable"
    assert set(reasons) == {
        "max_tool_rounds",
        "max_total_tool_calls",
        "max_output_budget",
        "max_response_time",
        "background_budget_exhausted",
    }


# ---------------------------------------------------------------------------
# ⑥ R-P1-34 -- two workers, one job, exactly one winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_claim_only_one_wins(store, rs):
    await rs.jobs.create_job(task_id="task_cc", response_id="resp_cc", workspace_id="t1")
    jobs_a = BackgroundJobStore(store)
    jobs_b = BackgroundJobStore(store)

    now = 4_000_000
    results = await asyncio.gather(
        jobs_a.claim_job(300, now=now),
        jobs_b.claim_job(300, now=now),
    )
    assert sorted(results, key=lambda v: v is None) == ["task_cc", None]

    job = await rs.jobs.get_job("task_cc", workspace_id="t1")
    assert job["attempt"] == 1, "the CAS must not double-charge the attempt"
    assert job["status"] == "in_progress"
    assert job["lease_until"] == now + 300


@pytest.mark.asyncio
async def test_second_worker_sees_no_job_while_lease_holds(store, rs):
    """Sequential proof of the same invariant (no leased job is handed out twice)."""
    await rs.jobs.create_job(task_id="task_seq", response_id="resp_seq", workspace_id="t1")
    jobs_a = BackgroundJobStore(store)
    jobs_b = BackgroundJobStore(store)
    now = 5_000_000
    assert await jobs_a.claim_job(300, now=now) == "task_seq"
    assert await jobs_b.claim_job(300, now=now + 10) is None


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_loop_drains_queued_jobs(rs):
    worker = BackgroundWorker(rs)
    await _seed(worker, "resp_loop")
    await worker.start(
        poll_interval=0.01,
        upstream_factory=lambda _tid: FakeUpstream([text()]),
        max_iterations=3,
    )
    record = await rs.get_response("resp_loop", workspace_id="t1")
    assert record.status == "completed"
    assert (await rs.jobs.get_job("resp_loop", workspace_id="t1"))["status"] == "completed"


@pytest.mark.asyncio
async def test_run_job_returns_none_when_not_claimable(rs):
    """A lease held by someone else is ``None``; a finished job reports itself."""
    worker = BackgroundWorker(rs)
    await _seed(worker, "resp_taken")
    assert await rs.jobs.claim_job(300) == "resp_taken"
    assert await worker.run_job("resp_taken", upstream=FakeUpstream([text()])) is None

    await _seed(worker, "resp_done")
    assert await worker.run_job("resp_done", upstream=FakeUpstream([text()])) == "completed"
    assert await worker.run_job("resp_done", upstream=FakeUpstream([text()])) == "completed"


# ---------------------------------------------------------------------------
# Idle polling: event-driven wake-up (2026-09-22 TiDB RU leak)
#
# 事故：``peek_claimable`` 每秒一次全表扫描 ``background_jobs``（无
# ``(status, lease_until)`` 索引），零 job 也照跑 = 86,400 次/天，
# 控制台实测占当月 RU 的 98.6%（≈124M RU/月，免费额度的 2.5 倍）。
# 修复：入队事件唤醒 + 间隔退化为 30s 安全网（migration v017 补索引）。
# 这三条测试锁住的正是修复的三个不变量。
# ---------------------------------------------------------------------------


class CountingJobs:
    """``background_jobs`` 计数壳：统计 ``peek_claimable`` 被调了几次。

    ``on_first_peek`` 把「peek 返回空」与「进入等待」之间那个最窄的竞态窗口
    撑开成可断言的场景（见 :func:`test_notify_between_peek_and_wait_is_not_lost`）。
    """

    def __init__(self, inner, *, on_first_peek=None):
        self._inner = inner
        self._on_first_peek = on_first_peek
        self.peek_calls = 0

    async def peek_claimable(self, **kwargs):
        self.peek_calls += 1
        if self.peek_calls == 1 and self._on_first_peek is not None:
            await self._on_first_peek()
            return None
        return await self._inner.peek_claimable(**kwargs)

    def __getattr__(self, name):  # claim/lease/TTL 路径原样透传
        return getattr(self._inner, name)


def _wrap_jobs(worker: BackgroundWorker, *, on_first_peek=None) -> CountingJobs:
    """把 worker 的 jobs 存储换成计数壳（``_jobs`` 是 __init__ 注入的唯一接缝）。"""
    wrapped = CountingJobs(worker.jobs, on_first_peek=on_first_peek)
    worker._jobs = wrapped
    return wrapped


@pytest.mark.asyncio
async def test_idle_loop_is_not_busy_polling(rs):
    """空闲 2 秒内 ``peek_claimable`` ≤ 2 次（旧实现是 1 秒 1 次 → 2~3 次）。

    默认间隔 30s 时，2 秒窗口里只该有**进入循环的那一次**探测 —— 这条断言就是
    124M RU/月 那个 bug 的回归门禁。
    """
    worker = BackgroundWorker(rs)
    jobs = _wrap_jobs(worker)

    task = asyncio.create_task(worker.start(poll_interval=30.0))
    await asyncio.sleep(2.0)
    worker.stop()
    await asyncio.wait_for(task, timeout=5)

    assert jobs.peek_calls <= 2, f"空闲 2s 内探测了 {jobs.peek_calls} 次；1Hz 空转回归"
    assert worker.stats()["claims"] == 0


@pytest.mark.asyncio
async def test_enqueue_wakes_idle_loop_without_waiting_out_interval(rs):
    """入队即唤醒：30s 安全网下，job 仍在 1 秒内跑完（延迟不退反进）。

    间隔从 1s 提到 30s 之所以安全，全靠这条路径 —— 单独提间隔会把
    ``background=true`` 的启动延迟从 ≤1s 劣化到 ≤30s。
    """
    worker = BackgroundWorker(rs)
    task = asyncio.create_task(
        worker.start(
            poll_interval=30.0,
            upstream_factory=lambda _tid: FakeUpstream([text()]),
        )
    )
    await asyncio.sleep(0.1)  # 让循环先进入空闲等待
    await _seed(worker, "resp_wake")

    deadline = time.monotonic() + 2.0
    status = ""
    while time.monotonic() < deadline:
        job = await rs.jobs.get_job("resp_wake", workspace_id="t1")
        status = str(job["status"]) if job else ""
        if status == "completed":
            break
        await asyncio.sleep(0.02)
    worker.stop()
    await asyncio.wait_for(task, timeout=5)

    assert status == "completed", f"入队未被唤醒，job 停在 {status!r}"
    assert worker.stats()["claims"] == 1


@pytest.mark.asyncio
async def test_notify_between_peek_and_wait_is_not_lost(rs):
    """peek 返回空与开始等待之间的 notify 不能被吞。

    用「第一次 peek 时同步入队 + 通知」把窗口撑开：若唤醒事件被放在等待**之后**
    清理，这次通知就丢了，循环只能睡满 30s 安全网 → 本用例会超时失败。
    """
    worker = BackgroundWorker(rs)

    async def _race() -> None:
        # 模拟「探测刚返回空，入队者就插队进来」：写行（durable）+ 置位事件。
        await _seed(worker, "resp_race")

    jobs = _wrap_jobs(worker, on_first_peek=_race)

    task = asyncio.create_task(
        worker.start(
            poll_interval=30.0,
            upstream_factory=lambda _tid: FakeUpstream([text()]),
        )
    )
    deadline = time.monotonic() + 2.0
    status = ""
    while time.monotonic() < deadline:
        job = await rs.jobs.get_job("resp_race", workspace_id="t1")
        status = str(job["status"]) if job else ""
        if status == "completed":
            break
        await asyncio.sleep(0.02)
    worker.stop()
    await asyncio.wait_for(task, timeout=5)

    assert jobs.peek_calls >= 2, "竞态场景没被构造出来（只有一次探测）"
    assert status == "completed", f"窗口内的通知被吞掉了，job 停在 {status!r}"


@pytest.mark.asyncio
async def test_idle_report_logs_per_interval_deltas(rs):
    """空闲上报打的是**区间增量**（同时带累计值）。

    读日志的人不该需要知道进程是什么时候起的：只打累计值，就没法判断轮询速率。
    这条日志是 T+24h 验收的主要读数（健康时应是 ≈60 polls / 1800s）。

    捕获必须走 loguru sink（生产唯一的日志体系）：background.py 曾用 stdlib
    ``logging.getLogger``，caplog 测试全绿但生产里 INFO 被整个吞掉，空闲自报
    静默失效 —— 本用例用真实 sink 防止这类"测试绿、生产哑"的回归。
    """
    ticks = iter([1800.0, 1800.0, 3600.0])  # 到期 / 未到期 / 再次到期
    worker = BackgroundWorker(rs, clock=lambda: next(ticks))
    worker._poll_count = 60

    captured: list[str] = []
    sink_id = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        last = 0.0
        last = worker._report_if_due(last, 1800.0)  # 到期 → 打第 1 行
        last = worker._report_if_due(last, 1800.0)  # 未到期（now==last）→ 不打
        worker._poll_count += 60
        last = worker._report_if_due(last, 1800.0)  # 到期 → 打第 2 行
    finally:
        logger.remove(sink_id)

    lines = [ln for ln in captured if "background worker idle" in ln]
    assert len(lines) == 2, captured
    assert "60 polls / 0 claims in 1800s" in lines[0], lines[0]
    assert "cumulative 60 / 0" in lines[0], lines[0]
    assert "60 polls / 0 claims in 1800s" in lines[1], lines[1]
    assert "cumulative 120 / 0" in lines[1], lines[1]
    assert last == 3600.0
