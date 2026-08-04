"""T35 / R-P1-61 + R-P0-14: 粘性会话稳定指纹 + session→route binding 持久化。

判据映射（架构文档 §T35 完成判据）：
- 判据③: 10 轮会话内容各异，断言 fingerprint 恒定、命中同一 key。
- 判据④: 同一会话两轮 reasoning 不同但 session hash 相同。
- 判据⑤: Responses 在 ResponseStore 持久化 session→route binding
  （TTL + 能力校验 + 故障迁移记录）。
- 判据⑥: sticky 仅在选定模型健康且能力兼容时生效。

设计约束
--------
* **测试零真实等待**：sticky TTL 使用可注入时钟（``handler._now``）。
* 能力校验通过 ``tools[N].type``（hosted tool）+ ``background`` +
  ``metadata.stateful_responses`` 提取。
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from aiohttp import web

from zhongzhuan.config import default_config
from zhongzhuan.proxy.handler import ProxyHandler
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.store.response_store import ResponseStore
from zhongzhuan.store.store import create_store


class FakeClock:
    """可注入时钟：测试通过 ``clock.t += n`` 推进时间，零真实等待。

    默认从**当前真实时间**起步：binding 持久化用 ``self._now()`` 生成
    ``expires_at``，而 store 读回时用真实 ``time.time()`` 判 TTL —— 若从 1970
    起步，所有 binding 都会被误判为早已过期。
    """

    def __init__(self, start: float | None = None) -> None:
        import time as _t
        self.t = start if start is not None else _t.time()

    def __call__(self) -> float:
        return self.t


def _make_key(
    key_id: int, model_name: str = "model", *,
    capabilities: set[str] | None = None,
) -> KeyHealth:
    return KeyHealth(
        key_id=key_id, api_key=f"sk-{key_id}",
        window=SlidingWindow(60, 1000),
        rpm_limit=1000,
        model_name=model_name,
        upstream_base="https://upstream.example.com",
        capabilities=capabilities or set(),
    )


def _make_handler(
    keys: list[KeyHealth] | None = None,
    *,
    store=None,
    sticky_ttl: float = 1800.0,
) -> ProxyHandler:
    h = ProxyHandler(
        clients={},
        keys=keys or [_make_key(1), _make_key(2)],
        store=store,
        sticky_ttl=sticky_ttl,
    )
    h._now = FakeClock()
    return h


def _make_request(headers: dict | None = None) -> web.Request:
    class _MockRequest:
        def __init__(self, hdrs: dict | None = None) -> None:
            self.headers = hdrs or {}
    return _MockRequest(headers)


@pytest_asyncio.fixture
async def store(tmp_path):
    cfg = default_config()
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    cfg.storage.db_path = cfg.storage.sqlite_db_path
    cfg.tidb = None
    s = await create_store(cfg)
    yield s
    await s.close()


# --------------------------------------------------------------------------- #
# 判据③：10 轮会话内容各异，fingerprint 恒定、命中同一 key
# --------------------------------------------------------------------------- #

class TestStableFingerprint:
    def test_fingerprint_stable_across_10_turns(self):
        """10 轮会话内容各异，fingerprint 恒定（首轮稳定指纹）。"""
        req = _make_request()
        body = {
            "messages": [
                {"role": "user", "content": "帮我写一个冒泡排序"},
            ]
        }
        first = ProxyHandler._session_key(req, body)
        assert first.startswith("fp:")
        for i in range(10):
            turn = {
                "messages": [
                    {"role": "user", "content": "帮我写一个冒泡排序"},
                    {"role": "assistant", "content": f"第 {i} 轮回复"},
                    {"role": "user", "content": f"继续优化第 {i} 轮"},
                ]
            }
            assert ProxyHandler._session_key(req, turn) == first

    def test_10_turns_hit_same_key(self):
        """10 轮会话命中同一 key（sticky 生效）。"""
        handler = _make_handler([_make_key(1), _make_key(2)])
        body = {
            "messages": [
                {"role": "user", "content": "帮我写一个冒泡排序"},
            ]
        }
        session = ProxyHandler._session_key(_make_request(), body)
        handler._set_sticky(session, 1)
        for i in range(10):
            turn = {
                "messages": [
                    {"role": "user", "content": "帮我写一个冒泡排序"},
                    {"role": "assistant", "content": f"第 {i} 轮回复"},
                    {"role": "user", "content": f"继续优化第 {i} 轮"},
                ]
            }
            # 每一轮用同一 session key 查 sticky —— 必须命中 key 1。
            sticky = handler._get_sticky_key(session, handler._keys, turn)
            assert sticky is not None
            assert sticky.key_id == 1


# --------------------------------------------------------------------------- #
# 判据④：同一会话两轮 reasoning 不同但 session hash 相同
# --------------------------------------------------------------------------- #

class TestReasoningExcludedFromFingerprint:
    def test_messages_reasoning_field_does_not_change_hash(self):
        """``messages`` 里 assistant 的 ``reasoning`` 字段不参与指纹。"""
        req = _make_request()
        base = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "回复",
                 "reasoning": "内部思考 A"},
            ]
        }
        other = {
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "回复",
                 "reasoning": "完全不同的内部思考 B"},
            ]
        }
        assert ProxyHandler._session_key(req, base) == ProxyHandler._session_key(req, other)

    def test_input_reasoning_items_do_not_change_hash(self):
        """Responses ``input`` 里 ``role=reasoning`` 的项不参与指纹。"""
        req = _make_request()
        key_a = ProxyHandler._session_key(req, {
            "input": [
                {"role": "user", "content": "你好"},
                {"role": "reasoning", "content": "推理过程 A"},
                {"role": "assistant", "content": "答复"},
            ]
        })
        key_b = ProxyHandler._session_key(req, {
            "input": [
                {"role": "user", "content": "你好"},
                {"role": "reasoning", "content": "推理过程 B"},
                {"role": "assistant", "content": "答复"},
            ]
        })
        assert key_a == key_b

    def test_reasoning_before_first_user_is_skipped(self):
        """reasoning 项出现在首条 user 消息之前时仍被跳过（变异真正命中路径）。

        这是变异测试的关键场景：若实现「遇到 reasoning 就终止」，前导 reasoning
        会直接污染指纹；正确实现必须跳过它、继续找第一条 ``role=user``。
        """
        req = _make_request()
        key_a = ProxyHandler._session_key(req, {
            "input": [
                {"role": "reasoning", "content": "推理过程 A"},
                {"role": "user", "content": "你好"},
            ]
        })
        key_b = ProxyHandler._session_key(req, {
            "input": [
                {"role": "reasoning", "content": "推理过程 B"},
                {"role": "user", "content": "你好"},
            ]
        })
        assert key_a == key_b
        assert key_a.startswith("fp:")

    def test_session_key_equality_means_same_routing(self):
        """session hash 相同 ⇒ 同一会话 ⇒ 命中同一 key（判据④完整链路）。"""
        handler = _make_handler([_make_key(1), _make_key(2)])
        body_a = {
            "input": [
                {"role": "user", "content": "分析这份报告"},
                {"role": "reasoning", "content": "步骤 A"},
            ]
        }
        body_b = {
            "input": [
                {"role": "user", "content": "分析这份报告"},
                {"role": "reasoning", "content": "步骤 B"},
            ]
        }
        key_a = ProxyHandler._session_key(_make_request(), body_a)
        key_b = ProxyHandler._session_key(_make_request(), body_b)
        assert key_a == key_b
        handler._set_sticky(key_a, 2)
        sticky_b = handler._get_sticky_key(key_b, handler._keys, body_b)
        assert sticky_b is not None
        assert sticky_b.key_id == 2


# --------------------------------------------------------------------------- #
# 判据⑤：ResponseStore 持久化 session→route binding（TTL + 能力 + 故障迁移）
# --------------------------------------------------------------------------- #

class TestRouteBindingPersistence:
    @pytest.mark.asyncio
    async def test_binding_persisted_and_readable(self, store):
        """binding 写入 ResponseStore 后可读回（判据⑤持久化）。"""
        import time as _t
        rs = ResponseStore(store)
        await rs.upsert_route_binding(
            session_key="hdr:abc",
            key_id=7,
            capabilities={"web_search", "background"},
            expires_at=int(_t.time()) + 3600,
        )
        rec = await rs.get_route_binding("hdr:abc")
        assert rec is not None
        assert rec["key_id"] == 7
        assert set(rec["capabilities"]) == {"web_search", "background"}
        assert rec["expires_at"] > _t.time()

    @pytest.mark.asyncio
    async def test_binding_ttl_expiry(self, store):
        """过期 binding 视为不存在并懒删除（TTL）。"""
        rs = ResponseStore(store)
        await rs.upsert_route_binding(
            session_key="hdr:exp", key_id=1, expires_at=100,
        )
        # 当前真实时间必然 > 100，直接读应返回 None。
        rec = await rs.get_route_binding("hdr:exp")
        assert rec is None
        # 行已被懒删除
        row = await store.fetchone(
            "SELECT 1 FROM route_bindings WHERE session_key = ?", ("hdr:exp",),
        )
        assert row is None

    @pytest.mark.asyncio
    async def test_binding_failover_record(self, store):
        """故障迁移记录：failover_count 递增 + reason 记录。"""
        rs = ResponseStore(store)
        await rs.upsert_route_binding(session_key="hdr:abc", key_id=1)
        await rs.record_binding_failover("hdr:abc", reason="capability mismatch")
        rec = await rs.get_route_binding("hdr:abc")
        assert rec["failover_count"] == 1
        assert rec["last_failover_reason"] == "capability mismatch"
        await rs.record_binding_failover("hdr:abc", reason="health check failed")
        rec = await rs.get_route_binding("hdr:abc")
        assert rec["failover_count"] == 2

    @pytest.mark.asyncio
    async def test_binding_upsert_resets_failover(self, store):
        """重新绑定（成功响应）清零 failover 计数。"""
        rs = ResponseStore(store)
        await rs.upsert_route_binding(session_key="hdr:abc", key_id=1)
        await rs.record_binding_failover("hdr:abc", reason="boom")
        rec = await rs.get_route_binding("hdr:abc")
        assert rec["failover_count"] == 1
        await rs.upsert_route_binding(session_key="hdr:abc", key_id=1)
        rec = await rs.get_route_binding("hdr:abc")
        assert rec["failover_count"] == 0
        assert rec["last_failover_reason"] == ""

    @pytest.mark.asyncio
    async def test_restore_sticky_from_store(self, store):
        """进程重启后从 ResponseStore 恢复 sticky（判据⑤跨进程连续性）。"""
        rs = ResponseStore(store)
        await rs.upsert_route_binding(session_key="hdr:abc", key_id=2)
        handler = _make_handler([_make_key(1), _make_key(2)], store=store)
        session_key = "hdr:abc"
        await handler._restore_sticky_from_store(session_key)
        sticky = handler._get_sticky_key(session_key, handler._keys)
        assert sticky is not None
        assert sticky.key_id == 2


# --------------------------------------------------------------------------- #
# 判据⑥：sticky 仅在选定模型健康且能力兼容时生效
# --------------------------------------------------------------------------- #

class TestStickyCapabilityCheck:
    def test_sticky_hits_when_healthy_and_compatible(self):
        """健康且能力兼容 → sticky 生效。"""
        handler = _make_handler([
            _make_key(1, capabilities={"web_search"}),
            _make_key(2),
        ])
        handler._set_sticky("hdr:s", 1, frozenset({"web_search"}))
        body = {"tools": [{"type": "web_search"}]}
        sticky = handler._get_sticky_key("hdr:s", handler._keys, body)
        assert sticky is not None
        assert sticky.key_id == 1

    def test_sticky_invalid_when_capability_mismatch(self):
        """能力不兼容 → sticky 失效（判据⑥），并记录故障迁移原因。"""
        handler = _make_handler([
            _make_key(1, capabilities={"file_search"}),
            _make_key(2),
        ])
        handler._set_sticky("hdr:s", 1, frozenset({"file_search"}))
        body = {"tools": [{"type": "web_search"}]}
        sticky = handler._get_sticky_key("hdr:s", handler._keys, body)
        assert sticky is None
        assert "hdr:s" in handler._binding_failover_reasons
        assert "capability mismatch" in handler._binding_failover_reasons["hdr:s"]

    def test_sticky_invalid_when_unhealthy(self):
        """模型不健康 → sticky 失效（原有行为，判据⑥健康维度）。"""
        from zhongzhuan.proxy.retry import mark_auth_failure
        handler = _make_handler([_make_key(1), _make_key(2)])
        handler._set_sticky("hdr:s", 1)
        mark_auth_failure(handler._keys[0])
        sticky = handler._get_sticky_key("hdr:s", handler._keys)
        assert sticky is None

    def test_sticky_without_caps_treats_any_compatible(self):
        """绑定未记录能力时不做能力判定（兼容任何请求）。"""
        handler = _make_handler([_make_key(1), _make_key(2)])
        handler._set_sticky("hdr:s", 1)  # 旧式绑定，无能力记录
        body = {"tools": [{"type": "web_search"}]}
        sticky = handler._get_sticky_key("hdr:s", handler._keys, body)
        assert sticky is not None
        assert sticky.key_id == 1

    def test_sticky_ttl_expiry(self):
        """TTL 过期 → sticky 失效（可注入时钟，零真实等待）。"""
        handler = _make_handler([_make_key(1), _make_key(2)], sticky_ttl=1800.0)
        handler._set_sticky("hdr:s", 1)
        clock: FakeClock = handler._now
        # 推进超过 TTL
        clock.t += 2000
        sticky = handler._get_sticky_key("hdr:s", handler._keys)
        assert sticky is None

    def test_required_capabilities_extraction(self):
        """能力提取：hosted tool / background / metadata.stateful_responses。"""
        handler = _make_handler()
        body = {
            "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "f"},  # 普通 function 不算
                {"type": "code_interpreter"},
            ],
            "background": True,
            "metadata": {"stateful_responses": True},
        }
        caps = handler._required_capabilities(body)
        assert "web_search" in caps
        assert "code_interpreter" in caps
        assert "background" in caps
        assert "stateful_responses" in caps
        assert "function" not in caps


# --------------------------------------------------------------------------- #
# 判据⑤ 补充：handler 层 binding 持久化链路（store 存在时）
# --------------------------------------------------------------------------- #

class TestHandlerBindingPersistence:
    @pytest.mark.asyncio
    async def test_persist_sticky_binding_writes_store(self, store):
        """成功响应后 binding 落库（handler 层）。"""
        handler = _make_handler([_make_key(1), _make_key(2)], store=store)
        await handler._persist_sticky_binding(
            "hdr:abc", 1, frozenset({"web_search"}),
        )
        rs = ResponseStore(store)
        rec = await rs.get_route_binding("hdr:abc")
        assert rec is not None
        assert rec["key_id"] == 1
        assert set(rec["capabilities"]) == {"web_search"}
        assert rec["expires_at"] > 0

    @pytest.mark.asyncio
    async def test_failover_flush_writes_store(self, store):
        """sticky 被拒后故障迁移原因落库（handler 层）。"""
        handler = _make_handler([_make_key(1), _make_key(2)], store=store)
        handler._binding_failover_reasons["hdr:abc"] = "capability mismatch"
        await handler._persist_sticky_failover("hdr:abc")
        rs = ResponseStore(store)
        # 先建 binding 再查 failover（record 只 UPDATE 已存在的行）
        await rs.upsert_route_binding(session_key="hdr:abc", key_id=1)
        # 由于 record 在 upsert 之前执行过且行不存在，重跑一次以验证语义
        handler._binding_failover_reasons["hdr:abc"] = "capability mismatch"
        await handler._persist_sticky_failover("hdr:abc")
        rec = await rs.get_route_binding("hdr:abc")
        assert rec["failover_count"] == 1
        assert rec["last_failover_reason"] == "capability mismatch"
