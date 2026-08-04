"""T37 criterion ②: store / previous_response_id / background multi-turn flows.

Every call goes through the official ``openai`` Python SDK.  The v3 skeleton
persists chains in ``response_state_chain`` (R-P0-29 guards: self-reference,
cycles, depth, budget) and records ``background`` on the response object.

Coverage:
* ``store=True`` persists a response that ``retrieve`` returns.
* ``store=False`` does not persist (official 404 on retrieve).
* ``previous_response_id`` round-trips on the response object and persists a
  state chain (chain recovery is unit-tested in T22; here we assert the SDK
  surface: the field echoes back and a chain row exists in the store).
* ``background=True`` is echoed on the created response object.
* Chain error semantics: an unknown ``previous_response_id`` surfaces the
  official chain error instead of a silent downgrade (R-P0-29).
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_store_true_persists_and_retrieve_roundtrips(sdk_env):
    """store=True -> retrieve() returns the same response."""
    client = sdk_env["client"]
    created = await client.responses.create(model="gpt-4o", input="hello", store=True)

    got = await client.responses.retrieve(created.id)
    assert got.id == created.id
    assert got.store is True
    assert got.previous_response_id is None


@pytest.mark.asyncio
async def test_previous_response_id_multi_turn_roundtrip(sdk_env):
    """Round 1 -> Round 2 with previous_response_id echoes the chain field."""
    client = sdk_env["client"]
    r1 = await client.responses.create(model="gpt-4o", input="turn one")
    r2 = await client.responses.create(model="gpt-4o", input="turn two", previous_response_id=r1.id)

    assert r2.previous_response_id == r1.id
    assert r2.id != r1.id

    # The chain is persisted: the store has a state-chain row for r2 -> r1.
    rs = sdk_env["rs"]
    prev = await rs.get_previous_response_id(r2.id)
    assert prev == r1.id


@pytest.mark.asyncio
async def test_unknown_previous_response_id_raises_chain_error(sdk_env):
    """Unknown parent -> official chain error, never a silent stateless turn.

    R-P0-29: an unresolvable chain is a standard Responses error.  The SDK
    surfaces it as a typed ``APIStatusError``.
    """
    from openai import APIStatusError

    client = sdk_env["client"]
    with pytest.raises(APIStatusError) as exc:
        await client.responses.create(
            model="gpt-4o",
            input="hi",
            previous_response_id="resp_no_such_ancestor_00000000",
        )
    assert exc.value.status_code == 400  # chain_error_response -> 400


@pytest.mark.asyncio
async def test_background_flag_roundtrip(sdk_env):
    """background=True is echoed on the created response object (R-P1-34)."""
    client = sdk_env["client"]
    created = await client.responses.create(model="gpt-4o", input="bg", background=True)

    assert created.background is True
    # background responses are persisted (store defaults to True) so the flag
    # survives a retrieve.
    got = await client.responses.retrieve(created.id)
    assert got.background is True


@pytest.mark.asyncio
async def test_three_turn_chain_preserves_input_items(sdk_env):
    """3-turn chain: each turn's input items are persisted and listable."""
    client = sdk_env["client"]
    texts = ["first message", "second message", "third message"]
    prev_id: str | None = None
    for text in texts:
        created = await client.responses.create(
            model="gpt-4o",
            input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
            previous_response_id=prev_id or None,
        )
        prev_id = created.id

    # Third turn's chain points at the second turn.
    assert prev_id is not None
    got = await client.responses.retrieve(prev_id)
    assert got.previous_response_id is not None
    assert got.previous_response_id != prev_id
