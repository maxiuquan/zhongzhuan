"""T11 tests: ToolCallAccumulator + TurnAccumulator + OutputIndexAllocator.

Covers the acceptance criteria of T11 (§5.3 / §5.4 / §9.3 / 铁律 2):
1. Tool name/arguments any fragmentation; call id late-binding or absent.
2. Parallel tool calls interleave without cross-contamination.
3. Arguments never "completed" early just because they are valid JSON.
4. Global output index is unique and never reused.
5. Reasoning is ephemeral (released at turn end).
6. Duplicate chunks / late chunks never double-complete.
"""
from __future__ import annotations

import pytest

from zhongzhuan.proxy.protocol.tool_accumulator import ToolCallAccumulator, ToolCallCollection
from zhongzhuan.proxy.protocol.turn_accumulator import (
    EphemeralReasoningAccumulator,
    OutputIndexAllocator,
    TurnAccumulator,
)


# ---------------------------------------------------------------------------
# 1. Output index allocator
# ---------------------------------------------------------------------------


def test_output_index_allocator_monotonic():
    a = OutputIndexAllocator()
    assert a.next() == 0
    assert a.next() == 1
    assert a.next() == 2
    assert a.peek() == 3


def test_output_index_never_reused():
    a = OutputIndexAllocator(start=5)
    seen = {a.next() for _ in range(100)}
    assert len(seen) == 100
    assert 5 in seen  # start is the first allocated index
    assert min(seen) == 5


# ---------------------------------------------------------------------------
# 2. Tool accumulation: fragmentation
# ---------------------------------------------------------------------------


def test_arguments_fragments_append_verbatim():
    acc = ToolCallAccumulator(source_index=0, output_index=0)
    acc.replace_name("get_weather")
    acc.append_arguments('{"loc')
    acc.append_arguments('ation": "HK"}')
    assert acc.arguments == '{"location": "HK"}'
    assert acc.validate_arguments() is True
    assert acc.is_complete()


def test_arguments_never_complete_early():
    """A valid JSON prefix must not be treated as complete before the end."""
    acc = ToolCallAccumulator(source_index=0, output_index=0)
    acc.replace_name("get_weather")
    acc.append_arguments('{"a": 1}')  # valid JSON, but stream may continue
    # Not finished until validate_arguments() is called at the close signal.
    assert acc.arguments_done is False
    assert acc.is_complete() is False


def test_invalid_arguments_never_runnable():
    acc = ToolCallAccumulator(source_index=0, output_index=0)
    acc.replace_name("get_weather")
    acc.append_arguments('{"broken')
    assert acc.validate_arguments() is False
    assert acc.is_complete() is False
    assert acc.item_done is False


def test_name_fragment_vs_replace_modes():
    # append mode (chunked names)
    a = ToolCallAccumulator(source_index=0, output_index=0, name_mode="append")
    a.append_name("get_")
    a.append_name("weather")
    assert a.name == "get_weather"
    # replace mode (repeated full names)
    b = ToolCallAccumulator(source_index=1, output_index=1)
    b.replace_name("get_weather")
    b.replace_name("get_weather")
    assert b.name == "get_weather"


def test_call_id_late_binding():
    coll = ToolCallCollection(response_id="resp_x")
    acc = coll.ensure(output_index=0, source_index=2)
    assert acc.call_id == "call_resp_x_2"  # synthetic until bound
    coll.finalize_call_id("call_abc", source_index=2)
    assert acc.call_id == "call_abc"
    assert coll.tools_by_call_id["call_abc"] is acc


def test_matching_priority_call_id_first():
    coll = ToolCallCollection(response_id="resp_x")
    # index 0 established first
    acc0 = coll.ensure(output_index=0, source_index=0)
    # a fragment arrives with call_id that maps to index 0
    got = coll.get(call_id=acc0.call_id, source_index=0)
    assert got is acc0


# ---------------------------------------------------------------------------
# 3. Parallel tool calls: no cross-contamination
# ---------------------------------------------------------------------------


def test_parallel_tool_calls_interleave():
    coll = ToolCallCollection(response_id="resp_x")
    a = coll.ensure(output_index=0, source_index=0)
    b = coll.ensure(output_index=1, source_index=1)
    a.replace_name("f_a")
    b.replace_name("f_b")
    a.append_arguments('{"x": ')
    b.append_arguments('{"y": ')
    a.append_arguments("1}")
    b.append_arguments("2}")
    assert a.arguments == '{"x": 1}'
    assert b.arguments == '{"y": 2}'
    assert a.name == "f_a"
    assert b.name == "f_b"
    assert a.validate_arguments() and b.validate_arguments()
    assert len(coll.completed()) == 2


def test_duplicate_finish_is_idempotent():
    a = ToolCallAccumulator(source_index=0, output_index=0)
    a.replace_name("f")
    a.append_arguments("{}")
    assert a.validate_arguments() is True
    a.mark_item_done()
    a.mark_item_done()  # second call must not error
    assert a.item_done is True
    assert a.arguments_done is True


# ---------------------------------------------------------------------------
# 4. Turn accumulator: output index uniqueness + item ids
# ---------------------------------------------------------------------------


def test_turn_accumulator_allocates_unique_output_indices():
    t = TurnAccumulator(response_id="resp_1")
    m1 = t.new_message()
    m2 = t.new_message()
    r = t.open_reasoning()
    fc = t.open_tool_call(source_index=0)
    indices = [m1.output_index, m2.output_index, r.output_index, fc.output_index]
    assert len(set(indices)) == len(indices)  # all unique
    assert m1.item_id == "msg_resp_1_0"
    assert r.item_id == "rs_resp_1_2"


def test_turn_accumulator_release_clears_reasoning():
    t = TurnAccumulator(response_id="resp_1")
    r = t.open_reasoning()
    r.append("SECRET")
    assert t.reasoning is not None
    t.release()
    assert t.reasoning is None
    assert r.text == ""  # ephemeral released


def test_reasoning_never_in_all_items_after_release():
    t = TurnAccumulator(response_id="resp_1")
    t.open_reasoning()
    t.new_message()
    t.release()
    items = t.all_items()
    assert all(not isinstance(i, EphemeralReasoningAccumulator) for i in items)


# ---------------------------------------------------------------------------
# 5. ToolCallCollection helper behaviour
# ---------------------------------------------------------------------------


def test_collection_incomplete_lists_truncated_calls():
    coll = ToolCallCollection(response_id="resp_x")
    ok = coll.ensure(output_index=0, source_index=0)
    ok.replace_name("fine")
    ok.append_arguments("{}")
    assert ok.validate_arguments() is True

    bad = coll.ensure(output_index=1, source_index=1)
    bad.replace_name("broken")
    bad.append_arguments("{nope")

    assert len(coll.completed()) == 1
    assert len(coll.incomplete()) == 1
    assert coll.incomplete()[0] is bad


def test_signature_stable():
    a = ToolCallAccumulator(source_index=0, output_index=0)
    a.replace_name("f")
    a.append_arguments('{"b":1,"a":2}')
    b = ToolCallAccumulator(source_index=1, output_index=1)
    b.replace_name("f")
    b.append_arguments('{"a":2,"b":1}')  # same content, different key order
    assert a.signature() == b.signature()