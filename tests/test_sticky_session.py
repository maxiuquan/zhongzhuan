"""Sticky session 测试：验证会话指纹提取和粘性路由逻辑。"""
import json
import time

import pytest
from aiohttp import web

from zhongzhuan.proxy.handler import ProxyHandler
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow


def _make_keys(n: int = 3) -> list[KeyHealth]:
    return [
        KeyHealth(
            key_id=i, api_key=f"sk-{i}",
            window=SlidingWindow(60, 1000),
            rpm_limit=1000,
            model_name=f"model-{i}",
            upstream_base="https://upstream.example.com",
        )
        for i in range(1, n + 1)
    ]


def _make_handler(keys: list[KeyHealth] | None = None) -> ProxyHandler:
    return ProxyHandler(
        clients={},
        keys=keys or _make_keys(),
        sticky_ttl=1800.0,
    )


def _make_request(headers: dict | None = None) -> web.Request:
    """构造一个最小化的 aiohttp Request mock。"""
    class _MockRequest:
        def __init__(self, hdrs: dict | None = None) -> None:
            self.headers = hdrs or {}
    return _MockRequest(headers)


class TestSessionKeyExtraction:
    def test_x_session_id_header(self):
        req = _make_request({"x-session-id": "abc123"})
        key = ProxyHandler._session_key(req, None)
        assert key == "hdr:abc123"

    def test_x_zhongzhuan_session_header(self):
        req = _make_request({"x-zhongzhuan-session": "sess-456"})
        key = ProxyHandler._session_key(req, None)
        assert key == "hdr:sess-456"

    def test_x_request_id_header(self):
        req = _make_request({"x-request-id": "req-789"})
        key = ProxyHandler._session_key(req, None)
        assert key == "hdr:req-789"

    def test_header_priority(self):
        """x-session-id 优先于 x-zhongzhuan-session 和 x-request-id。"""
        req = _make_request({
            "x-session-id": "first",
            "x-zhongzhuan-session": "second",
            "x-request-id": "third",
        })
        key = ProxyHandler._session_key(req, None)
        assert key == "hdr:first"

    def test_messages_hash_fallback(self):
        """无 header 时从 messages 首轮稳定指纹生成会话指纹（T35 / R-P1-61）。

        用**第一条 user 消息**的归一化指纹（``fp:`` 前缀），而不是滚动消息
        尾部 —— 这样多轮会话的 fingerprint 恒定，粘性路由才能持续命中同一 key。
        """
        req = _make_request()
        body = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ]
        }
        key = ProxyHandler._session_key(req, body)
        assert key.startswith("fp:")
        assert len(key) > 10  # SHA-256 前 16 字符

    def test_messages_hash_stable(self):
        """相同的 messages 生成相同的会话指纹。"""
        req1 = _make_request()
        req2 = _make_request()
        body = {
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ]
        }
        key1 = ProxyHandler._session_key(req1, body)
        key2 = ProxyHandler._session_key(req2, body)
        assert key1 == key2

    def test_single_message_returns_fingerprint(self):
        """只有一条消息也能生成首轮稳定指纹（T35 / R-P1-61 判据③）。

        旧行为「只有一条消息不生成指纹」基于「滚动尾部需要 ≥2 条」的假设；
        首轮指纹取第一条 user 消息，单条消息天然就是首轮，因此可以生成指纹。
        """
        req = _make_request()
        body = {"messages": [{"role": "user", "content": "Hello"}]}
        key = ProxyHandler._session_key(req, body)
        assert key.startswith("fp:")

    def test_first_turn_fingerprint_stable_across_turns(self):
        """10 轮会话内容各异，但 fingerprint 恒定（T35 / R-P1-61 判据③）。

        只要第一条 user 消息不变，后续追加的 assistant/user 轮次都不影响指纹。
        """
        req = _make_request()
        first = ProxyHandler._session_key(req, {
            "messages": [
                {"role": "user", "content": "帮我写一个冒泡排序"},
                {"role": "assistant", "content": "好的"},
            ]
        })
        for i in range(10):
            body = {
                "messages": [
                    {"role": "user", "content": "帮我写一个冒泡排序"},
                    {"role": "assistant", "content": f"第 {i} 轮回复"},
                    {"role": "user", "content": f"继续优化第 {i} 轮"},
                ]
            }
            assert ProxyHandler._session_key(req, body) == first

    def test_reasoning_does_not_affect_fingerprint(self):
        """reasoning 内容不参与指纹计算（T35 / R-P0-14 判据④）。

        两条仅 reasoning 不同的请求，fingerprint 必须相同。
        """
        req = _make_request()
        base = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "回复", "reasoning": "思考过程A"},
            ]
        }
        other = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "回复", "reasoning": "完全不同的思考过程B"},
            ]
        }
        assert ProxyHandler._session_key(req, base) == ProxyHandler._session_key(req, other)

    def test_input_reasoning_items_skipped(self):
        """Responses 输入里 ``role=reasoning`` 的项被跳过（R-P0-14）。"""
        req = _make_request()
        key1 = ProxyHandler._session_key(req, {
            "input": [
                {"role": "user", "content": "你好"},
                {"role": "reasoning", "content": "推理内容A"},
            ]
        })
        key2 = ProxyHandler._session_key(req, {
            "input": [
                {"role": "user", "content": "你好"},
                {"role": "reasoning", "content": "推理内容B"},
            ]
        })
        assert key1 == key2

    def test_conversation_field_priority(self):
        """显式 conversation / previous_response_id 优先于首轮指纹（R-P1-61）。"""
        req = _make_request()
        body = {
            "conversation": "conv_abc",
            "input": [{"role": "user", "content": "你好"}],
        }
        assert ProxyHandler._session_key(req, body) == "id:conv_abc"
        body2 = {
            "previous_response_id": "resp_123",
            "input": [{"role": "user", "content": "你好"}],
        }
        assert ProxyHandler._session_key(req, body2) == "id:resp_123"

    def test_no_messages_returns_empty(self):
        req = _make_request()
        key = ProxyHandler._session_key(req, {})
        assert key == ""


