"""pricing.py 计费数学直接单测（store/pricing.py）。

覆盖：
* 单价解析（行 -> ModelPricing，currency 缺省）；
* token -> 费用计算（cost = tokens_in/1000*input + tokens_out/1000*output）；
* 边界：0 / 负值 / 超大值；
* 精度与舍入；
* upsert / delete / init_default_pricing 的 CRUD 行为。

用最小内存 fake store 模拟 ``model_pricing`` 表，不落盘、不依赖真实后端。
"""

from __future__ import annotations

import math

import pytest

from zhongzhuan.store.pricing import (
    _DEFAULT_PRICING,
    ModelPricing,
    calculate_cost,
    delete_pricing,
    get_pricing,
    init_default_pricing,
    list_pricing,
    upsert_pricing,
)


class _PricingStore:
    """最小内存 store：只模拟 pricing.py 用到的接口与 ``model_pricing`` 表。

    行布局与 SELECT 一致：(model_name, input_price_per_1k, output_price_per_1k,
    currency, updated_at)，首列为主键。``transaction()`` 与基类默认一致
    （no-op 批处理：execute 即时生效）。
    """

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows: dict[str, tuple] = {r[0]: tuple(r) for r in (rows or [])}
        self.executed: list[tuple[str, tuple | None]] = []

    def transaction(self):
        return _NoopTransaction()

    async def fetchone(self, sql: str, params: tuple | None = None):
        if "COUNT(*)" in sql.upper():
            return (len(self._rows),)
        if params:
            return self._rows.get(params[0])
        return None

    async def fetchall(self, sql: str, params: tuple | None = None):
        return sorted(self._rows.values())

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        head = sql.lstrip().split(None, 1)[0].upper()
        self.executed.append((sql, params))
        if head == "DELETE" and params:
            self._rows.pop(params[0], None)
            return 0
        if head == "INSERT" and params:
            self._rows[params[0]] = tuple(params)
            return 1
        return 0


