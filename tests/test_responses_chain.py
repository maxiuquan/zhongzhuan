"""T22 tests: state-chain recovery + cycle guard (R-P0-29 / R-P1-31 / R-P1-40).

Acceptance criteria, one assertion block each:

* ① self-reference / A->B->A / A->B->C->A / 65-deep chain / cross-tenant parent
  each return a **standard Responses error**, never a stateless downgrade:
  :func:`test_self_reference_returns_standard_error`,
  :func:`test_two_node_cycle_returns_standard_error`,
  :func:`test_three_node_cycle_returns_standard_error`,
  :func:`test_chain_deeper_than_max_depth_returns_standard_error`,
  :func:`test_cross_tenant_parent_returns_standard_error`.
* ② three turns -> the third upstream payload carries the first turn's user
  text and no reasoning at all:
  :func:`test_third_turn_payload_has_first_user_text_and_no_reasoning`.
* ③ ``instructions`` are not inherited:
  :func:`test_instructions_are_not_inherited`.
* ④ a deleted parent yields a standard error:
  :func:`test_deleted_parent_returns_standard_error`.
* ⑤ ``retrieve`` keeps the reasoning placeholder while the next upstream
  payload has none of its text:
  :func:`test_retrieve_keeps_reasoning_placeholder_but_upstream_drops_text`.
"""

from __future__ import annotations

import json

import pytest

from zhongzhuan.config import default_config
from zhongzhuan.proxy.protocol.item_registry import redact_item
from zhongzhuan.proxy.protocol.responses import (
    convert_responses_request_to_chatcompletions,
)
from zhongzhuan.proxy.protocol.responses_models import ErrorClass, TerminalReason
from zhongzhuan.responses_v3.chain import (
    DEFAULT_MAX_CHAIN_DEPTH,
    ChainResolver,
    build_upstream_input,
)
from zhongzhuan.responses_v3.handler import ResponsesV3Handler
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store

#: Text that must never survive into an upstream payload (铁律 1 / R-P1-40).
SECRET_COT = "STEP-BY-STEP-CHAIN-OF-THOUGHT-DO-NOT-REPLAY"


@pytest.fixture
async def env(tmp_path):
    cfg = default_config()
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.storage.db_path = cfg.storage.sqlite_db_path
    cfg.tidb = None
    store = await create_store(cfg)
    rs = ResponseStore(store)
    handler = ResponsesV3Handler(rs)
    yield rs, handler, ChainResolver(rs)
    await store.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def assert_standard_responses_error(status: int, body: dict) -> None:
    """A guard trip must look exactly like an official Responses error object."""
    assert status == 400, f"expected a 4xx client error, got {status}"
    assert "error" in body, f"not a Responses error envelope: {body}"
    err = body["error"]
    assert err["type"] == "invalid_request_error"
    assert err["code"] == "invalid_request"
    assert err["param"] == "previous_response_id"
    assert isinstance(err["message"], str) and err["message"]
    # Not degraded to a stateless response object.
    assert "id" not in body and "object" not in body


def reasoning_placeholder(item_id: str) -> dict:
    """A reasoning item as it is allowed to be persisted (metadata only)."""
    return redact_item(
        {
            "id": item_id,
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": SECRET_COT}],
            "content": [{"type": "reasoning_text", "text": SECRET_COT}],
            "encrypted_content": SECRET_COT,
        }
    )


def assistant_message(text: str) -> dict:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


async def seed_response(
    rs: ResponseStore,
    response_id: str,
    *,
    workspace_id: str = "t1",
    previous_response_id: str = "",
    request: dict | None = None,
) -> None:
    await rs.create_response(
        response_id=response_id,
        workspace_id=workspace_id,
        model="gpt-4o",
        status="completed",
        previous_response_id=previous_response_id,
        request=request or {},
    )


async def turn(
    handler: ResponsesV3Handler,
    rs: ResponseStore,
    *,
    text: str,
    previous_response_id: str = "",
    instructions: str | None = None,
    answer: str = "",
    workspace_id: str = "t1",
) -> tuple[int, dict]:
    """Run one create + persist a (reasoning, assistant) output pair."""
    body: dict = {"model": "gpt-4o", "input": text}
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    if instructions is not None:
        body["instructions"] = instructions
    status, created = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id=workspace_id,
        body=body,
    )
    if status == 200 and answer:
        await rs.update_status(
            created["id"],
            "completed",
            output=[reasoning_placeholder("rs_" + created["id"]), assistant_message(answer)],
        )
    return status, created


