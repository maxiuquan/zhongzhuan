"""T23 tests: execution budget, loop signatures and the circuit breaker.

Acceptance criteria -> test functions:

* ① R-P0-27, one assertion per ceiling, each pinning the ``TerminalReason``:
  :func:`test_max_tool_rounds_tripped`,
  :func:`test_max_calls_per_tool_tripped`,
  :func:`test_max_identical_call_repeats_tripped`,
  :func:`test_max_total_tool_calls_tripped`,
  :func:`test_max_output_budget_tripped`,
  :func:`test_max_wall_time_tripped`.
* ② R-P0-28, signature normalisation and no-progress detection:
  :func:`test_identical_call_3x_trips`,
  :func:`test_json_key_order_ignored`,
  :func:`test_whitespace_ignored`,
  :func:`test_call_id_ignored`,
  :func:`test_identical_failure_no_progress`.
* ③ R-P0-32, the ten reasons and the six-step teardown:
  :func:`test_all_ten_terminal_reasons_present`,
  :func:`test_circuit_breaker_trip_emits_terminal_once`,
  :func:`test_circuit_breaker_six_steps_order`.
* ⑥ Q8, no unlimited wall clock:
  :func:`test_zero_wall_time_rejected`,
  :func:`test_budget_profiles_wall_time`.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from zhongzhuan.proxy.protocol.responses_emitter import ResponsesEventEmitter
from zhongzhuan.proxy.protocol.responses_models import (
    CIRCUIT_BREAKER_REASONS,
    TerminalReason,
)
from zhongzhuan.proxy.protocol.turn_accumulator import TurnAccumulator
from zhongzhuan.responses_v3.budget import (
    BACKGROUND_BUDGET,
    SYNC_BUDGET,
    TRIP_STEPS,
    BudgetLedger,
    CircuitBreaker,
    ExecutionBudget,
    tool_signature,
)

#: The ten circuit-breaker reasons of §9.4 / R-P0-32, spelled out here on
#: purpose: the test must fail if the production enum ever loses one.
TEN_CB_REASONS: tuple[str, ...] = (
    "max_tool_rounds",
    "max_total_tool_calls",
    "repeated_tool_call",
    "repeated_tool_failure",
    "max_response_time",
    "max_output_budget",
    "response_chain_cycle",
    "response_chain_too_deep",
    "retry_budget_exhausted",
    "background_budget_exhausted",
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakePipeline:
    """Records that the breaker stopped the upstream and the scheduler."""

    def __init__(self, *, fail_cancel: bool = False) -> None:
        self.cancelled = 0
        self.stopped = 0
        self._fail_cancel = fail_cancel

    async def cancel_upstream(self) -> None:
        self.cancelled += 1
        if self._fail_cancel:
            raise RuntimeError("upstream socket already gone")

    def stop_scheduling(self) -> None:
        self.stopped += 1


class FakeStore:
    """Audit sink stand-in (``ResponseStore.record_audit`` lands in T24)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_audit(self, event: dict) -> None:
        self.events.append(event)


class FakeCtx:
    """Only the two identity fields the breaker reads (see §3.10 note)."""

    def __init__(self, request_id: str = "req_1", workspace_id: str = "ws_1") -> None:
        self.request_id = request_id
        self.workspace_id = workspace_id


def make_emitter(response_id: str = "resp_cb") -> ResponsesEventEmitter:
    """A started emitter, i.e. one in the state a live stream would be in."""
    emitter = ResponsesEventEmitter(response_id=response_id, model="m")
    emitter.start()
    return emitter


def terminal_event(frames: list[bytes]) -> dict:
    """Parse the single terminal ``response.*`` event out of ``frames``."""
    events = []
    for frame in frames:
        text = frame.decode("utf-8")
        if "data: [DONE]" in text:
            continue
        payload = json.loads(text.split("data: ", 1)[1].strip())
        if str(payload.get("type", "")).startswith("response.") and "response" in payload:
            events.append(payload)
    assert len(events) == 1, f"expected exactly one terminal event, got {len(events)}"
    return events[0]


