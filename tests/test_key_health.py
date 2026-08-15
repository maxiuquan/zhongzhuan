"""T07: key 健康度分类修正测试。

覆盖：
- 4xx 客户端错误（400/404/409/413/422）不再标记 key 健康度
- 认证/限流/5xx/网络四类仍生效
- 失败 3 次后成功 1 次 → consecutive_failures 归零、total_failures 保留、退避恢复
- handler 流式路径使用 classify_failure 返回值
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from zhongzhuan.proxy.ratelimit import (
    STATE_ERROR,
    STATE_HEALTHY,
    STATE_INVALID,
    STATE_RATE_LIMITED,
    KeyHealth,
    SlidingWindow,
)
from zhongzhuan.proxy.retry import (
    classify_failure,
    mark_success,
    mark_auth_failure,
    mark_rate_limited,
    mark_server_error,
    mark_network_failure,
)

# 请求侧错误（不应标记健康度）
CLIENT_ERRORS = [400, 404, 409, 413, 422]
# 认证类 / 限流类 / 服务端类 / 网络类
AUTH_CODES = [401, 403]
SERVER_ERRORS = [500, 502, 503, 504]


def _key() -> KeyHealth:
    return KeyHealth(
        key_id=1,
        api_key="sk-test",
        window=SlidingWindow(0, 0),
        upstream_model="gpt-4o",
    )


def _snapshot(k: KeyHealth) -> tuple:
    return (
        k.status,
        k.total_failures,
        k.consecutive_failures,
        k.cooldown_until,
        k.recent_429_count,
    )


@pytest.mark.parametrize("code", CLIENT_ERRORS)
def test_client_errors_do_not_mark_health(code):
    """4xx 客户端错误：不标记健康度，各维度不变。"""
    k = _key()
    before = _snapshot(k)
    retryable = classify_failure(k, code, {})
    assert retryable is False
    assert _snapshot(k) == before


@pytest.mark.parametrize("code", [401])
def test_auth_errors_mark_invalid(code):
    """认证/凭据类（401）：标记 invalid，等待管理端确认恢复（v1 不再自动到期）。"""
    k = _key()
    retryable = classify_failure(k, code, {})
    assert retryable is True
    assert k.status == STATE_INVALID
    assert k.total_failures == 1
    assert k.consecutive_failures == 1
    # v1（2026-08-15）：cooldown=0，invalid 由 is_available() 永久拒绝，
    # 直到「确认恢复」或服务重启——不再 1h 自动到期。
    assert k.cooldown_until == 0.0


@pytest.mark.parametrize("code", [403])
def test_bare_403_marks_transient(code):
    """裸 403（无 CF 特征）：按瞬时处理（v1，避免误伤非封禁的网关拒绝）。"""
    k = _key()
    retryable = classify_failure(k, code, {})
    assert retryable is True
    assert k.status == STATE_ERROR
    assert k.total_failures == 1


def test_403_cf_block_marks_banned():
    """带 CF/WAF 特征的 403：banned（长冷却 600s，自动恢复 + 可手动提前）。"""
    k = _key()
    retryable = classify_failure(k, 403, {"server": "cloudflare"})
    assert retryable is True
    assert k.status == STATE_ERROR
    assert k.cooldown_until > time.time() + 500


def test_rate_limited_marks_health():
    """限流类（429）：标记 rate_limited，尊重 Retry-After。"""
    k = _key()
    retryable = classify_failure(k, 429, {"Retry-After": "3"})
    assert retryable is True
    assert k.status == STATE_RATE_LIMITED
    assert k.total_failures == 1
    assert k.consecutive_failures == 1


@pytest.mark.parametrize("code", SERVER_ERRORS)
def test_server_error_marks_health(code):
    """5xx：标记 server error，短退避。"""
    k = _key()
    retryable = classify_failure(k, code, {})
    assert retryable is True
    assert k.status == STATE_ERROR
    assert k.total_failures == 1
    assert k.consecutive_failures == 1


def test_network_failure_marks_health():
    """网络类：标记 server error。"""
    k = _key()
    mark_network_failure(k)
    assert k.status == STATE_ERROR
    assert k.total_failures == 1
    assert k.consecutive_failures == 1


def test_fail3_then_success_resets_consecutive_keeps_total():
    """失败 3 次后成功 1 次 → consecutive 归零，total 保留，退避恢复。"""
    k = _key()
    for _ in range(3):
        mark_server_error(k)
    assert k.total_failures == 3
    assert k.consecutive_failures == 3
    assert k.status == STATE_ERROR
    assert k.cooldown_until > time.time()

    # 成功 1 次
    mark_success(k)
    assert k.consecutive_failures == 0
    assert k.total_failures == 3  # 累计失败保留
    assert k.status == STATE_HEALTHY
    assert k.cooldown_until == 0.0


def test_mark_failure_backwards_compat():
    """mark_failure 仍兼容（→ server-error 处理）。"""
    k = _key()
    from zhongzhuan.proxy.retry import mark_failure

    mark_failure(k)
    assert k.status == STATE_ERROR
    assert k.total_failures == 1


def test_keyhealth_reserved_fields():
    """v3 预留字段：capabilities / upstream_mode 存在且有默认值。"""
    k = _key()
    assert k.capabilities == set()
    assert k.upstream_mode == "bonded"
    # 可赋值
    k.capabilities.add("text_generation")
    assert "text_generation" in k.capabilities