# ---------------------------------------------------------------------------
# ① cycle / depth / tenant guards -> standard Responses error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_reference_returns_standard_error(env):
    """① a response pointing at itself is rejected, not silently ignored."""
    rs, handler, resolver = env
    await seed_response(rs, "resp_self", previous_response_id="resp_self")

    resolution = await resolver.resolve_chain("resp_self", "t1")
    assert resolution.error is ErrorClass.INVALID_CLIENT_REQUEST
    assert resolution.terminal_reason is TerminalReason.RESPONSE_CHAIN_CYCLE
    assert resolution.items == []

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi", "previous_response_id": "resp_self"},
    )
    assert_standard_responses_error(status, body)
    assert "response_chain_cycle" in body["error"]["message"]


@pytest.mark.asyncio
async def test_two_node_cycle_returns_standard_error(env):
    """① A -> B -> A."""
    rs, handler, resolver = env
    await seed_response(rs, "resp_a", previous_response_id="resp_b")
    await seed_response(rs, "resp_b", previous_response_id="resp_a")

    resolution = await resolver.resolve_chain("resp_a", "t1")
    assert resolution.terminal_reason is TerminalReason.RESPONSE_CHAIN_CYCLE
    assert resolution.visited == ["resp_a", "resp_b"]

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi", "previous_response_id": "resp_a"},
    )
    assert_standard_responses_error(status, body)


@pytest.mark.asyncio
async def test_three_node_cycle_returns_standard_error(env):
    """① A -> B -> C -> A."""
    rs, handler, resolver = env
    await seed_response(rs, "resp_a", previous_response_id="resp_b")
    await seed_response(rs, "resp_b", previous_response_id="resp_c")
    await seed_response(rs, "resp_c", previous_response_id="resp_a")

    resolution = await resolver.resolve_chain("resp_a", "t1")
    assert resolution.terminal_reason is TerminalReason.RESPONSE_CHAIN_CYCLE
    assert resolution.visited == ["resp_a", "resp_b", "resp_c"]

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi", "previous_response_id": "resp_a"},
    )
    assert_standard_responses_error(status, body)


@pytest.mark.asyncio
async def test_chain_deeper_than_max_depth_returns_standard_error(env):
    """① 65 stored ancestors exceed the default depth of 64; 64 still resolve."""
    rs, handler, resolver = env
    total = DEFAULT_MAX_CHAIN_DEPTH + 1  # 65 nodes: resp_0 (root) .. resp_64
    for i in range(total):
        await seed_response(
            rs,
            f"resp_{i}",
            previous_response_id=(f"resp_{i - 1}" if i else ""),
        )

    ok = await resolver.resolve_chain(f"resp_{DEFAULT_MAX_CHAIN_DEPTH - 1}", "t1")
    assert ok.ok and ok.depth == DEFAULT_MAX_CHAIN_DEPTH

    too_deep = await resolver.resolve_chain(f"resp_{total - 1}", "t1")
    assert too_deep.error is ErrorClass.INVALID_CLIENT_REQUEST
    assert too_deep.terminal_reason is TerminalReason.RESPONSE_CHAIN_TOO_DEEP
    assert too_deep.items == []

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi", "previous_response_id": f"resp_{total - 1}"},
    )
    assert_standard_responses_error(status, body)
    assert "response_chain_too_deep" in body["error"]["message"]


@pytest.mark.asyncio
async def test_cross_tenant_parent_returns_standard_error(env):
    """① t2 may not chain onto a response owned by t1."""
    rs, handler, resolver = env
    _, created = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "tenant one"},
    )
    parent = created["id"]

    assert (await resolver.resolve_chain(parent, "t1")).ok
    foreign = await resolver.resolve_chain(parent, "t2")
    assert foreign.error is ErrorClass.INVALID_CLIENT_REQUEST
    assert foreign.items == []

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t2",
        body={"model": "gpt-4o", "input": "hi", "previous_response_id": parent},
    )
    assert_standard_responses_error(status, body)
    # No cross-tenant existence oracle: same wording as a plain miss.
    assert "not found" in body["error"]["message"]