def ledger(**overrides) -> BudgetLedger:
    return BudgetLedger(budget=ExecutionBudget(**overrides))


# ---------------------------------------------------------------------------
# ① R-P0-27 -- the six ceilings
# ---------------------------------------------------------------------------


def test_max_tool_rounds_tripped():
    """32 rounds are free; the 33rd is ``MAX_TOOL_ROUNDS``."""
    led = ledger()
    for i in range(SYNC_BUDGET.max_tool_rounds):
        assert led.charge_round() is None, f"round {i + 1} should be within budget"
    assert led.charge_round() is TerminalReason.MAX_TOOL_ROUNDS


def test_max_calls_per_tool_tripped():
    """Nine calls to one tool trip ``REPEATED_TOOL_CALL`` even with new args."""
    led = ledger()
    for i in range(SYNC_BUDGET.max_calls_per_tool):
        assert led.charge_tool_call("read_file", '{"path":"f%d"}' % i) is None
    reason = led.charge_tool_call("read_file", '{"path":"f8"}')
    assert reason is TerminalReason.REPEATED_TOOL_CALL
    assert led.calls_per_tool["read_file"] == 9


def test_max_identical_call_repeats_tripped():
    """The same tool + the same arguments three times is a stuck loop."""
    led = ledger()
    args = '{"path":"/etc/hosts"}'
    assert led.charge_tool_call("read_file", args) is None
    assert led.charge_tool_call("read_file", args) is None
    assert led.charge_tool_call("read_file", args) is TerminalReason.REPEATED_TOOL_CALL


def test_max_total_tool_calls_tripped():
    """65 distinct tools trip the global total, not the per-tool ceiling."""
    led = ledger()
    for i in range(SYNC_BUDGET.max_total_tool_calls):
        assert led.charge_tool_call(f"tool_{i}", "{}") is None
    assert led.charge_tool_call("tool_64", "{}") is TerminalReason.MAX_TOTAL_TOOL_CALLS


def test_max_output_budget_tripped():
    """Crossing 200k output tokens is ``MAX_OUTPUT_BUDGET``."""
    led = ledger()
    assert led.charge_output_tokens(199_999) is None
    assert led.charge_output_tokens(1) is None, "exactly at the ceiling is allowed"
    assert led.charge_output_tokens(1) is TerminalReason.MAX_OUTPUT_BUDGET
    assert led.output_tokens == 200_001


def test_max_wall_time_tripped():
    """901s past ``started_at`` is ``MAX_RESPONSE_TIME`` (900s ceiling)."""
    led = ledger()
    led.started_at = 1_000.0
    assert led.check_wall_time(now=1_000.0 + 900) is None
    assert led.check_wall_time(now=1_000.0 + 901) is TerminalReason.MAX_RESPONSE_TIME


def test_upstream_switch_budget_tripped():
    """Two pre-first-byte switches are free; the third exhausts the budget."""
    led = ledger()
    assert led.charge_upstream_switch() is None
    assert led.charge_upstream_switch() is None
    assert led.charge_upstream_switch() is TerminalReason.RETRY_BUDGET_EXHAUSTED


# ---------------------------------------------------------------------------
# ② R-P0-28 -- signature normalisation / no progress
# ---------------------------------------------------------------------------


def test_identical_call_3x_trips():
    """The R-P0-28 headline case, phrased as the acceptance criterion does."""
    led = ledger()
    reasons = [led.charge_tool_call("search", '{"q":"kubernetes"}') for _ in range(3)]
    assert reasons == [None, None, TerminalReason.REPEATED_TOOL_CALL]


def test_json_key_order_ignored():
    """Key order carries no meaning in JSON, so it must not change identity."""
    assert tool_signature("f", '{"a":1,"b":2}') == tool_signature("f", '{"b":2,"a":1}')


