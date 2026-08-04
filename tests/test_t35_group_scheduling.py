"""T35 / R-P1-60: 分组调度策略生效 —— round_robin / weighted / failover 分布断言。

判据映射（架构文档 §T35 完成判据）：
- 判据①: round_robin / weighted / failover 各断言 100 次调用分布；failover 严格遵守 ``ord`` 顺序。
- 判据②: weighted 使用**可测试的平滑加权轮询**（非随机，确定性可重放）。

设计约束
--------
* **禁止 random**：平滑加权轮询的分布由模块级 ``_swrr_state`` 累积决定，同一组
  权重连续调用 100 次，分布稳定且与权重成比例。
* 测试直接调用 ``pick_group_model``，不经过网络。
"""

from __future__ import annotations

from collections import Counter

import pytest

from zhongzhuan.proxy.scheduler import (
    Group,
    GroupMember,
    ModelHealth,
    _swrr_state,
    pick_group_model,
)


@pytest.fixture(autouse=True)
def _clean_swrr_state():
    """每个测试前清空模块级 SWRR 状态，避免测试间相互污染。"""
    _swrr_state.clear()
    yield
    _swrr_state.clear()


def _models(*pairs: tuple[int, bool]) -> dict[int, ModelHealth]:
    """构造 ``{model_id: ModelHealth}``。``(id, available)`` 对。"""
    return {mid: ModelHealth(model_id=mid, name=f"m{mid}", available=avail) for mid, avail in pairs}


def _group(members: list[tuple[int, int, int]], strategy: str) -> Group:
    """``(model_id, weight, ord)`` 列表构造一个 group。"""
    return Group(
        id=1,
        name="g",
        strategy=strategy,
        members=[GroupMember(model_id=mid, weight=w, ord=o) for mid, w, o in members],
    )


# --------------------------------------------------------------------------- #
# 判据① 上半：round_robin 100 次调用分布
# --------------------------------------------------------------------------- #


def test_round_robin_100_calls_balanced():
    """round_robin：100 次调用在两个可用模型间均匀分布（各 50 次）。"""
    g = _group([(1, 1, 0), (2, 1, 0)], "round_robin")
    models = _models((1, True), (2, True))
    counts = Counter()
    for _ in range(100):
        h = pick_group_model(g, models)
        assert h is not None
        counts[h.model_id] += 1
    assert counts == {1: 50, 2: 50}


def test_round_robin_skips_unavailable_and_last_model():
    """round_robin：不可用模型被跳过；last_model_id 优先不重复选（除非只剩它）。"""
    g = _group([(1, 1, 0), (2, 1, 0), (3, 1, 0)], "round_robin")
    # 模型 2 不可用
    models = _models((1, True), (2, False), (3, True))
    picked = [pick_group_model(g, models, last_model_id=None) for _ in range(6)]
    ids = [h.model_id for h in picked]
    assert 2 not in ids
    assert ids.count(1) == 3 and ids.count(3) == 3
    # last_model_id=3 时下一轮先避开 3
    h = pick_group_model(g, models, last_model_id=3)
    assert h.model_id != 3
    # 只有一个可用模型时，last_model_id 相同也返回它（避免空转）
    g1 = _group([(1, 1, 0), (2, 1, 0)], "round_robin")
    models1 = _models((1, True), (2, False))
    h1 = pick_group_model(g1, models1, last_model_id=1)
    assert h1.model_id == 1


# --------------------------------------------------------------------------- #
# 判据① 上半 + 判据②：weighted 100 次调用分布（平滑加权轮询）
# --------------------------------------------------------------------------- #


def test_weighted_100_calls_proportional_3_to_1():
    """weighted 3:1 → 100 次调用 ≈75:25（断言落在 70~80 vs 20~30 区间）。

    这是平滑加权轮询的核心分布断言：连续调用 100 次，选中次数与权重成比例。
    """
    g = _group([(1, 3, 0), (2, 1, 0)], "weighted")
    models = _models((1, True), (2, True))
    counts = Counter()
    for _ in range(100):
        h = pick_group_model(g, models)
        assert h is not None
        counts[h.model_id] += 1
    assert 70 <= counts[1] <= 80, f"model1 count {counts[1]} outside 70..80"
    assert 20 <= counts[2] <= 30, f"model2 count {counts[2]} outside 20..30"
    assert counts[1] + counts[2] == 100


def test_weighted_deterministic_replayable():
    """weighted 确定性可重放：清空状态后连续调用 100 次，序列可复现。"""
    g = _group([(1, 3, 0), (2, 1, 0)], "weighted")
    models = _models((1, True), (2, True))

    def run_once() -> list[int]:
        _swrr_state.clear()
        return [pick_group_model(g, models).model_id for _ in range(100)]

    seq1 = run_once()
    seq2 = run_once()
    assert seq1 == seq2, "平滑加权轮询必须是确定性的（同一权重序列可复现）"


