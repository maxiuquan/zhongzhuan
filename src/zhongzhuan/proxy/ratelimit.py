"""Rate limiter: sliding window + Key health."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


class SlidingWindow:
    """60x 1s buckets with circular deque; O(window_seconds) at most per rotate."""

    def __init__(self, window_seconds: int = 60, limit: int = 0) -> None:
        self.window_seconds = window_seconds
        self.limit = limit  # 0 = unlimited
        self.buckets: deque[int] = deque([0] * window_seconds, maxlen=window_seconds)
        self.total: int = 0
        self.last_rotate: float = time.time()

    def _rotate(self) -> None:
        now = time.time()
        elapsed = int(now - self.last_rotate)
        if elapsed <= 0:
            return
        if elapsed >= self.window_seconds:
            self.buckets = deque([0] * self.window_seconds, maxlen=self.window_seconds)
            self.total = 0
            self.last_rotate = now
            return
        for _ in range(elapsed):
            self.total -= self.buckets.popleft()  # O(1) with deque
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
    model_id: int = 0
    upstream_base: str = ""
    upstream_model: str = ""
    model_name: str = ""
    rpm_limit: int = 0
    cooldown_until: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    recent_429_count: int = 0
    upstream_protocol: str = "openai"  # "openai" | "anthropic"
    anthropic_version: str = "2023-06-01"
    max_tokens_default: int = 4096
    # 上游完整地址覆盖：非空时直接用作请求路径/URL，不自动拼接 /v1/chat/completions 等
    upstream_path_override: str = ""

    def is_available(self) -> bool:
        if time.time() < self.cooldown_until:
            return False
        if self.rpm_limit > 0 and self.window.current_usage() >= self.rpm_limit:
            return False
        return True