def test_whitespace_ignored():
    """Insignificant whitespace must not change identity."""
    assert tool_signature("f", '{"a": 1}') == tool_signature("f", '{"a":1}')
    assert tool_signature("f", '  {\n  "a" : 1\n}  ') == tool_signature("f", '{"a":1}')


def test_call_id_ignored():
    """``call_id`` is regenerated every round; it cannot be part of identity."""
    assert tool_signature("f", '{"call_id":"x","a":1}') == tool_signature("f", '{"a":1}')
    assert tool_signature("f", '{"call_id":"y","a":1}') == tool_signature("f", '{"a":1}')


def test_signature_still_separates_real_differences():
    """Normalisation must not over-collapse: real changes stay distinct."""
    assert tool_signature("f", '{"a":1}') != tool_signature("f", '{"a":2}')
    assert tool_signature("f", '{"a":1}') != tool_signature("g", '{"a":1}')
    assert tool_signature("f", '{"a":1}', "ok") != tool_signature("f", '{"a":1}', "boom")


def test_empty_arguments_normalize_to_empty_object():
    """``""`` / ``None`` / ``"{}"`` are the same "no arguments" call."""
    assert tool_signature("f", "") == tool_signature("f", "{}")
    assert tool_signature("f", None) == tool_signature("f", "{}")
    assert tool_signature("f", "   ") == tool_signature("f", "{}")


def test_identical_failure_no_progress():
    """Same call + same failure three times is definitionally no progress."""
    led = ledger()
    sig = tool_signature("read_file", '{"path":"/nope"}', "ENOENT: no such file")
    assert led.charge_tool_result(sig, failed=True) is None
    assert led.charge_tool_result(sig, failed=True) is None
    assert led.charge_tool_result(sig, failed=True) is TerminalReason.REPEATED_TOOL_FAILURE


def test_successful_results_are_free():
    """Only *failures* accumulate -- a succeeding loop is making progress."""
    led = ledger()
    sig = tool_signature("read_file", '{"path":"/ok"}', "contents")
    for _ in range(10):
        assert led.charge_tool_result(sig, failed=False) is None
    assert led.failure_signatures == {}


# ---------------------------------------------------------------------------
# ③ R-P0-32 -- ten reasons + the six-step trip
# ---------------------------------------------------------------------------


def test_all_ten_terminal_reasons_present():
    """The closed set of ten circuit-breaker reasons must stay complete."""
    values = {member.value for member in TerminalReason}
    for reason in TEN_CB_REASONS:
        assert reason in values, f"TerminalReason lost {reason}"
    assert {r.value for r in CIRCUIT_BREAKER_REASONS} == set(TEN_CB_REASONS)


@pytest.mark.parametrize("reason_value", TEN_CB_REASONS)
async def test_circuit_breaker_trip_emits_terminal_once(reason_value):
    """Each of the ten reasons produces one terminal event + one ``[DONE]``."""
    reason = TerminalReason(reason_value)
    breaker = CircuitBreaker()
    pipeline = FakePipeline()
    store = FakeStore()
    emitter = make_emitter()

    # One open item, so step 4 has real work to do.
    turn = TurnAccumulator(response_id="resp_cb")
    msg = turn.new_message()
    msg.mark_added()
    emitter.open_item(turn.open_items()[0])

    frames = await breaker.trip(reason, FakeCtx(), pipeline, emitter, turn, store)

    assert breaker.last_steps == list(TRIP_STEPS)
    assert breaker.step_errors == []
    assert pipeline.cancelled == 1 and pipeline.stopped == 1
    assert emitter.done is True

    event = terminal_event(frames)
    assert event["type"] == "response.incomplete"
    assert event["response"]["status"] == "incomplete"
    assert event["response"]["incomplete_details"]["reason"] == reason_value
    assert event["response"]["terminal_reason"] == reason_value
    # Terminal frame is the response.incomplete event (Responses API has no [DONE] sentinel).
    assert b"response.incomplete" in frames[-1]
    assert b"data: [DONE]" not in frames[-1]
    assert sum(1 for f in frames if f == b"data: [DONE]\n\n") == 0  # Responses API has no [DONE] sentinel
    assert any(e["event"] == "circuit_breaker.trip" for e in store.events)