# ---------------------------------------------------------------------------
# ② multi-turn recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_third_turn_payload_has_first_user_text_and_no_reasoning(env):
    """② turn 3 sees turn 1's user text and zero reasoning (R-P1-31 / 铁律 1)."""
    rs, handler, resolver = env
    status1, r1 = await turn(handler, rs, text="What is the capital of France?", answer="Paris.")
    assert status1 == 200
    status2, r2 = await turn(handler, rs, text="And Germany?", previous_response_id=r1["id"], answer="Berlin.")
    assert status2 == 200
    status3, r3 = await turn(handler, rs, text="And Italy?", previous_response_id=r2["id"])
    assert status3 == 200
    assert r3["previous_response_id"] == r2["id"]

    resolution = await resolver.resolve_chain(r2["id"], "t1")
    assert resolution.ok
    assert resolution.depth == 2
    assert resolution.visited == [r2["id"], r1["id"]]

    wire_input = build_upstream_input(resolution, "And Italy?")
    # The Chat converter silently ignores unknown item types, so assert on the
    # Responses-level array too -- otherwise a leak would hide behind it.
    assert all(it.get("type") != "reasoning" for it in wire_input)
    assert SECRET_COT not in json.dumps(wire_input, ensure_ascii=False)
    upstream = convert_responses_request_to_chatcompletions({"model": "gpt-4o", "input": wire_input})
    blob = json.dumps(upstream, ensure_ascii=False)

    # First turn survived, in chronological order, together with its answer.
    assert "What is the capital of France?" in blob
    assert "Paris." in blob and "Berlin." in blob
    assert "And Italy?" in blob
    texts = [m["content"] for m in upstream["messages"]]
    flat = json.dumps(texts, ensure_ascii=False)
    assert flat.index("capital of France") < flat.index("And Germany?")
    assert flat.index("And Germany?") < flat.index("And Italy?")

    # ... and not a single reasoning item or byte of reasoning text.
    assert SECRET_COT not in blob
    assert "reasoning" not in blob
    assert all(it.item_type != "reasoning" for it in resolution.items)


# ---------------------------------------------------------------------------
# ③ instructions are per-request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_instructions_are_not_inherited(env):
    """③ a child never picks up its parent's ``instructions`` (R-P1-31)."""
    rs, handler, resolver = env
    _, parent = await turn(handler, rs, text="ahoy", instructions="You are a pirate. Always say ARRR.", answer="ARRR.")
    status, child = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hello", "previous_response_id": parent["id"]},
    )
    assert status == 200
    assert child["instructions"] is None

    resolution = await resolver.resolve_chain(parent["id"], "t1")
    upstream = convert_responses_request_to_chatcompletions(
        {"model": "gpt-4o", "input": build_upstream_input(resolution, "hello")}
    )
    blob = json.dumps(upstream, ensure_ascii=False)
    assert "You are a pirate" not in blob
    assert not any(m.get("role") == "system" for m in upstream["messages"])
    # The parent's own instructions are still readable on the parent itself.
    assert (
        (await rs.get_response(parent["id"], workspace_id="t1")).request["instructions"].startswith("You are a pirate")
    )


# ---------------------------------------------------------------------------
# ④ deleted parent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleted_parent_returns_standard_error(env):
    """④ deleting the parent invalidates the chain with a standard error."""
    rs, handler, resolver = env
    _, parent = await turn(handler, rs, text="round one", answer="ok")
    assert (await resolver.resolve_chain(parent["id"], "t1")).ok

    del_status, _ = await handler.dispatch(
        "DELETE",
        f"/v1/responses/{parent['id']}",
        workspace_id="t1",
    )
    assert del_status == 200

    gone = await resolver.resolve_chain(parent["id"], "t1")
    assert gone.error is ErrorClass.INVALID_CLIENT_REQUEST
    assert gone.terminal_reason is None
    assert gone.items == []

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "round two", "previous_response_id": parent["id"]},
    )
    assert_standard_responses_error(status, body)


# ---------------------------------------------------------------------------
# ⑤ reasoning placeholder survives retrieve, its text never goes upstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_keeps_reasoning_placeholder_but_upstream_drops_text(env):
    """⑤ R-P1-40 metadata stays visible; 铁律 1 keeps the text out of the replay."""
    rs, handler, resolver = env
    _, parent = await turn(handler, rs, text="think hard", answer="42")

    status, retrieved = await handler.dispatch(
        "GET",
        f"/v1/responses/{parent['id']}",
        workspace_id="t1",
    )
    assert status == 200
    reasoning = [it for it in retrieved["output"] if it.get("type") == "reasoning"]
    assert len(reasoning) == 1, "the reasoning item must remain for API completeness"
    placeholder = reasoning[0]
    assert placeholder["id"] == "rs_" + parent["id"]
    assert placeholder["status"] == "completed"
    # Metadata only: no summary text, no content, no encrypted blob.
    assert "content" not in placeholder and "encrypted_content" not in placeholder
    assert all("text" not in s for s in placeholder["summary"])
    assert SECRET_COT not in json.dumps(retrieved, ensure_ascii=False)

    resolution = await resolver.resolve_chain(parent["id"], "t1")
    wire_input = build_upstream_input(resolution, "next")
    assert all(it.get("type") != "reasoning" for it in wire_input)
    upstream = convert_responses_request_to_chatcompletions({"model": "gpt-4o", "input": wire_input})
    blob = json.dumps(upstream, ensure_ascii=False)
    assert SECRET_COT not in blob
    assert "reasoning" not in blob
    assert "think hard" in blob and "42" in blob


