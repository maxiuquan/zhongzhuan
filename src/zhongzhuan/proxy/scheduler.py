"""Scheduler: pick model / pick key."""
from __future__ import annotations

from dataclasses import dataclass

from .ratelimit import KeyHealth, STATE_HEALTHY, STATE_RATE_LIMITED, STATE_ERROR


# 状态权重：healthy 满分，error 大幅降权（但仍可用），rate_limited 中等降权
_STATUS_WEIGHT = {
    STATE_HEALTHY: 1.0,
    STATE_RATE_LIMITED: 0.5,
    STATE_ERROR: 0.3,
}


def score(k: KeyHealth) -> float:
    """Key health score. 0-1 range, higher = better.

    评分维度：
    - 成功率（50%）：历史成功比例
    - RPM 窗口余量（20%）：当前分钟配额剩余比例
    - TPM 窗口余量（10%）：当前分钟 token 配额剩余比例
    - 状态权重：healthy=1.0, rate_limited=0.5, error=0.3
    - 兜底降权：is_fallback 的 key 总分 ×fallback_penalty（可配置，默认 0.1）
    - 随机扰动（5%）：避免相同分数时总选同一个
    """
    import random
    if not k.is_available():
        return -1.0
    total = k.success_count + k.total_failures
    success_rate = 1.0 if total == 0 else k.success_count / total
    # RPM 窗口余量
    if k.rpm_limit > 0 and k.window is not None:
        rpm_usage = k.window.current_usage()
        rpm_factor = max(0.0, 1.0 - rpm_usage / k.rpm_limit)
    else:
        rpm_factor = 1.0
    # TPM 窗口余量
    if k.tpm_limit > 0 and k.tpm_window is not None:
        tpm_usage = k.tpm_window.current_usage()
        tpm_factor = max(0.0, 1.0 - tpm_usage / k.tpm_limit)
    else:
        tpm_factor = 1.0
    # 状态权重
    status_weight = _STATUS_WEIGHT.get(k.status, 0.5)
    # 兜底 key 降权（系数可配置，默认 0.1；1.0 表示不降权）
    fallback_penalty = k.fallback_penalty if k.is_fallback else 1.0

    base = (
        0.50 * success_rate
        + 0.20 * rpm_factor
        + 0.10 * tpm_factor
        + 0.15 * (1.0 if k.api_key else 0.0)
        + 0.05 * random.random()  # noqa: S311 -- 仅用于分数打散，不参与加权调度
    )
    return base * status_weight * fallback_penalty


def pick_key(keys: list[KeyHealth]) -> KeyHealth | None:
    best: KeyHealth | None = None
    best_score = -1.0
    for k in keys:
        s = score(k)
        if s > best_score:
            best_score = s
            best = k
    return best


# ---------------- Group scheduling ----------------


@dataclass
class GroupMember:
    model_id: int
    weight: int = 1
    ord: int = 0


@dataclass
class Group:
    id: int
    name: str
    strategy: str
    fallback_enabled: bool = True
    members: list[GroupMember] | None = None

    def member_ids(self) -> list[int]:
        return [m.model_id for m in (self.members or [])]


@dataclass
class ModelHealth:
    model_id: int
    name: str
    available: bool = True
    weight_penalty: float = 1.0


_round_robin_counters: dict[int, int] = {}

# --------------------------------------------------------------------------- #
# Smooth Weighted Round Robin (nginx 同款算法，T35 / R-P1-60)
# --------------------------------------------------------------------------- #
# 每个 (group, model) 成员维护 ``current_weight``；每轮先全体 ``+= weight``，
# 然后选 ``current_weight`` 最大的成员，选中后 ``current_weight -= total``。
# 与随机抽签的本质区别：**确定性可重放** —— 同一组权重连续调用 N 次，各成员的
# 选中次数与权重成比例（权重 3:1 时 100 次 ≈ 75:25），且两次连续选中同一成员
# 的次数有界（≤ 2），这是「平滑」的度量。状态挂在模块级字典上，天然跨调用
# 累积；测试直接连续调用 ``pick_group_model`` 断言分布即可。
_swrr_state: dict[int, dict[int, int]] = {}


def _swrr_pick(group_id: int, candidates: list[tuple[int, ModelHealth, int]]) -> ModelHealth:
    """从 ``(model_id, health, weight)`` 候选中做一次平滑加权选择。

    *weight* 取 ``weight * weight_penalty`` 后的整数权重（下取整、至少 1），
    保证全正权重下 ``total > 0`` 恒成立。
    """
    state = _swrr_state.setdefault(group_id, {})
    for mid, _h, _w in candidates:
        state[mid] = state.get(mid, 0) + _w
    best_mid = max(candidates, key=lambda c: state[c[0]])[0]
    total = sum(c[2] for c in candidates)
    state[best_mid] -= total
    # 清理不再属于本组的成员状态，防止已删除成员 / 不可用成员泄漏
    for mid in list(state):
        if mid not in {c[0] for c in candidates}:
            del state[mid]
    for mid, h, _w in candidates:
        if mid == best_mid:
            return h
    return candidates[0][1]


def pick_group_model(
    g: Group,
    models: dict[int, ModelHealth],
    last_model_id: int | None = None,
) -> ModelHealth | None:
    members = g.members or []
    if not members:
        # 清理已删除 group 的计数器（优化点7：防止内存泄漏）
        _round_robin_counters.pop(g.id, None)
        _swrr_state.pop(g.id, None)
        return None
    if g.strategy == "failover":
        for m in sorted(members, key=lambda x: x.ord):
            h = models.get(m.model_id)
            if h and h.available:
                return h
        return None
    if g.strategy == "round_robin":
        candidates: list[ModelHealth] = []
        for m in members:
            if m.model_id == last_model_id:
                continue
            h = models.get(m.model_id)
            if h and h.available:
                candidates.append(h)
        if not candidates:
            for m in members:
                h = models.get(m.model_id)
                if h and h.available:
                    candidates.append(h)
        if not candidates:
            return None
        idx = _round_robin_counters.get(g.id, 0) % len(candidates)
        _round_robin_counters[g.id] = idx + 1
        return candidates[idx]
    if g.strategy == "weighted":
        weights: list[tuple[int, ModelHealth, int]] = []
        for m in members:
            h = models.get(m.model_id)
            if h and h.available:
                # 权重 × 健康惩罚折算后四舍五入取整（≥1）：0.5 惩罚下 3→1.5→2，
                # 若下取整会退化成 1 抹掉差异。
                w = max(1, int(m.weight * h.weight_penalty + 0.5))
                weights.append((m.model_id, h, w))
        if not weights:
            return None
        return _swrr_pick(g.id, weights)
    return None