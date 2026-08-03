"""Key CRUD tests."""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

import pytest

from zhongzhuan.store.models import Model, create_model
from zhongzhuan.store.keys import ApiKey, create_key, list_keys, get_key_cipher, delete_key


@pytest.mark.asyncio
async def test_key_crud(store):
    m = await create_model(store, Model(name="m1", upstream_base="http://x", upstream_model="m1"))
    k = await create_key(store, ApiKey(id=None, model_id=m.id, label="L", key_value="sk-longkey123456"))
    assert k.id and k.id > 0
    rows = await list_keys(store, m.id)
    assert len(rows) == 1
    assert rows[0].key_masked.startswith("sk-l")
    plain = await get_key_cipher(store, k.id)
    assert plain == "sk-longkey123456"
    await delete_key(store, k.id)
    assert (await list_keys(store, m.id)) == []
