"""Key CRUD tests."""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

from zhongzhuan.store import Store
from zhongzhuan.store.models import Model, create_model
from zhongzhuan.store.keys import ApiKey, create_key, list_keys, get_key_cipher, delete_key


def test_key_crud(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    try:
        m = create_model(s, Model(name="m1", upstream_base="http://x", upstream_model="m1"))
        k = create_key(s, ApiKey(id=None, model_id=m.id, label="L", key_value="sk-longkey123456"))
        assert k.id and k.id > 0
        rows = list_keys(s, m.id)
        assert len(rows) == 1
        assert rows[0].key_masked.startswith("sk-l")
        plain = get_key_cipher(s, k.id)
        assert plain == "sk-longkey123456"
        delete_key(s, k.id)
        assert list_keys(s, m.id) == []
    finally:
        s.close()