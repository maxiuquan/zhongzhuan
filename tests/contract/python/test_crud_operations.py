"""T37 criterion ①: six official Responses operations via the official SDK.

Covers ``create`` / ``retrieve`` / ``delete`` / ``cancel`` / ``compact`` /
``input_items`` -- every call goes through the **official** ``openai`` Python
SDK (``AsyncOpenAI.responses.*``), with **zero vendor-specific code**: no raw
HTTP, no private/undocumented SDK internals, no poking at response internals.

Compatibility notes (openai 2.53.0, verified against the live server):
* ``delete()`` returns ``None`` (the SDK casts the response to ``NoneType``),
  so the assertion is "no exception" + the response is gone on a later GET.
* ``compact()`` raises ``APIStatusError`` with ``status_code == 501`` because
  the v3 skeleton ships an honest 501 stub (T21 / R-P1-33); the test asserts
  the *official* error surface (typed exception + code) rather than a 200.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_returns_official_response_object(sdk_env):
    """create() -> typed ``Response`` with official fields."""
    client = sdk_env["client"]
    r = await client.responses.create(model="gpt-4o", input="hi")

    assert r.object == "response"
    assert r.id.startswith("resp_")
    # Non-stream create executes the real upstream chain synchronously, so the
    # returned object is already terminal (completed), not a skeleton.
    assert r.status == "completed"
    assert r.model == "gpt-4o"
    assert isinstance(r.output, list)
    assert r.usage is not None  # {} parses to an empty usage object
    assert r.previous_response_id is None
    assert r.store is True


@pytest.mark.asyncio
async def test_create_store_false_then_retrieve_404(sdk_env):
    """store=False -> not persisted -> retrieve() surfaces the official 404."""
    client = sdk_env["client"]
    r = await client.responses.create(model="gpt-4o", input="hi", store=False)

    with pytest.raises(Exception) as exc:
        await client.responses.retrieve(r.id)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_retrieve_returns_created_response(sdk_env):
    """create then retrieve round-trips the official object."""
    client = sdk_env["client"]
    created = await client.responses.create(model="gpt-4o", input="hi")

    got = await client.responses.retrieve(created.id)
    assert got.id == created.id
    assert got.object == "response"
    assert got.status in ("in_progress", "completed", "cancelled")


@pytest.mark.asyncio
async def test_retrieve_unknown_id_404(sdk_env):
    """retrieve() on a missing id raises the official 404."""
    client = sdk_env["client"]
    with pytest.raises(Exception) as exc:
        await client.responses.retrieve("resp_does_not_exist_0000")
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_delete_returns_none_and_removes_response(sdk_env):
    """delete() -> SDK 2.53 casts to None; a subsequent GET must 404."""
    client = sdk_env["client"]
    created = await client.responses.create(model="gpt-4o", input="hi")

    result = await client.responses.delete(created.id)
    assert result is None  # official SDK returns None for delete

    with pytest.raises(Exception) as exc:
        await client.responses.retrieve(created.id)
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_cancel_marks_response_cancelled(sdk_env):
    """cancel() -> the response object reports ``status == cancelled``."""
    client = sdk_env["client"]
    created = await client.responses.create(model="gpt-4o", input="hi")

    cancelled = await client.responses.cancel(created.id)
    assert cancelled.id == created.id
    assert cancelled.status == "cancelled"

    re_retrieved = await client.responses.retrieve(created.id)
    assert re_retrieved.status == "cancelled"


@pytest.mark.asyncio
async def test_compact_is_official_501_stub(sdk_env):
    """compact() -> official 501 ``not_implemented`` error (honest stub).

    The v3 skeleton intentionally returns 501 (reachable, documented, not
    silently dropped) -- R-P1-33.  We assert the official SDK error surface:
    a typed ``APIStatusError`` carrying status 501 and code ``not_implemented``.
    """
    from openai import APIStatusError

    client = sdk_env["client"]
    with pytest.raises(APIStatusError) as exc:
        await client.responses.compact(model="gpt-4o", input="hi")

    err = exc.value
    assert err.status_code == 501
    # The official error body is surfaced through the SDK's response envelope.
    import json as _json

    body = {}
    try:
        body = _json.loads(err.response.text) if getattr(err, "response", None) is not None else {}
    except (ValueError, TypeError):
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    assert error.get("code") == "not_implemented"


@pytest.mark.asyncio
async def test_input_items_lists_persisted_input(sdk_env):
    """input_items.list() -> official paginated ``ResponseItemList``."""
    client = sdk_env["client"]
    created = await client.responses.create(
        model="gpt-4o",
        input=[
            {
                "id": "msg_user_contract_0001",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
    )

    lst = await client.responses.input_items.list(created.id)
    assert lst.object == "list"
    assert isinstance(lst.data, list)
    assert len(lst.data) == 1
    item = lst.data[0]
    # openai 2.53 returns typed item objects (attribute access, not dict).
    assert item.type == "message"
    assert item.role == "user"
    assert item.content is not None
    assert lst.has_more is False


@pytest.mark.asyncio
async def test_input_items_pagination(sdk_env):
    """50 input items: page-1 returns 30 with ``has_more=True`` (T21 criterion ③).

    NOTE: the v3 skeleton uses a **seq cursor** (``after`` echoes a seq int)
    whereas the official API uses an item-id cursor (T21 deviation note in
    ``schema.to_input_items_list``).  The item-id vs seq cursor mismatch is a
    known deviation tracked for T38's compatibility report -- the SDK-level
    pagination walk via ``after=<last_id>`` is therefore NOT asserted here.
    We assert the *official response shape* of one page and ``has_more``.
    """
    client = sdk_env["client"]
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": f"m{i}"}]} for i in range(50)
    ]
    created = await client.responses.create(model="gpt-4o", input=items)

    page1 = await client.responses.input_items.list(created.id, limit=30)
    assert len(page1.data) == 30
    assert page1.has_more is True
    assert page1.object == "list"
    assert page1.first_id is not None
    assert page1.last_id is not None

    # Raw JSON echo via the SDK's raw-response accessor: the v3 handler returns
    # its own seq cursor (``after`` echoes a seq int -- T21 deviation).
    import json as _json

    raw = await client.responses.input_items.with_raw_response.list(created.id, limit=30)
    body = _json.loads(raw.text)
    assert body["object"] == "list"
    assert len(body["data"]) == 30
    assert body["has_more"] is True
    # seq cursor echo (T21 deviation: int, not item id).
    assert isinstance(body.get("after"), int)