class TestStickyRouting:
    def test_set_and_get_sticky(self):
        handler = _make_handler()
        keys = handler._keys
        handler._set_sticky("hdr:session1", keys[0].key_id)
        sticky_k = handler._get_sticky_key("hdr:session1", keys)
        assert sticky_k is not None
        assert sticky_k.key_id == keys[0].key_id

    def test_expired_sticky_returns_none(self):
        handler = _make_handler()
        keys = handler._keys
        # 设置一个已过期的 sticky 条目
        handler._sticky["hdr:expired"] = (keys[0].key_id, time.time() - 1)
        sticky_k = handler._get_sticky_key("hdr:expired", keys)
        assert sticky_k is None

    def test_sticky_key_unavailable_returns_none(self):
        """如果 sticky key 已不可用（如 invalid），返回 None 让调度器选其他 key。"""
        from zhongzhuan.proxy.retry import mark_auth_failure
        handler = _make_handler()
        keys = handler._keys
        handler._set_sticky("hdr:session1", keys[0].key_id)
        mark_auth_failure(keys[0])  # 标记为 invalid
        sticky_k = handler._get_sticky_key("hdr:session1", keys)
        assert sticky_k is None

    def test_unknown_session_returns_none(self):
        handler = _make_handler()
        keys = handler._keys
        sticky_k = handler._get_sticky_key("hdr:unknown", keys)
        assert sticky_k is None

    def test_empty_session_key_not_stored(self):
        """空会话指纹不应被存储。"""
        handler = _make_handler()
        handler._set_sticky("", 1)
        assert "" not in handler._sticky

    def test_sticky_cleanup_on_overflow(self):
        """超过 256 个条目时触发清理。"""
        handler = _make_handler()
        keys = handler._keys
        # 填入 256 个过期条目
        for i in range(256):
            handler._sticky[f"hdr:sess{i}"] = (keys[0].key_id, time.time() - 100)
        # 再 set 一个，触发清理
        handler._set_sticky("hdr:trigger", keys[0].key_id)
        # 过期条目应被清理
        assert len(handler._sticky) < 256
        assert "hdr:trigger" in handler._sticky


class TestSchedulerFallbackPenalty:
    """验证兜底 key 在调度器中被降权。"""
    def test_fallback_key_score_lower_than_normal(self):
        from zhongzhuan.proxy.scheduler import score
        normal = KeyHealth(
            key_id=1, api_key="sk-1",
            window=SlidingWindow(60, 1000),
            rpm_limit=1000,
        )
        fallback = KeyHealth(
            key_id=-1, api_key="public",
            window=SlidingWindow(60, 0),
            is_fallback=True,
            fallback_penalty=0.1,  # 可配置降权系数，默认 0.1
        )
        normal_score = score(normal)
        fallback_score = score(fallback)
        assert normal_score > 0
        assert fallback_score > 0
        assert fallback_score < normal_score * 0.2  # 降权到 ~10%

    def test_fallback_penalty_configurable_no_penalty(self):
        """fallback_penalty=1.0 时不降权，兜底 key 与普通 key 同等评分。"""
        from zhongzhuan.proxy.scheduler import score
        normal = KeyHealth(
            key_id=1, api_key="sk-1",
            window=SlidingWindow(60, 1000),
            rpm_limit=1000,
        )
        fallback = KeyHealth(
            key_id=-1, api_key="public",
            window=SlidingWindow(60, 0),
            is_fallback=True,
            fallback_penalty=1.0,  # 不降权
        )
        normal_score = score(normal)
        fallback_score = score(fallback)
        # 不降权时两者分数接近（都在 0.9-1.0 区间，仅随机扰动不同）
        assert fallback_score > 0.8
        assert abs(fallback_score - normal_score) < 0.1
