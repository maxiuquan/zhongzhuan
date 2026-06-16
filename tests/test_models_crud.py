"""Model CRUD tests."""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

from zhongzhuan.store import Store
from zhongzhuan.store.models import (
    Model, create_model, get_model, list_models, update_model, delete_model,
)


def test_model_crud(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    try:
        m = create_model(s, Model(name="gpt-4o", upstream_base="https://x", upstream_model="gpt-4o"))
        assert m.id and m.id > 0
        got = get_model(s, "gpt-4o")
        assert got is not None
        assert got.name == "gpt-4o"
        update_model(s, m.id, Model(
            name="gpt-4o", upstream_base="https://x", upstream_model="gpt-4o-renamed",
            rpm_limit=100,
        ))
        got2 = get_model(s, "gpt-4o")
        assert got2 is not None
        assert got2.upstream_model == "gpt-4o-renamed"
        assert got2.rpm_limit == 100
        models = list_models(s)
        assert any(x.id == m.id for x in models)
        delete_model(s, m.id)
        assert get_model(s, "gpt-4o") is None
    finally:
        s.close()