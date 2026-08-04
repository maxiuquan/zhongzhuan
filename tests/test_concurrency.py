"""T34 acceptance — concurrency governance + capacity isolation (R-P2-15/16/17, R-P1-64).

Criterion mapping
-----------------
* ① over-limit concurrency is **queued**, never blindly passed; the three
  semaphore layers (global / tenant / model) each get one test;
* ② queue timeout -> :class:`QueueTimeout` -> HTTP **429 + Retry-After**;
* ③ tool pool saturated -> model-stream success rate **>= 95%** (isolation);
* ④ a 500 ms DB delay injected into the persistence path leaves the model-stream
  **P99 latency increase < 50 ms** (async writes never block the response chain);
* ⑦ a second *live* SQLite instance is detected at startup -> **startup error**
  (multi-instance SQLite is explicitly rejected by R-P1-64 / PRD §3.3 #15).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from zhongzhuan.proxy.concurrency import (
    ConcurrencyGate,
    GateConfig,
    MultiInstanceError,
    QueueTimeout,
    guard_sqlite_single_instance,
    make_concurrency_middleware,
    queue_timeout_response,
)


class _FakeClock:
    """Injectable monotonic-ish clock; advance it to fake the passage of time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _p99(values: list[float]) -> float:
    ordered = sorted(values)
    idx = max(0, int(math.ceil(0.99 * len(ordered))) - 1)
    return ordered[idx]


# --------------------------------------------------------------------------- #
# ① three-layer semaphores: over-limit requests are queued, not let through
# --------------------------------------------------------------------------- #


async def _run_concurrent(gate: ConcurrencyGate, n: int, *, tenant: str, model: str) -> tuple[bool, int]:
    """Run *n* scoped model requests concurrently.

    Returns ``(all_succeeded, max_observed_active)``.
    """
    active = 0
    max_active = 0
    lock = asyncio.Lock()

    async def worker() -> bool:
        nonlocal active, max_active
        async with gate.model_scope(tenant=tenant, model=model):
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.005)
            async with lock:
                active -= 1
        return True

    results = await asyncio.gather(*[worker() for _ in range(n)])
    return all(results), max_active


async def test_global_layer_queues_instead_of_rejecting():
    gate = ConcurrencyGate(GateConfig(global_limit=1, queue_timeout_seconds=10))
    ok, max_active = await _run_concurrent(gate, 5, tenant="ws-a", model="m")
    assert ok is True  # all 5 got through => queued, not rejected
    assert max_active == 1  # global layer serialized them
    await gate.close(cancel_pending=True)


async def test_tenant_layer_serializes_within_tenant():
    gate = ConcurrencyGate(GateConfig(global_limit=100, tenant_limit=1, queue_timeout_seconds=10))
    # Tenant A's only slot is taken.
    hold_a = await gate.acquire_model(tenant="ws-a", model="m")
    # A different tenant is unaffected by A's saturation.
    async with gate.model_scope(tenant="ws-b", model="m"):
        pass
    # A's second request queues (blocked), then proceeds once the slot frees.
    second_done = asyncio.Event()

    async def second_a() -> None:
        async with gate.model_scope(tenant="ws-a", model="m"):
            second_done.set()

    task = asyncio.create_task(second_a())
    await asyncio.sleep(0.01)
    assert not second_done.is_set()  # queued, not let through
    await hold_a.release()
    await asyncio.wait_for(second_done.wait(), timeout=2)
    await task
    await gate.close(cancel_pending=True)


async def test_model_layer_serializes_same_model():
    gate = ConcurrencyGate(GateConfig(global_limit=100, model_limit=1, queue_timeout_seconds=10))
    hold_m1 = await gate.acquire_model(tenant="ws-a", model="m1")
    # A different model is independent of m1's saturation.
    async with gate.model_scope(tenant="ws-a", model="m2"):
        pass
    second_done = asyncio.Event()

    async def second_m1() -> None:
        async with gate.model_scope(tenant="ws-a", model="m1"):
            second_done.set()

    task = asyncio.create_task(second_m1())
    await asyncio.sleep(0.01)
    assert not second_done.is_set()  # queued at the model layer
    await hold_m1.release()
    await asyncio.wait_for(second_done.wait(), timeout=2)
    await task
    await gate.close(cancel_pending=True)


# --------------------------------------------------------------------------- #
# ② queue timeout -> 429 + Retry-After (R-P2-16)
# --------------------------------------------------------------------------- #


async def test_queue_timeout_immediate_zero_wait():
    """An already-expired deadline fails immediately (injectable clock)."""
    clock = _FakeClock()
    gate = ConcurrencyGate(GateConfig(global_limit=1, queue_timeout_seconds=5.0), clock=clock)
    hold = await gate.acquire_model(tenant="ws-a", model="m")  # take the slot
    with pytest.raises(QueueTimeout) as excinfo:
        await gate.acquire_model(tenant="ws-a", model="m", timeout=0.0)
    exc = excinfo.value
    assert exc.layer == "global"
    assert exc.retry_after == 0.0
    resp = queue_timeout_response(exc)
    assert resp.status == 429
    assert resp.headers.get("Retry-After")
    assert int(resp.headers["Retry-After"]) > 0
    await hold.release()
    await gate.close(cancel_pending=True)


async def test_queue_timeout_after_real_wait_maps_to_429():
    """A waiter that exhausts the queue budget gets 429 + Retry-After."""
    gate = ConcurrencyGate(GateConfig(global_limit=1, queue_timeout_seconds=0.05))
    hold = await gate.acquire_model(tenant="ws-a", model="m")
    with pytest.raises(QueueTimeout) as excinfo:
        await gate.acquire_model(tenant="ws-a", model="m")
    assert excinfo.value.retry_after == 0.05
    resp = queue_timeout_response(excinfo.value)
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "1"
    assert resp.body  # JSON error body present
    await hold.release()
    await gate.close(cancel_pending=True)


