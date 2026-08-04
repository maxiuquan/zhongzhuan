"""Model CRUD tests."""

import os

os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

import pytest

from zhongzhuan.store.models import (
    Model,
    create_model,
    get_model,
    list_models,
    update_model,
    delete_model,
)


@pytest.mark.asyncio
async def test_model_crud(store):
    m = await create_model(store, Model(name="gpt-4o", upstream_base="https://x", upstream_model="gpt-4o"))
    assert m.id and m.id > 0
    got = await get_model(store, "gpt-4o")
    assert got is not None
    assert got.name == "gpt-4o"
    await update_model(
        store,
        m.id,
        Model(
            name="gpt-4o",
            upstream_base="https://x",
            upstream_model="gpt-4o-renamed",
            rpm_limit=100,
        ),
    )
    got2 = await get_model(store, "gpt-4o")
    assert got2 is not None
    assert got2.upstream_model == "gpt-4o-renamed"
    assert got2.rpm_limit == 100
    models = await list_models(store)
    assert any(x.id == m.id for x in models)
    await delete_model(store, m.id)
    assert (await get_model(store, "gpt-4o")) is None


@pytest.mark.asyncio
async def test_model_capabilities_roundtrip(store):
    """v005 columns: capabilities / upstream_mode survive create + read + update (T25 / D1)."""
    m = await create_model(
        store,
        Model(
            name="gpt-4o-cap",
            upstream_base="https://x",
            upstream_model="gpt-4o-cap",
            capabilities="web_search,code_interpreter",
            upstream_mode="native",
        ),
    )
    assert m.id and m.id > 0

    got = await get_model(store, "gpt-4o-cap")
    assert got is not None
    assert got.capabilities == "web_search,code_interpreter"
    assert got.upstream_mode == "native"

    # update: change mode, clear capabilities
    await update_model(
        store,
        m.id,
        Model(
            name="gpt-4o-cap",
            upstream_base="https://x",
            upstream_model="gpt-4o-cap",
            capabilities="",
            upstream_mode="emulate",
        ),
    )
    got2 = await get_model(store, "gpt-4o-cap")
    assert got2 is not None
    assert got2.capabilities == ""
    assert got2.upstream_mode == "emulate"

    await delete_model(store, m.id)
    assert (await get_model(store, "gpt-4o-cap")) is None


@pytest.mark.asyncio
async def test_model_capabilities_defaults(store):
    """Columns default to "" / "bonded" when the caller omits them (T25 / D1)."""
    m = await create_model(
        store,
        Model(
            name="gpt-4o-default",
            upstream_base="https://x",
            upstream_model="gpt-4o-default",
        ),
    )
    got = await get_model(store, "gpt-4o-default")
    assert got is not None
    assert got.capabilities == ""
    assert got.upstream_mode == "bonded"
    await delete_model(store, m.id)