class _NoopTransaction:
    """镜像 ``Store.transaction`` 的默认 no-op 形态。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ---------------------------------------------------------------------------
# 单价解析
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pricing_parses_row():
    store = _PricingStore(rows=[("gpt-4o", 0.035, 0.14, "CNY", 1700000000)])
    p = await get_pricing(store, "gpt-4o")
    assert p is not None
    assert isinstance(p, ModelPricing)
    assert p.model_name == "gpt-4o"
    assert p.input_price_per_1k == pytest.approx(0.035)
    assert p.output_price_per_1k == pytest.approx(0.14)
    assert p.currency == "CNY"
    assert p.updated_at == 1700000000


@pytest.mark.asyncio
async def test_get_pricing_missing_model_returns_none():
    store = _PricingStore(rows=[("gpt-4o", 0.035, 0.14, "CNY", None)])
    assert await get_pricing(store, "no-such-model") is None


@pytest.mark.asyncio
async def test_get_pricing_null_currency_defaults_to_cny():
    """currency 列为 NULL/空 → 回退默认 'CNY'。"""
    store = _PricingStore(rows=[("m1", 0.1, 0.2, "", None), ("m2", 0.1, 0.2, None, None)])
    assert (await get_pricing(store, "m1")).currency == "CNY"
    assert (await get_pricing(store, "m2")).currency == "CNY"


def test_model_pricing_dataclass_defaults():
    p = ModelPricing(model_name="x")
    assert p.input_price_per_1k == 0.0
    assert p.output_price_per_1k == 0.0
    assert p.currency == "CNY"
    assert p.updated_at is None


# ---------------------------------------------------------------------------
# token -> 费用计算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculate_cost_known_math():
    """cost = tokens_in/1000*input + tokens_out/1000*output（模块 docstring 公式）。"""
    store = _PricingStore(rows=[("gpt-4", 0.21, 0.42, "CNY", None)])
    cost = await calculate_cost(store, "gpt-4", 2000, 500)
    expected = (2000 / 1000) * 0.21 + (500 / 1000) * 0.42
    assert cost == pytest.approx(expected)
    assert cost == pytest.approx(0.42 + 0.21)


@pytest.mark.asyncio
async def test_calculate_cost_input_only_and_output_only():
    store = _PricingStore(rows=[("m", 0.10, 0.90, "CNY", None)])
    assert await calculate_cost(store, "m", 1000, 0) == pytest.approx(0.10)
    assert await calculate_cost(store, "m", 0, 2000) == pytest.approx(1.80)


@pytest.mark.asyncio
async def test_calculate_cost_both_tokens_zero_is_zero():
    store = _PricingStore()  # 故意空表：0 token 应在查价前短路
    assert await calculate_cost(store, "anything", 0, 0) == 0.0


@pytest.mark.asyncio
async def test_calculate_cost_negative_tokens_is_zero():
    """双负值走 ``both <= 0`` 守卫 → 直接 0，不产生负费用、不查表。"""
    store = _PricingStore(rows=[("m", 0.10, 0.20, "CNY", None)])
    assert await calculate_cost(store, "m", -100, -50) == 0.0


@pytest.mark.asyncio
async def test_calculate_cost_mixed_sign_follows_formula():
    """混合符号（一正一负）按公式原样计算——守卫只拦双双非正的情况。

    这里如实锁定现状语义：输入侧贡献为负、输出侧为正。
    """
    store = _PricingStore(rows=[("m", 0.10, 0.20, "CNY", None)])
    cost = await calculate_cost(store, "m", -1000, 1000)
    assert cost == pytest.approx(-0.10 + 0.20)


@pytest.mark.asyncio
async def test_calculate_cost_no_pricing_record_returns_zero():
    store = _PricingStore()
    assert await calculate_cost(store, "unlisted-model", 12345, 6789) == 0.0


@pytest.mark.asyncio
async def test_calculate_cost_huge_tokens_stays_finite():
    """超大值不溢出：结果仍是有限浮点且符合公式量级。"""
    store = _PricingStore(rows=[("gpt-4", 0.21, 0.42, "CNY", None)])
    big = 10**15
    cost = await calculate_cost(store, "gpt-4", big, big)
    assert math.isfinite(cost)
    assert cost == pytest.approx(big / 1000 * (0.21 + 0.42))


# ---------------------------------------------------------------------------
# 精度与舍入
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calculate_cost_small_unit_prices_keep_precision():
    """小单价（gpt-4o-mini 档）下浮点误差可控，可安全 round 到固定小数位。"""
    store = _PricingStore(rows=[("gpt-4o-mini", 0.00105, 0.0042, "CNY", None)])
    cost = await calculate_cost(store, "gpt-4o-mini", 1000, 3333)
    exact = 0.00105 + 3.333 * 0.0042
    assert cost == pytest.approx(exact, rel=1e-12)
    # 展示层按 8 位小数舍入是稳定的（两次独立计算一致）。
    assert round(cost, 8) == round(exact, 8)


@pytest.mark.asyncio
async def test_calculate_cost_rounding_to_cent_for_display():
    """常规用量下按两位小数（分）舍入得到稳定账面值。"""
    store = _PricingStore(rows=[("gpt-4", 0.21, 0.42, "CNY", None)])
    cost = await calculate_cost(store, "gpt-4", 1500, 700)
    assert round(cost, 2) == 0.61  # 0.315 + 0.294 = 0.609 -> 0.61


@pytest.mark.asyncio
async def test_calculate_cost_deterministic_across_calls():
    store = _PricingStore(rows=[("m", 0.033, 0.066, "CNY", None)])
    a = await calculate_cost(store, "m", 7777, 8888)
    b = await calculate_cost(store, "m", 7777, 8888)
    assert a == b  # 同参数同结果（无隐藏状态）


# ---------------------------------------------------------------------------
# upsert / delete / list / init
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_then_get_roundtrip():
    store = _PricingStore()
    await upsert_pricing(store, ModelPricing(model_name="m1", input_price_per_1k=0.5, output_price_per_1k=1.0))
    p = await get_pricing(store, "m1")
    assert p is not None
    assert p.input_price_per_1k == pytest.approx(0.5)
    assert p.output_price_per_1k == pytest.approx(1.0)
    assert p.currency == "CNY"
    assert p.updated_at is not None  # 写入时打上了时间戳


@pytest.mark.asyncio
async def test_upsert_replaces_existing_row():
    """upsert 先 DELETE 再 INSERT：同名只有一行，价格以最后一次为准。"""
    store = _PricingStore()
    await upsert_pricing(store, ModelPricing(model_name="m", input_price_per_1k=0.1, output_price_per_1k=0.2))
    await upsert_pricing(store, ModelPricing(model_name="m", input_price_per_1k=0.3, output_price_per_1k=0.6))
    assert len(await list_pricing(store)) == 1
    p = await get_pricing(store, "m")
    assert p.input_price_per_1k == pytest.approx(0.3)
    # upsert 的语句形态：先删后插（跨库兼容口径）
    heads = [sql.strip().split(None, 1)[0].upper() for sql, _p in store.executed]
    assert heads == ["DELETE", "INSERT"] * 2


@pytest.mark.asyncio
async def test_delete_pricing_removes_row():
    store = _PricingStore(rows=[("gone", 1.0, 2.0, "CNY", None)])
    await delete_pricing(store, "gone")
    assert await get_pricing(store, "gone") is None


@pytest.mark.asyncio
async def test_list_pricing_sorted_by_model_name():
    store = _PricingStore(
        rows=[
            ("b-model", 0.1, 0.2, "CNY", None),
            ("a-model", 0.3, 0.4, "USD", 7),
        ]
    )
    rows = await list_pricing(store)
    assert [r.model_name for r in rows] == ["a-model", "b-model"]
    assert rows[0].currency == "USD"


@pytest.mark.asyncio
async def test_init_default_pricing_seeds_empty_table_once():
    store = _PricingStore()
    count = await init_default_pricing(store)
    assert count == len(_DEFAULT_PRICING)
    assert len(await list_pricing(store)) == len(_DEFAULT_PRICING)
    # 表非空时不再重复播种。
    assert await init_default_pricing(store) == 0
