"""Rate limiter: sliding window + Key health."""
from __future__ import annotations

import time
from dataclasses import dataclass


class SlidingWindow:
    """60x 1s buckets circular array; O(1) complexity."""

    def __init__(self, window_seconds: int = 60, limit: int = 0) -> None:
        self.window_seconds = window_seconds
        self.limit = limit  # 0 = unlimited
        self.buckets: list[int] = [0] * window_seconds
        self.total: int = 0
        self.last_rotate: float = time.time()

    def _rotate(self) -> None:
        now = time.time()
        elapsed = int(now - self.last_rotate)
        if elapsed <= 0:
            return
        if elapsed >= self.window_seconds:
            self.buckets = [0] * self.window_seconds
            self.total = 0
            self.last_rotate = now
            return
        for _ in range(elapsed):
            self.total -= self.buckets.pop(0)
            self.buckets.append(0)
        self.last_rotate = now

    def allow(self, n: int = 1) -> bool:
        self._rotate()
        if self.limit > 0 and self.total + n > self.limit:
            return False
        self.buckets[-1] += n
        self.total += n
        return True

    def current_usage(self) -> int:
        self._rotate()
        return self.total


@dataclass
class KeyHealth:
    key_id: int
    api_key: str
    window: SlidingWindow
    upstream_base: str = ""
    upstream_model: str = ""
    model_name: str = ""
    rpm_limit: int = 0
    cooldown_until: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    recent_429_count: int = 0

    def is_available(self) -> bool:
        if time.time() < self.cooldown_until:
            return False
        if self.rpm_limit > 0 and self.window.current_usage() >= self.rpm_limit:
            return False
        return True