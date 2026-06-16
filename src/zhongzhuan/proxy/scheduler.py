"""Scheduler: pick model / pick key."""
from __future__ import annotations

import random
from dataclasses import dataclass

from .ratelimit import KeyHealth


def score(k: KeyHealth) -> float:
    """Key health score. 0-1 range, higher = better."""
    if not k.is_available():
        return -1.0
    total = k.success_count + k.failure_count
    success_rate = 1.0 if total == 0 else k.success_count / total
    if k.rpm_limit > 0:
        window = 1.0 - k.window.current_usage() / k.rpm_limit
    else:
        window = 1.0
    return (
        0.50 * success_rate
        + 0.30 * window
        + 0.15 * (1.0 if k.api_key else 0.0)
        + 0.05 * random.random()
    )


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