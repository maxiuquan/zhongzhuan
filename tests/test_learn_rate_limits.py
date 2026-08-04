"""429 头学习测试：验证 learn_rate_limits 能正确解析 x-ratelimit-* 头。"""

import time

from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.proxy.retry import learn_rate_limits


def _kh(rpm_limit: int = 0, tpm_limit: int = 0) -> KeyHealth:
    return KeyHealth(
        key_id=1,
        api_key="sk-1",
        window=SlidingWindow(60, rpm_limit),
        rpm_limit=rpm_limit,
        tpm_limit=tpm_limit,
        tpm_window=SlidingWindow(60, tpm_limit) if tpm_limit > 0 else None,
    )


class TestLearnRateLimits:
    def test_retry_after_sets_cooldown(self):
        k = _kh()
        learn_rate_limits(k, {"retry-after": "30"}, 429)
        assert k.cooldown_until > time.time() + 25

    def test_retry_after_capped_at_600s(self):
        k = _kh()
        learn_rate_limits(k, {"retry-after": "9999"}, 429)
        assert k.cooldown_until <= time.time() + 601

    def test_x_ratelimit_limit_requests_tightens_rpm(self):
        k = _kh(rpm_limit=1000)
        learn_rate_limits(k, {"x-ratelimit-limit-requests": "50"}, 200)
        assert k.rpm_limit == 50  # 收紧到 50

    def test_x_ratelimit_limit_requests_ignored_if_higher(self):
        k = _kh(rpm_limit=30)
        learn_rate_limits(k, {"x-ratelimit-limit-requests": "100"}, 200)
        assert k.rpm_limit == 30  # 不放宽，只收紧

    def test_x_ratelimit_limit_tokens_tightens_tpm(self):
        k = _kh(tpm_limit=100000)
        learn_rate_limits(k, {"x-ratelimit-limit-tokens": "10000"}, 200)
        assert k.tpm_limit == 10000
        assert k.tpm_window is not None
        assert k.tpm_window.limit == 10000

    def test_x_ratelimit_limit_tokens_creates_tpm_window(self):
        """如果 key 原本没有 tpm_window，学习后自动创建。"""
        k = _kh(tpm_limit=0)
        learn_rate_limits(k, {"x-ratelimit-limit-tokens": "5000"}, 200)
        assert k.tpm_limit == 5000
        assert k.tpm_window is not None
        assert k.tpm_window.limit == 5000

    def test_remaining_tokens_syncs_usage(self):
        k = _kh(tpm_limit=10000)
        # remaining=8000 → used=2000
        learn_rate_limits(
            k,
            {
                "x-ratelimit-limit-tokens": "10000",
                "x-ratelimit-remaining-tokens": "8000",
            },
            200,
        )
        assert k.tpm_window.current_usage() == 2000

    def test_remaining_requests_syncs_usage(self):
        k = _kh(rpm_limit=100)
        # remaining=80 → used=20
        learn_rate_limits(
            k,
            {
                "x-ratelimit-limit-requests": "100",
                "x-ratelimit-remaining-requests": "80",
            },
            200,
        )
        assert k.window.current_usage() == 20

    def test_header_lookup_case_insensitive(self):
        """验证 _lower_headers 正确处理大小写。"""
        k = _kh(rpm_limit=100)
        learn_rate_limits(k, {"X-RateLimit-Limit-Requests": "50"}, 200)
        assert k.rpm_limit == 50

    def test_invalid_header_values_ignored(self):
        k = _kh(rpm_limit=100)
        learn_rate_limits(
            k,
            {
                "x-ratelimit-limit-requests": "not-a-number",
                "retry-after": "also-not-a-number",
            },
            200,
        )
        assert k.rpm_limit == 100  # 未改变
