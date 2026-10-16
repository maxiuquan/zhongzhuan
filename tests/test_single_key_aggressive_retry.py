"""单 key 渠道激进重试：瞬时退避封顶单测（2026-10-16）。

背景：多 key 组的逐级退避（5s→10s→60s→600s）本质是"把流量让给其他 key"的
调度手段；单 key 渠道没有可让路的候选，冷却唯一的效果是把请求拒之门外
（线上实证：vsllm 上游 60s 掐线 → 唯一 key 连续失败逐级锁到 60s/600s，
期间所有请求直接 503 "all keys cooling"）。

契约：
1. transient 类失败（5xx / 网络异常 / 空响应 / unknown 盲区）在单 key 渠道
   （``single_key=True``）封顶 ``SINGLE_KEY_BACKOFF_MAX_LEVEL``（=1，10s 档），
   连续失败不逐级锁死；
2. 多 key 渠道（缺省 ``single_key=False``）行为不变：10s → 60s → 600s；
3. 429（rate_limited）**不**走单 key 封顶——立即重试只会更糟，继续尊重
   Retry-After / 逐级退避；
4. permanent / banned 等非 transient 分类同样不受影响。
"""

from __future__ import annotations

import time

from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow, backoff_seconds
from zhongzhuan.proxy.retry import (
    SINGLE_KEY_BACKOFF_MAX_LEVEL,
    classify_failure,
    classify_failure_labelled,
    mark_empty_response,
    mark_network_failure,
    mark_rate_limited,
    mark_server_error,
)


def _kh(key_id: int = 1) -> KeyHealth:
    return KeyHealth(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        window=SlidingWindow(60, 100),
        rpm_limit=100,
    )


class TestSingleKeyBackoffCap:
    def test_mark_server_error_single_key_caps_at_10s(self):
        k = _kh()
        for i in range(5):  # 连续 5 次失败：多 key 渠道此时早已锁 600s
            mark_server_error(k, single_key=True)
            remaining = k.cooldown_until - time.time()
            assert backoff_seconds(k.backoff_level) == 10
            assert 9.0 <= remaining <= 10.5, f"iter={i} remaining={remaining}"
        assert k.backoff_level == SINGLE_KEY_BACKOFF_MAX_LEVEL

    def test_mark_network_failure_single_key_caps_at_10s(self):
        k = _kh()
        for i in range(5):
            mark_network_failure(k, single_key=True)
            assert k.backoff_level <= SINGLE_KEY_BACKOFF_MAX_LEVEL
            remaining = k.cooldown_until - time.time()
            assert 9.0 <= remaining <= 10.5, f"iter={i} remaining={remaining}"

    def test_mark_empty_response_single_key_caps_at_10s(self):
        k = _kh()
        for i in range(5):
            mark_empty_response(k, single_key=True)
            assert k.backoff_level <= SINGLE_KEY_BACKOFF_MAX_LEVEL
            remaining = k.cooldown_until - time.time()
            assert 9.0 <= remaining <= 10.5, f"iter={i} remaining={remaining}"

    def test_multi_key_default_escalation_unchanged(self):
        """缺省（多 key）路径保持旧行为：10s → 60s → 600s 逐级。"""
        k = _kh()
        mark_server_error(k)
        assert k.backoff_level == 1 and backoff_seconds(1) == 10
        mark_server_error(k)
        assert k.backoff_level == 2 and backoff_seconds(2) == 60
        mark_server_error(k)
        mark_server_error(k)
        assert k.backoff_level == 3 and backoff_seconds(3) == 600


class TestClassifySingleKey:
    def test_500_single_key_caps_at_10s(self):
        k = _kh()
        retryable = classify_failure(k, 500, {}, b"internal error", single_key=True)
        assert retryable is True
        remaining = k.cooldown_until - time.time()
        assert 9.0 <= remaining <= 10.5

    def test_554_single_key_caps_at_10s(self):
        """554 是 vsllm 场景的实际状态码。"""
        k = _kh()
        retryable = classify_failure(k, 554, {}, b"gateway", single_key=True)
        assert retryable is True
        remaining = k.cooldown_until - time.time()
        assert 9.0 <= remaining <= 10.5

    def test_429_single_key_not_capped(self):
        """429 不走单 key 封顶：Retry-After 原样生效。"""
        k = _kh()
        retryable = classify_failure(k, 429, {"Retry-After": "300"}, b"", single_key=True)
        assert retryable is True
        remaining = k.cooldown_until - time.time()
        assert remaining > 200, f"429 cooldown should honor Retry-After, got {remaining:.0f}s"

    def test_401_single_key_still_invalid(self):
        k = _kh()
        retryable = classify_failure(k, 401, {}, b"unauthorized", single_key=True)
        assert not k.is_available()
        assert k.cooldown_until == 0.0

    def test_labelled_tuple_passthrough(self):
        k = _kh()
        retryable, label = classify_failure_labelled(k, 503, {}, b"oops", single_key=True)
        assert retryable is True and label == "transient"
        remaining = k.cooldown_until - time.time()
        assert 9.0 <= remaining <= 10.5