async def test_middleware_maps_queue_timeout_to_429_over_http():
    """End-to-end: a real HTTP request blocked by a full pool gets 429."""
    gate = ConcurrencyGate(GateConfig(global_limit=1, queue_timeout_seconds=0.05))
    middleware = make_concurrency_middleware(
        gate,
        tenant_provider=lambda request: "ws-a",
        model_provider=lambda request: "m",
    )
    app = web.Application(middlewares=[middleware])

    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app.router.add_get("/v1/responses", handler)

    hold = await gate.acquire_model(tenant="ws-a", model="m")  # fill the pool
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/v1/responses")
        assert resp.status == 429
        assert resp.headers.get("Retry-After")
        payload = await resp.json()
        assert payload["error"]["type"] == "server_overloaded"
    finally:
        await client.close()
        await hold.release()
    await gate.close(cancel_pending=True)


# --------------------------------------------------------------------------- #
# ③ isolated pools: tool pool full must not drag model-stream success down
# --------------------------------------------------------------------------- #


async def test_tool_pool_saturation_keeps_model_streams_above_95_percent():
    gate = ConcurrencyGate(
        GateConfig(
            global_limit=500,
            tenant_limit=0,
            model_limit=0,
            tool_pool_size=4,
            persistence_pool_size=2,
            queue_timeout_seconds=30,
        )
    )
    # Saturate the isolated tool-execution pool.
    held = [await gate.acquire_tool() for _ in range(4)]

    async def model_request() -> int:
        try:
            async with gate.model_scope(tenant="ws-a", model="m"):
                pass
            return 1
        except Exception:
            return 0

    results = await asyncio.gather(*[model_request() for _ in range(100)])
    success = sum(results)
    assert success >= 95  # R-P2-17: success rate > 95%
    for h in held:
        await h.release()
    await gate.close(cancel_pending=True)


# --------------------------------------------------------------------------- #
# ④ 500 ms DB delay -> model-stream P99 increase < 50 ms (R-P2-10/17)
# --------------------------------------------------------------------------- #


class _DelayedStore:
    """Fake DB whose writes sleep ``delay`` seconds (injected DB latency)."""

    def __init__(self) -> None:
        self.delay: float = 0.0
        self.writes: int = 0

    async def execute(self, sql: str, params: tuple | None = None) -> int:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        self.writes += 1
        return 0

    async def fetchone(self, sql, params=None):
        return None

    async def fetchall(self, sql, params=None):
        return []

    async def close(self) -> None:
        pass


async def test_db_delay_does_not_inflate_response_p99():
    store = _DelayedStore()
    gate = ConcurrencyGate(
        GateConfig(
            global_limit=200,
            tenant_limit=0,
            model_limit=0,
            persistence_pool_size=4,
            queue_timeout_seconds=30,
        )
    )
    n = 100

    async def one() -> float:
        start = time.perf_counter()
        async with gate.model_scope(tenant="ws-a", model="m"):
            # Persistence is fire-and-forget: the write lands on the isolated
            # persistence pool, never on the response chain.
            await gate.submit_persist(store.execute("INSERT INTO t(a) VALUES (1)"))
        return time.perf_counter() - start

    base = await asyncio.gather(*[one() for _ in range(n)])
    await asyncio.sleep(0.05)  # let the zero-delay writes drain
    base_writes = store.writes

    store.delay = 0.5  # 500 ms per write from now on
    slow = await asyncio.gather(*[one() for _ in range(n)])

    p99_base = _p99(list(base))
    p99_slow = _p99(list(slow))
    assert p99_slow - p99_base < 0.05  # P99 increase < 50 ms
    assert p99_slow < 0.05  # even with a 500 ms DB delay in the persist path
    assert base_writes >= n  # the async writes really happened (not dropped)
    await gate.close(cancel_pending=True)


# --------------------------------------------------------------------------- #
# ⑦ SQLite multi-instance detection -> startup error (R-P1-64)
# --------------------------------------------------------------------------- #


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "data.db")


def test_second_live_instance_raises_startup_error(tmp_path):
    db = _db(tmp_path)
    alive_1111 = lambda pid: pid == 1111  # noqa: E731
    lock = guard_sqlite_single_instance(db, pid=1111, is_alive=alive_1111)
    try:
        with pytest.raises(MultiInstanceError):
            # A second instance (different live PID) must refuse to start.
            guard_sqlite_single_instance(db, pid=2222, is_alive=alive_1111)
    finally:
        lock.release()


def test_stale_lock_from_dead_instance_is_stolen(tmp_path):
    db = _db(tmp_path)
    nobody_alive = lambda pid: False  # noqa: E731
    first = guard_sqlite_single_instance(db, pid=1111, is_alive=nobody_alive)
    first.release()
    second = guard_sqlite_single_instance(db, pid=2222, is_alive=nobody_alive)
    assert second.pid == 2222
    second.release()


def test_lock_file_records_owner_pid(tmp_path):
    db = _db(tmp_path)
    lock = guard_sqlite_single_instance(db)
    try:
        lock_path = Path(db).with_name(Path(db).name + ".instance.lock")
        assert lock_path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_same_process_re_guard_is_idempotent(tmp_path):
    db = _db(tmp_path)
    first = guard_sqlite_single_instance(db)
    try:
        second = guard_sqlite_single_instance(db)  # same PID -> no error
        assert second.pid == os.getpid()
        second.release()
    finally:
        first.release()
