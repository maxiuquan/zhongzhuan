"""Retry & cooldown utilities."""
from __future__ import annotations

import time

from .ratelimit import KeyHealth


def cooldown_for(failures: int) -> float:
    """Return cooldown seconds based on consecutive failures."""
    if failures <= 1:
        return 5.0
    if failures == 2:
        return 10.0
    if failures == 3:
        return 30.0
    return 60.0


def mark_failure(k: KeyHealth) -> None:
    k.failure_count += 1
    k.recent_429_count += 1
    k.cooldown_until = time.time() + cooldown_for(k.failure_count)


def mark_success(k: KeyHealth) -> None:
    k.success_count += 1
    if k.recent_429_count > 0:
        k.recent_429_count = 0
    k.cooldown_until = 0.0