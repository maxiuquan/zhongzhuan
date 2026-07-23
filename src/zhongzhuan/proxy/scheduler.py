"""Scheduler: pick model / pick key."""
from __future__ import annotations

import random
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
    - 兜底降权：is_fallback 的 key 总分 ×0.1，只在所有正常 key 不可用时使用
    - 随机扰动（5%）：避免相同分数时总选同一个
    """
    if not k.is_available():
        return -1.0
    total = k.success_count + k.failure_count
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
    # 兜底 key 大幅降权
    fallback_penalty = 0.1 if k.is_fallback else 1.0

    base = (
        0.50 * success_rate
        + 0.20 * rpm_factor
        + 0.10 * tpm_factor
        + 0.15 * (1.0 if k.api_key else 0.0)
        + 0.05 * random.random()
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


def pick_group_model(
    g: Group,
    models: dict[int, ModelHealth],
    last_model_id: int | None = None,
) -> ModelHealth | None:
    members = g.members or []
    if not members:
        # 清理已删除 group 的计数器（优化点7：防止内存泄漏）
        _round_robin_counters.pop(g.id, None)
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
        weights: list[tuple[ModelHealth, float]] = []
        for m in members:
            h = models.get(m.model_id)
            if h and h.available:
                weights.append((h, m.weight * h.weight_penalty))
        if not weights:
            return None
        total = sum(w for _, w in weights)
        if total <= 0:
            return weights[0][0]
        pick = random.random() * total
        for h, w in weights:
            pick -= w
            if pick < 0:
                return h
        return weights[-1][0]
    return None