async def test_circuit_breaker_six_steps_order():
    """The teardown order is contractual, not incidental (§3.10)."""
    breaker = CircuitBreaker()
    await breaker.trip(
        TerminalReason.MAX_TOOL_ROUNDS,
        FakeCtx(),
        FakePipeline(),
        make_emitter(),
        TurnAccumulator(response_id="resp_cb"),
        FakeStore(),
    )
    assert breaker.last_steps == [
        "stop_upstream_read",
        "stop_scheduling",
        "rollback_side_effects",
        "close_open_items",
        "emit_terminal",
        "write_audit",
    ]


async def test_trip_reaches_terminal_even_if_early_step_raises():
    """铁律: a broken teardown step must never swallow the terminal event."""
    breaker = CircuitBreaker()
    emitter = make_emitter()
    frames = await breaker.trip(
        TerminalReason.MAX_RESPONSE_TIME,
        FakeCtx(),
        FakePipeline(fail_cancel=True),
        emitter,
        TurnAccumulator(response_id="resp_cb"),
        FakeStore(),
    )
    assert breaker.last_steps == list(TRIP_STEPS)
    assert breaker.step_errors and "stop_upstream_read" in breaker.step_errors[0]
    assert emitter.done is True
    assert terminal_event(frames)["response"]["incomplete_details"]["reason"] == "max_response_time"


async def test_second_trip_on_same_emitter_is_a_safe_noop():
    """Exactly-once: a re-trip must not write a second terminal event."""
    breaker = CircuitBreaker()
    emitter = make_emitter()
    turn = TurnAccumulator(response_id="resp_cb")
    first = await breaker.trip(
        TerminalReason.MAX_OUTPUT_BUDGET,
        FakeCtx(),
        FakePipeline(),
        emitter,
        turn,
        FakeStore(),
    )
    second = await breaker.trip(
        TerminalReason.MAX_OUTPUT_BUDGET,
        FakeCtx(),
        FakePipeline(),
        emitter,
        turn,
        FakeStore(),
    )
    assert terminal_event(first)["response"]["status"] == "incomplete"
    assert second == [], "the emitter latch must suppress the duplicate terminal"
    assert breaker.last_steps == list(TRIP_STEPS)


async def test_async_audit_sink_is_not_awaited_inline():
    """A slow audit sink must not sit between the trip and the client's frame."""
    import asyncio

    written: list[dict] = []

    class AsyncStore:
        async def record_audit(self, event: dict) -> None:
            written.append(event)

    breaker = CircuitBreaker()
    emitter = make_emitter()
    frames = await breaker.trip(
        TerminalReason.RESPONSE_CHAIN_CYCLE,
        FakeCtx(),
        FakePipeline(),
        emitter,
        TurnAccumulator(response_id="r"),
        AsyncStore(),
    )
    assert emitter.done is True and breaker.step_errors == []
    assert terminal_event(frames)["response"]["incomplete_details"]["reason"] == "response_chain_cycle"
    await asyncio.sleep(0)  # let the fire-and-forget audit tasks run
    assert {e["event"] for e in written} == {
        "circuit_breaker.rollback",
        "circuit_breaker.trip",
    }


async def test_broken_audit_sink_does_not_break_the_trip():
    """Audit is best-effort: a raising sink must not lose the terminal event."""

    class ExplodingStore:
        def record_audit(self, event: dict) -> None:
            raise RuntimeError("audit db is down")

    breaker = CircuitBreaker()
    emitter = make_emitter()
    frames = await breaker.trip(
        TerminalReason.REPEATED_TOOL_FAILURE,
        FakeCtx(),
        FakePipeline(),
        emitter,
        TurnAccumulator(response_id="r"),
        ExplodingStore(),
    )
    assert breaker.last_steps == list(TRIP_STEPS)
    assert breaker.step_errors == [], "audit failures are swallowed, not step errors"
    assert terminal_event(frames)["response"]["incomplete_details"]["reason"] == "repeated_tool_failure"