def test_weighted_maximum_consecutive_picks_bounded():
    """weighted 平滑性：连击有界、低权重成员不会挨饿。

    nginx 平滑加权轮询在权重 3:1 下的性质：
    * 最大连击 ≤ ``ceil(max_weight / min_weight)`` = 3；
    * 每轮（total=4 次调用）里低权重成员**至少出现一次** —— 等待间隔有界。
    随机抽签在 100 次里几乎必然出现 >3 连击（概率 ≈ 1 - (3/4)^3^94，≈ 1），
    所以这两条断言足以把「随机」变异点打回去。
    """
    g = _group([(1, 3, 0), (2, 1, 0)], "weighted")
    models = _models((1, True), (2, True))
    seq = [pick_group_model(g, models).model_id for _ in range(100)]
    run = 1
    max_run = 1
    for a, b in zip(seq, seq[1:]):
        run = run + 1 if a == b else 1
        max_run = max(max_run, run)
    assert max_run <= 3, f"max consecutive picks {max_run} > 3 (not smooth)"
    # 低权重成员最大等待间隔有界：任何位置往前 4 次调用内必出现一次 model2。
    # （窗口宽度 = total = 3+1 = 4，窗口内 model2 至少 1 次。）
    for start in range(0, len(seq) - 3):
        window = seq[start : start + 4]
        assert 2 in window, f"low-weight member starved in window {window}"


def test_weighted_three_members_distribution():
    """weighted 3:2:1 → 100 次调用分布 ≈50:33:17。"""
    g = _group([(1, 3, 0), (2, 2, 0), (3, 1, 0)], "weighted")
    models = _models((1, True), (2, True), (3, True))
    counts = Counter()
    for _ in range(100):
        h = pick_group_model(g, models)
        assert h is not None
        counts[h.model_id] += 1
    assert 45 <= counts[1] <= 55
    assert 28 <= counts[2] <= 38
    assert 12 <= counts[3] <= 22


def test_weighted_penalty_shifts_distribution():
    """weight_penalty 参与权重折算：weight 3 × penalty 0.5 = 1.5 → 四舍五入 2:1。"""
    g = _group([(1, 3, 0), (2, 1, 0)], "weighted")
    models = {
        1: ModelHealth(model_id=1, name="m1", available=True, weight_penalty=0.5),
        2: ModelHealth(model_id=2, name="m2", available=True, weight_penalty=1.0),
    }
    counts = Counter()
    for _ in range(100):
        h = pick_group_model(g, models)
        assert h is not None
        counts[h.model_id] += 1
    # 折算后权重 2 : 1 → 100 次约 67:33；罚重后不应仍是 75:25。
    assert 60 <= counts[1] <= 73
    assert 27 <= counts[2] <= 40


def test_weighted_skips_unavailable_and_returns_none():
    """weighted：全部不可用返回 None；部分不可用时跳过。"""
    g = _group([(1, 3, 0), (2, 1, 0)], "weighted")
    models = _models((1, False), (2, False))
    assert pick_group_model(g, models) is None
    models2 = _models((1, False), (2, True))
    h = pick_group_model(g, models2)
    assert h is not None and h.model_id == 2


# --------------------------------------------------------------------------- #
# 判据① 下半：failover 严格遵守 ord 顺序
# --------------------------------------------------------------------------- #


def test_failover_strictly_follows_ord():
    """failover：永远选 ``ord`` 最小的可用成员，无视成员列表顺序。"""
    # 成员故意乱序传入；ord 顺序是 3→1→2
    g = _group([(3, 1, 2), (1, 1, 0), (2, 1, 1)], "failover")
    models = _models((1, True), (2, True), (3, True))
    for _ in range(100):
        h = pick_group_model(g, models)
        assert h is not None
        assert h.model_id == 1, "ord=0 的成员健康时必须始终被选中"


def test_failover_falls_through_in_ord_order():
    """failover：ord=0 不可用时退到 ord=1，再退到 ord=2。"""
    g = _group([(3, 1, 2), (1, 1, 0), (2, 1, 1)], "failover")
    models = _models((1, False), (2, True), (3, True))
    for _ in range(100):
        assert pick_group_model(g, models).model_id == 2
    models2 = _models((1, False), (2, False), (3, True))
    for _ in range(100):
        assert pick_group_model(g, models2).model_id == 3
    models3 = _models((1, False), (2, False), (3, False))
    assert pick_group_model(g, models3) is None


def test_failover_does_not_rotate():
    """failover 不做轮转：健康模型恒定返回，调用次数不改变选择。"""
    g = _group([(1, 1, 0), (2, 1, 1)], "failover")
    models = _models((1, True), (2, True))
    picked = [pick_group_model(g, models).model_id for _ in range(100)]
    assert picked == [1] * 100
