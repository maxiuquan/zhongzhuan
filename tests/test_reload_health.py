"""reload_keys() 健康状态保留策略测试（2026-08-20，P1）。

背景：长跑进程 reload 曾"无脑保留"旧 key 的瞬态冷却状态，配合失败不入库，
造成 "no enabled keys" 静默 503（线上根因）。本次修复后 reload 只保留：
  * permanent（invalid）—— 等管理端「确认恢复」，不因 reload 复活；
  * 未过期的瞬态冷却（error / rate_limited + cooldown_until > now）；
已过期的瞬态冷却归位 healthy（上游恢复后自动回池）。
"""

import time

from zhongzhuan.proxy.handler import ProxyHandler
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow, STATE_ERROR, STATE_HEALTHY, STATE_INVALID
from zhongzhuan.proxy.retry import mark_auth_failure, mark_server_error


def _kh(key_id: int, model_name: str = "m") -> KeyHealth:
    return KeyHealth(
        key_id=key_id,
        api_key=f"sk-{key_id}",
        model_id=key_id,
        model_name=model_name,
        window=SlidingWindow(60, 1000),
        rpm_limit=1000,
    )


def _handler(old_keys: list[KeyHealth], new_keys: list[KeyHealth]) -> ProxyHandler:
    h = ProxyHandler(clients={}, keys=old_keys, groups=None)

    async def _load():
        return [k for k in new_keys]  # 返回新副本，模拟 DB 重载

    h._load_keys_fn = _load
    return h


async def test_reload_preserves_unexpired_transient_cooldown():
    """未过期瞬态冷却 → reload 后保留 error + cooldown。"""
    old = _kh(1)
    mark_server_error(old)  # error + backoff 冷却
    # 保证冷却未过期（mark_server_error 是 5s 档，立即 reload 必未过期）
    new = _kh(1)
    h = _handler([old], [new])
    await h.reload_keys()
    nk = h._keys[0]
    assert nk.status == STATE_ERROR
    assert nk.cooldown_until > time.time()


async def test_reload_resets_expired_transient_cooldown():
    """已过期瞬态冷却 → reload 归位 healthy（上游恢复自动回池）。"""
    old = _kh(1)
    mark_server_error(old)
    old.cooldown_until = time.time() - 1  # 强制冷却过期
    old.status = STATE_ERROR
    new = _kh(1)
    h = _handler([old], [new])
    await h.reload_keys()
    assert h._keys[0].status == STATE_HEALTHY
    assert h._keys[0].cooldown_until == 0.0


async def test_reload_keeps_invalid_permanent():
    """永久失效（invalid）→ reload 不复活，保持 invalid。"""
    old = _kh(1)
    mark_auth_failure(old)  # STATE_INVALID
    new = _kh(1)
    h = _handler([old], [new])
    await h.reload_keys()
    assert h._keys[0].status == STATE_INVALID


async def test_reload_healthy_key_stays_healthy():
    """健康 key reload 后仍健康（不受影响）。"""
    old = _kh(1)
    new = _kh(1)
    h = _handler([old], [new])
    await h.reload_keys()
    assert h._keys[0].status == STATE_HEALTHY