# ---------------------------------------------------------------------------
# additional guard coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_previous_response_id_is_rejected_by_resolver(env):
    rs, handler, resolver = env
    res = await resolver.resolve_chain("", "t1")
    assert res.error is ErrorClass.INVALID_CLIENT_REQUEST
    assert res.terminal_reason is None
    # An absent previous_response_id is a plain stateless create, not an error.
    status, _ = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi"},
    )
    assert status == 200


@pytest.mark.asyncio
async def test_tenant_ceiling_can_only_narrow_the_per_call_limits(env):
    """R-P0-29: a workspace may tighten max_depth; a request may not widen it."""
    rs, handler, _ = env
    for i in range(5):
        await seed_response(
            rs,
            f"resp_{i}",
            previous_response_id=(f"resp_{i - 1}" if i else ""),
        )
    narrow = ChainResolver(rs, max_depth=3)
    res = await narrow.resolve_chain("resp_4", "t1", max_depth=64)
    assert res.terminal_reason is TerminalReason.RESPONSE_CHAIN_TOO_DEEP
    assert res.depth == 3


@pytest.mark.asyncio
async def test_handler_uses_the_injected_tenant_resolver(env):
    """The narrowed resolver must reach ``create``, not just ``resolve_chain``."""
    rs, _, _ = env
    for i in range(5):
        await seed_response(
            rs,
            f"resp_{i}",
            previous_response_id=(f"resp_{i - 1}" if i else ""),
        )
    handler = ResponsesV3Handler(rs, chain=ChainResolver(rs, max_depth=2))

    via_handler = await handler.resolve_chain("resp_4", workspace_id="t1")
    assert via_handler.terminal_reason is TerminalReason.RESPONSE_CHAIN_TOO_DEEP

    status, body = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={"model": "gpt-4o", "input": "hi", "previous_response_id": "resp_4"},
    )
    assert_standard_responses_error(status, body)
    # The default 64-deep resolver would have accepted this 5-node chain.
    assert (await ChainResolver(rs).resolve_chain("resp_4", "t1")).ok


@pytest.mark.asyncio
async def test_max_items_budget_trips_before_the_depth_guard(env):
    rs, handler, _ = env
    _, parent = await turn(handler, rs, text="one", answer="two")
    resolver = ChainResolver(rs, max_items=1)
    res = await resolver.resolve_chain(parent["id"], "t1")
    assert res.error is ErrorClass.INVALID_CLIENT_REQUEST
    assert res.terminal_reason is TerminalReason.RESPONSE_CHAIN_TOO_DEEP
    assert "items" in res.message


@pytest.mark.asyncio
async def test_max_tokens_budget_trips(env):
    rs, handler, _ = env
    _, parent = await turn(handler, rs, text="x" * 4000, answer="y" * 4000)
    resolver = ChainResolver(rs, max_tokens=10)
    res = await resolver.resolve_chain(parent["id"], "t1")
    assert res.terminal_reason is TerminalReason.RESPONSE_CHAIN_TOO_DEEP
    assert "tokens" in res.message


@pytest.mark.asyncio
async def test_compact_boundary_stops_the_walk(env):
    """A compacted record is the new root: nothing older is recovered."""
    rs, handler, resolver = env
    await seed_response(
        rs,
        "resp_old",
        request={
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ANCIENT-HISTORY"}]}
            ],
        },
    )
    await seed_response(
        rs,
        "resp_sum",
        previous_response_id="resp_old",
        request={
            "compact_boundary": True,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "SUMMARY-SO-FAR"}]}
            ],
        },
    )
    res = await resolver.resolve_chain("resp_sum", "t1")
    assert res.ok and res.depth == 1
    assert res.visited == ["resp_sum"]
    blob = json.dumps(build_upstream_input(res), ensure_ascii=False)
    assert "SUMMARY-SO-FAR" in blob
    assert "ANCIENT-HISTORY" not in blob


@pytest.mark.asyncio
async def test_create_persists_input_items_with_reasoning_redacted(env):
    """铁律 1 on the write path: reasoning text never reaches the store."""
    rs, handler, _ = env
    status, created = await handler.dispatch(
        "POST",
        "/v1/responses",
        workspace_id="t1",
        body={
            "model": "gpt-4o",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": SECRET_COT}],
                    "encrypted_content": SECRET_COT,
                },
            ],
        },
    )
    assert status == 200
    rid = created["id"]
    record = await rs.get_response(rid, workspace_id="t1")
    assert SECRET_COT not in json.dumps(record.request, ensure_ascii=False)
    stored = await rs.list_input_items(rid)
    assert SECRET_COT not in json.dumps(stored, ensure_ascii=False)
    # The reasoning placeholder is kept for API completeness (R-P1-40) ...
    assert any(it.get("type") == "reasoning" for it in stored)
    # ... but never replayed.
    depth_row = await rs.chain_depth(rid)
    assert depth_row == 0  # no previous_response_id -> no chain row
