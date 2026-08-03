"""模型定价 CRUD（用于成本估算）。

定价单位：每 1K tokens 的价格（CNY 人民币）。
费用计算：cost = (tokens_in/1000 * input_price) + (tokens_out/1000 * output_price)
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass
class ModelPricing:
    model_name: str
    input_price_per_1k: float = 0.0
    output_price_per_1k: float = 0.0
    currency: str = "CNY"
    updated_at: int | None = None


# 参考_one_api 标准倍率的默认定价表（每 1K tokens，单位：CNY）
# 实际部署时应在后台按真实上游价格调整
_DEFAULT_PRICING = {
    "gpt-4":        (0.21, 0.42),
    "gpt-4-turbo":  (0.14, 0.28),
    "gpt-4o":       (0.035, 0.14),
    "gpt-4o-mini":  (0.00105, 0.0042),
    "gpt-3.5-turbo": (0.0015, 0.002),
    "claude-3-opus":  (0.105, 0.525),
    "claude-3-sonnet": (0.021, 0.105),
    "claude-3-haiku":  (0.00168, 0.0084),
    "claude-3.5-sonnet": (0.021, 0.105),
}


async def get_pricing(s: Store, model_name: str) -> ModelPricing | None:
    """查询单个模型的定价。"""
    r = await s.fetchone(
        "SELECT model_name, input_price_per_1k, output_price_per_1k, currency, updated_at FROM model_pricing WHERE model_name=?",
        (model_name,),
    )
    if not r:
        return None
    return ModelPricing(
        model_name=r[0], input_price_per_1k=r[1], output_price_per_1k=r[2],
        currency=r[3] if r[3] else "CNY", updated_at=r[4],
    )


async def list_pricing(s: Store) -> list[ModelPricing]:
    """列出所有模型定价。"""
    rows = await s.fetchall(
        "SELECT model_name, input_price_per_1k, output_price_per_1k, currency, updated_at FROM model_pricing ORDER BY model_name"
    )
    return [
        ModelPricing(
            model_name=r[0], input_price_per_1k=r[1], output_price_per_1k=r[2],
            currency=r[3] if r[3] else "CNY", updated_at=r[4],
        )
        for r in rows
    ]


async def upsert_pricing(s: Store, p: ModelPricing) -> None:
    """插入或更新模型定价。"""
    now = Store.now()
    # 跨数据库兼容：先 DELETE 再 INSERT
    await s.execute("DELETE FROM model_pricing WHERE model_name=?", (p.model_name,))
    await s.execute(
        "INSERT INTO model_pricing(model_name, input_price_per_1k, output_price_per_1k, currency, updated_at) VALUES(?,?,?,?,?)",
        (p.model_name, p.input_price_per_1k, p.output_price_per_1k, p.currency, now),
    )


async def delete_pricing(s: Store, model_name: str) -> None:
    await s.execute("DELETE FROM model_pricing WHERE model_name=?", (model_name,))


async def calculate_cost(s: Store, model_name: str, tokens_in: int, tokens_out: int) -> float:
    """计算单次请求的费用。无定价记录则返回 0。"""
    if tokens_in <= 0 and tokens_out <= 0:
        return 0.0
    p = await get_pricing(s, model_name)
    if p is None:
        return 0.0
    return (tokens_in / 1000.0) * p.input_price_per_1k + (tokens_out / 1000.0) * p.output_price_per_1k


async def init_default_pricing(s: Store) -> int:
    """初始化默认定价表（仅在表为空时执行）。返回插入条数。"""
    existing = await s.fetchone("SELECT COUNT(*) FROM model_pricing")
    if existing and existing[0] > 0:
        return 0
    count = 0
    for model, (inp, out) in _DEFAULT_PRICING.items():
        await upsert_pricing(s, ModelPricing(
            model_name=model, input_price_per_1k=inp, output_price_per_1k=out, currency="CNY",
        ))
        count += 1
    return count
