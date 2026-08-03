"""Rate limiter: sliding window + Key health state machine."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from .protocol.responses_models import Capability, ExecutionMode


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

    def add(self, n: int) -> None:
        """Record usage without checking the limit (post-hoc accounting)."""
        self._rotate()
        if n <= 0:
            return
        self.buckets[-1] += n
        self.total += n

    def current_usage(self) -> int:
        self._rotate()
        return self.total


# ---- Key health states ----
STATE_HEALTHY = "healthy"
STATE_RATE_LIMITED = "rate_limited"
STATE_INVALID = "invalid"
STATE_ERROR = "error"


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
    # TPM (tokens per minute) rate limiting
    tpm_limit: int = 0
    tpm_window: SlidingWindow | None = None
    # RPD (requests per day) counting — in-memory, rolling 86400s window
    rpd_limit: int = 0
    rpd_count: int = 0
    rpd_reset_at: float = 0.0
    # Health state machine
    status: str = STATE_HEALTHY
    cooldown_until: float = 0.0
    success_count: int = 0
    # 累计失败次数（永不因成功而清零，用于观测/告警）
    total_failures: int = 0
    # 连续失败次数（成功即清零，退避判断依据，T07）
    consecutive_failures: int = 0
    recent_429_count: int = 0
    # v3 能力路由字段（T07 建字段，T25 接 CapabilityRouter）。
    #
    # DEVIATION（§3.9 / T25）：文档把它们写成 ``frozenset[Capability]`` 与
    # ``ExecutionMode``，但 T07 已经落地为宽松的字符串形态，且配置 / DB / 管理端
    # 都按字符串读写。改动类型会连带改掉既有构造点与 T07 的字段契约，收益为零。
    # 因此**保留字符串存储**，由 :meth:`declared_capabilities` /
    # :meth:`execution_mode` 提供强类型视图 —— 路由器只消费后者，永远看不到裸
    # 字符串，文档要求的类型安全在使用侧完整成立。
    capabilities: set[str] = field(default_factory=set)
    upstream_mode: str = "bonded"  # "bonded" | "native" | "emulate" | "translate"
    upstream_protocol: str = "openai"  # "openai" | "anthropic"
    anthropic_version: str = "2023-06-01"
    max_tokens_default: int = 4096
    # 上游完整地址覆盖：非空时直接用作请求路径/URL，不自动拼接 /v1/chat/completions 等
    upstream_path_override: str = ""
    # 兜底上游标志：True 表示这是无 key 时的免费兜底 key，调度器按 fallback_penalty 降权
    is_fallback: bool = False
    # 兜底降权系数：is_fallback=True 时生效，1.0 表示不降权（由 config.fallback.fallback_penalty 注入）
    fallback_penalty: float = 1.0
    # 模型别名：逗号分隔，客户端用别名请求时也能匹配到此 key
    aliases: str = ""

    def declared_capabilities(self) -> frozenset[Capability]:
        """本 key 声明的上游能力（强类型视图，T25）。

        无法识别的名字被忽略：一个拼错的能力名不该让 key 整体不可用，它只会
        表现为「该能力没人声明」，随后被启动期缺口报告如实抓出来。
        """
        out: set[Capability] = set()
        for raw in self.capabilities or ():
            if isinstance(raw, Capability):
                out.add(raw)
                continue
            try:
                out.add(Capability(str(raw).strip().lower()))
            except ValueError:
                continue
        return frozenset(out)

    def execution_mode(self) -> ExecutionMode:
        """``upstream_mode`` 的强类型视图；未声明（``bonded``）按最保守的
        :attr:`ExecutionMode.TRANSLATE` 处理 —— 没声明原生能力就不假设有。"""
        text = (self.upstream_mode or "").strip().lower()
        if text in ("native", "responses_native"):
            return ExecutionMode.NATIVE
        if text == "emulate":
            return ExecutionMode.EMULATE
        return ExecutionMode.TRANSLATE

    def _maybe_reset_rpd(self) -> None:
        now = time.time()
        if self.rpd_reset_at <= now:
            self.rpd_count = 0
            self.rpd_reset_at = now + 86400

    def is_available(self) -> bool:
        # Invalid keys (401/403) are never retried — avoids wasting requests
        if self.status == STATE_INVALID:
            return False
        if time.time() < self.cooldown_until:
            return False
        # RPM check
        if self.rpm_limit > 0 and self.window.current_usage() >= self.rpm_limit:
            return False
        # TPM check
        if self.tpm_limit > 0 and self.tpm_window is not None:
            if self.tpm_window.current_usage() >= self.tpm_limit:
                return False
        # RPD check
        if self.rpd_limit > 0:
            self._maybe_reset_rpd()
            if self.rpd_count >= self.rpd_limit:
                return False
        return True

    def record_request(self) -> None:
        """Call when a request is dispatched to this key (for RPD counting)."""
        if self.rpd_limit > 0:
            self._maybe_reset_rpd()
            self.rpd_count += 1

    def record_tokens(self, tokens_in: int, tokens_out: int) -> None:
        """Record token usage for TPM tracking (called after a successful response)."""
        total = tokens_in + tokens_out
        if total > 0 and self.tpm_window is not None:
            self.tpm_window.add(total)
