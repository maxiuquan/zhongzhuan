"""``_resolve_candidates`` 严格匹配测试。

回归场景（Trae 403 → 跨模型路由 bug 的根治）：
指定模型名时，若组/模型/别名都匹配不到（或匹配到的 key 全部不可用），
必须返回**空列表**，绝不能回退到其他模型的 key（原来 ``return available``
会把 gpt-5.6-sol 的请求悄悄发给 agnes/oc-* 等慢模型）。

仅当请求未指定模型（空串）时，才允许从所有可用 key 中兜底。
"""

from zhongzhuan.proxy.handler import ProxyHandler
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.proxy.retry import mark_auth_failure


def _mk(key_id: int, model_name: str, *, model_id: int | None = None, available: bool = True, aliases: str = "") -> KeyHealth:
    k = KeyHealth(
        key_id=key_id,
        api_key=f"sk-test-{key_id}",
        model_id=model_id if model_id is not None else key_id,
        model_name=model_name,
        aliases=aliases,
        window=SlidingWindow(60, 1000),
        rpm_limit=1000,
    )
    if not available:
        mark_auth_failure(k)  # status → invalid，等价于真实 403 后的状态
    return k


def _handler(keys: list[KeyHealth], groups: list[dict] | None = None) -> ProxyHandler:
    return ProxyHandler(clients={}, keys=keys, groups=groups)


def test_specified_model_all_keys_invalid_returns_empty():
    """指定模型但该模型所有 key 都 invalid → 空列表，不跨模型兜底（核心回归）。"""
    keys = [
        _mk(1, "gpt-5.6-sol", available=False),
        _mk(2, "gpt-5.6-sol", available=False),
        _mk(3, "agnes", available=True),  # 其他模型 healthy，绝不能被抓去用
    ]
    h = _handler(keys)
    assert h._resolve_candidates("gpt-5.6-sol") == []


def test_specified_model_matches_only_its_own_keys():
    """指定模型且该模型有可用 key → 只返回该模型的 key。"""
    keys = [
        _mk(1, "gpt-5.6-sol", available=True),
        _mk(2, "agnes", available=True),
    ]
    h = _handler(keys)
    got = h._resolve_candidates("gpt-5.6-sol")
    assert [k.key_id for k in got] == [1]


def test_unknown_model_returns_empty():
    """指定一个没配置的模型名 → 空列表（原来返回所有可用 key）。"""
    keys = [
        _mk(1, "agnes", available=True),
        _mk(2, "oc-deepseek-v4-flash-free", available=True),
    ]
    h = _handler(keys)
    assert h._resolve_candidates("not-a-real-model") == []


def test_empty_model_returns_all_available():
    """未指定模型（空串）→ 仍从所有可用 key 兜底（保持向后兼容）。"""
    keys = [
        _mk(1, "agnes", available=True),
        _mk(2, "oc-mimo-v2.5-free", available=True),
        _mk(3, "gpt-5.6-sol", available=False),
    ]
    h = _handler(keys)
    got = h._resolve_candidates("")
    assert {k.key_id for k in got} == {1, 2}


def test_group_match_still_works():
    """指定组名 → 返回组内可用 key（组路由语义不受影响）。"""
    keys = [
        _mk(1, "agnes", available=True),
        _mk(2, "oc-deepseek-v4-flash-free", available=True),
        _mk(3, "gpt-5.6-sol", available=True),
    ]
    groups = [
        {"name": "mf", "members": [1, 2]},
    ]
    h = _handler(keys, groups=groups)
    got = h._resolve_candidates("mf")
    assert {k.key_id for k in got} == {1, 2}


def test_group_match_but_no_available_member_returns_empty():
    """指定组名但组内成员全部不可用 → 空列表（不跨模型兜底到组外）。"""
    keys = [
        _mk(1, "agnes", available=False),
        _mk(2, "oc-deepseek-v4-flash-free", available=False),
        _mk(3, "gpt-5.6-sol", available=True),  # 组外模型，绝不能被抓去用
    ]
    groups = [
        {"name": "mf", "members": [1, 2]},
    ]
    h = _handler(keys, groups=groups)
    assert h._resolve_candidates("mf") == []


def test_passthrough_mode_unknown_model_still_serves():
    """透传模式（所有 key 未绑定模型名）：指定任意模型名仍回退到可用 key。

    兼容 ``ProxyServer(api_key=...)`` 简写与旧测试：这种模式下所有 key 的
    model_name 都为空，不存在"跨模型路由"风险，保持宽松行为。
    """
    k = KeyHealth(
        key_id=1,
        api_key="sk-1",
        window=SlidingWindow(60, 1000),
        rpm_limit=1000,
    )  # model_name 默认空串
    h = _handler([k])
    got = h._resolve_candidates("x")  # 任意模型名
    assert [k.key_id for k in got] == [1]


def test_mixed_binding_unknown_model_returns_empty():
    """有 key 绑定了模型名时，指定未知模型 → 空列表（严格模式生效）。"""
    keys = [
        _mk(1, "agnes", available=True),  # 绑定了模型名
        _mk(2, "oc-deepseek-v4-flash-free", available=True),
    ]
    h = _handler(keys)
    assert h._resolve_candidates("not-a-real-model") == []