async def test_trip_tolerates_a_pipeline_without_hooks():
    """A collaborator that predates the protocol must not break a teardown."""
    breaker = CircuitBreaker()
    emitter = make_emitter()
    frames = await breaker.trip(
        TerminalReason.BACKGROUND_BUDGET_EXHAUSTED,
        FakeCtx(),
        object(),
        emitter,
        TurnAccumulator(response_id="r"),
        object(),
    )
    assert breaker.step_errors == []
    assert emitter.done is True
    assert terminal_event(frames)["response"]["incomplete_details"]["reason"] == "background_budget_exhausted"


async def test_close_open_items_closes_every_open_item():
    """Every announced item is closed before the terminal event."""
    breaker = CircuitBreaker()
    emitter = make_emitter()
    turn = TurnAccumulator(response_id="resp_cb")
    msg = turn.new_message()
    msg.mark_added()
    tool = turn.open_tool_call(call_id="call_1", source_index=0, name="search")
    tool.item_added = True
    opened = turn.open_items()
    assert len(opened) == 2
    for item in opened:
        emitter.open_item(item)

    frames = await breaker.trip(
        TerminalReason.REPEATED_TOOL_CALL,
        FakeCtx(),
        FakePipeline(),
        emitter,
        turn,
        FakeStore(),
    )
    done_frames = [f for f in frames if b"response.output_item.done" in f]
    assert len(done_frames) == 2
    assert emitter.stats.items_done == 2


def test_open_items_excludes_never_added_and_already_done():
    """ "Open" is *added and not done* -- closing anything else is malformed."""
    turn = TurnAccumulator(response_id="resp_cb")
    never_added = turn.new_message()
    already_done = turn.new_message()
    already_done.mark_added()
    already_done.mark_done()
    live = turn.new_message()
    live.mark_added()

    ids = [item.id for item in turn.open_items()]
    assert ids == [live.item_id]
    assert never_added.item_id not in ids and already_done.item_id not in ids


# ---------------------------------------------------------------------------
# ⑥ Q8 -- no unlimited wall clock
# ---------------------------------------------------------------------------


def test_zero_wall_time_rejected():
    """``0`` / ``None`` would disable the very guard this module exists for."""
    with pytest.raises(ValueError):
        ExecutionBudget(max_wall_time_seconds=0)
    with pytest.raises(ValueError):
        ExecutionBudget(max_wall_time_seconds=None)
    with pytest.raises(ValueError):
        ExecutionBudget(max_wall_time_seconds=-1)


def test_budget_profiles_wall_time():
    """The two shipped profiles differ only in their wall-clock ceiling."""
    assert SYNC_BUDGET.max_wall_time_seconds == 900
    assert BACKGROUND_BUDGET.max_wall_time_seconds == 3600
    assert ExecutionBudget.SYNC_BUDGET is SYNC_BUDGET
    assert ExecutionBudget.BACKGROUND_BUDGET is BACKGROUND_BUDGET


def test_budget_defaults_match_the_spec():
    """§3.10's eight documented defaults, pinned."""
    b = ExecutionBudget()
    assert (b.max_tool_rounds, b.max_calls_per_tool, b.max_identical_call_repeats) == (32, 8, 2)
    assert (b.max_total_tool_calls, b.max_output_tokens_total) == (64, 200_000)
    assert (b.max_chain_depth, b.max_upstream_switches) == (64, 2)


def test_budget_is_immutable():
    """A per-request policy that a handler could mutate is not a policy."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        SYNC_BUDGET.max_tool_rounds = 999  # type: ignore[misc]
