"""Concurrency governance: 3-layer semaphores + isolated pools (T34 / R-P2-15~18).

Requirements covered
--------------------
* **R-P2-15** -- ``limits.global_concurrent`` is actually enforced.  Requests
  above the limit are **queued** (they wait for a free slot), never blindly
  let through.  The gate implements three independent semaphore layers --
  global, per-tenant and per-model -- plus a dedicated background-worker pool
  (each tenant/model pool is created lazily, so the gate does not pre-allocate
  unbounded state for unknown keys).
* **R-P2-16** -- queueing has an upper bound.  A request that waits longer than
  ``queue_timeout_seconds`` raises :class:`QueueTimeout`, which the HTTP layer
  maps to **429 + ``Retry-After``** via :func:`queue_timeout_response`.
* **R-P2-17** -- tool execution, model streaming and persistence use **mutually
  isolated pools**.  Saturating the tool pool (or a slow DB on the persistence
  path) must not drag the model-stream success rate down.

Why a queue instead of ``asyncio.Semaphore`` directly
-----------------------------------------------------
``asyncio.Semaphore.acquire()`` has no timeout, and its wake-up uses the real
event loop timer.  The tests need to verify the queue-timeout behaviour
**without actually waiting**, so every pool is backed by a
:class:`_TimedSlotPool` that carries an **injectable clock**: a waiter's
deadline is computed from ``clock()``, and a waiter whose deadline has already
passed fails immediately.  ``asyncio.wait_for`` is still used to wake up on
slot release, but the timeout decision itself is always made from the injected
clock -- see :class:`ConcurrencyGate`.

SQLite multi-instance guard
---------------------------
SQLite is **single-instance only** (PRD §13.1-P1-13 / §3.3 #15).  Two live
processes sharing one SQLite file corrupt each other's WAL writes, so startup
must refuse a second instance.  :func:`guard_sqlite_single_instance` creates a
per-DB lock file (``<db>.instance.lock``) holding the owner's PID; a *live*
foreign owner raises :class:`MultiInstanceError` (a stale lock from a dead PID
is stolen).  The call site is the startup path of the SQLite backend (see the
T34 report for the exact wiring line in ``SqliteStore.create``).
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loguru import logger

__all__ = [
    "QueueTimeout",
    "MultiInstanceError",
    "GateConfig",
    "ConcurrencyGate",
    "queue_timeout_response",
    "make_concurrency_middleware",
    "guard_sqlite_single_instance",
    "acquire_instance_lock",
    "instance_lock_path",
    "InstanceLock",
]


class QueueTimeout(RuntimeError):
    """A request waited longer than ``queue_timeout_seconds`` for a slot.

    Attributes:
        retry_after: Seconds the caller should wait before retrying (used for
            the ``Retry-After`` header of the mapped 429 response).
        layer: Which pool gave up (``"global"`` / ``"tenant:..."`` /
            ``"model:..."`` / ``"tool"`` / ``"background"``).
    """

    def __init__(self, retry_after: float, layer: str) -> None:
        super().__init__(f"concurrency slot '{layer}' unavailable after {retry_after:g}s")
        self.retry_after: float = retry_after
        self.layer: str = layer


class MultiInstanceError(RuntimeError):
    """A second live process tried to open the same SQLite database.

    SQLite is single-instance only (R-P1-64): the startup path must refuse to
    boot when another process already holds the DB (WAL multi-writer is
    unsafe).  Multi-instance deployments must use TiDB/MySQL instead.
    """


@dataclass(frozen=True)
class GateConfig:
    """Tunables for :class:`ConcurrencyGate`.

    ``0`` on ``tenant_limit`` / ``model_limit`` means "unlimited at that
    layer" -- only the global layer (and the relevant workload pool) then
    constrains the request.
    """

    global_limit: int = 64
    tenant_limit: int = 0
    model_limit: int = 0
    background_limit: int = 8
    tool_pool_size: int = 8
    persistence_pool_size: int = 4
    queue_timeout_seconds: float = 30.0


class _TimedSlotPool:
    """A fixed-size slot pool whose acquire honours an injectable-clock deadline.

    The wait loop is the *only* place in the gate that decides "queue vs
    429", so every timeout path is exercised deterministically by tests that
    advance a fake clock.
    """

    def __init__(self, size: int, clock: Callable[[], float], name: str) -> None:
        self._size = size
        self._free = size
        self._clock = clock
        self._name = name
        self._cond = asyncio.Condition()

    async def acquire(self, timeout: float | None, retry_after: float) -> None:
        deadline = None
        if timeout is not None:
            deadline = self._clock() + timeout
        async with self._cond:
            while self._free <= 0:
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise QueueTimeout(retry_after=retry_after, layer=self._name)
                    # Wait for a release or for the (real) remaining time.  The
                    # timeout *decision* is always re-made from the injected
                    # clock below, so a fake clock that is already past the
                    # deadline fails immediately without any real sleep.
                    waiter = asyncio.create_task(self._cond.wait())
                    try:
                        await asyncio.wait_for(waiter, timeout=remaining)
                    except asyncio.TimeoutError:
                        if self._free <= 0:
                            raise QueueTimeout(retry_after=retry_after, layer=self._name) from None
                        continue
                else:
                    await self._cond.wait()
            self._free -= 1

    async def release(self) -> None:
        async with self._cond:
            self._free += 1
            self._cond.notify(1)


@dataclass
class _Hold:
    """A set of acquired pools; release them in reverse order exactly once."""

    _pools: list[_TimedSlotPool] = field(default_factory=list)
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        for pool in reversed(self._pools):
            await pool.release()


class ConcurrencyGate:
    """Three-layer concurrency governance with isolated workload pools.

    Example:
        .. code-block:: python

            gate = ConcurrencyGate(GateConfig(global_limit=64, queue_timeout_seconds=30))
            async with gate.model_scope(tenant="ws-1", model="gpt-4o"):
                ...  # streaming
            await gate.submit_persist(store.execute("INSERT ..."))  # async write
    """

    def __init__(
        self,
        config: GateConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config: GateConfig = config or GateConfig()
        self._clock = clock
        self._global = _TimedSlotPool(self.config.global_limit, clock, "global")
        self._tool = _TimedSlotPool(self.config.tool_pool_size, clock, "tool")
        self._background = _TimedSlotPool(self.config.background_limit, clock, "background")
        # Lazily created per-tenant / per-model pools (bounded by config limits).
        self._tenants: dict[str, _TimedSlotPool] = {}
        self._models: dict[str, _TimedSlotPool] = {}
        self._persist_sem = asyncio.Semaphore(self.config.persistence_pool_size)
        self._persist_tasks: set[asyncio.Task] = set()
        self._closed = False

    # -- 3-layer model-stream chain ---------------------------------------- #

    def _tenant_pool(self, tenant: str) -> _TimedSlotPool | None:
        if self.config.tenant_limit <= 0:
            return None
        pool = self._tenants.get(tenant)
        if pool is None:
            pool = _TimedSlotPool(self.config.tenant_limit, self._clock, f"tenant:{tenant}")
            self._tenants[tenant] = pool
        return pool

    def _model_pool(self, model: str) -> _TimedSlotPool | None:
        if self.config.model_limit <= 0:
            return None
        pool = self._models.get(model)
        if pool is None:
            pool = _TimedSlotPool(self.config.model_limit, self._clock, f"model:{model}")
            self._models[model] = pool
        return pool

    async def acquire_model(self, *, tenant: str = "", model: str = "", timeout: float | None = None) -> _Hold:
        """Acquire a model-stream slot: global -> tenant -> model (reverse release).

        Waits (queues) when over the limit; raises :class:`QueueTimeout` after
        ``timeout`` (defaults to ``config.queue_timeout_seconds``) seconds on
        the injected clock.
        """
        if self._closed:
            raise RuntimeError("concurrency gate is closed")
        timeout = self.config.queue_timeout_seconds if timeout is None else timeout
        acquired: list[_TimedSlotPool] = []
        try:
            await self._global.acquire(timeout, timeout)
            acquired.append(self._global)
            tenant_pool = self._tenant_pool(tenant)
            if tenant_pool is not None:
                await tenant_pool.acquire(timeout, timeout)
                acquired.append(tenant_pool)
            model_pool = self._model_pool(model)
            if model_pool is not None:
                await model_pool.acquire(timeout, timeout)
                acquired.append(model_pool)
        except BaseException:
            for pool in reversed(acquired):
                await pool.release()
            raise
        return _Hold(acquired)

    @asynccontextmanager
    async def model_scope(self, *, tenant: str = "", model: str = "", timeout: float | None = None):
        """Async context manager wrapping :meth:`acquire_model`."""
        hold = await self.acquire_model(tenant=tenant, model=model, timeout=timeout)
        try:
            yield
        finally:
            await hold.release()

    # -- isolated workload pools ------------------------------------------- #

    async def acquire_tool(self, *, timeout: float | None = None) -> _Hold:
        """Acquire one slot in the isolated tool-execution pool (R-P2-17)."""
        timeout = self.config.queue_timeout_seconds if timeout is None else timeout
        await self._tool.acquire(timeout, timeout)
        return _Hold([self._tool])

    @asynccontextmanager
    async def tool_scope(self, *, timeout: float | None = None):
        hold = await self.acquire_tool(timeout=timeout)
        try:
            yield
        finally:
            await hold.release()

    async def acquire_background(self, *, timeout: float | None = None) -> _Hold:
        """Acquire one slot in the background-worker pool (R-P2-15)."""
        timeout = self.config.queue_timeout_seconds if timeout is None else timeout
        await self._background.acquire(timeout, timeout)
        return _Hold([self._background])

    @asynccontextmanager
    async def background_scope(self, *, timeout: float | None = None):
        hold = await self.acquire_background(timeout=timeout)
        try:
            yield
        finally:
            await hold.release()

    # -- persistence pool (async writes, never blocks the response chain) -- #

    async def submit_persist(self, coro) -> asyncio.Task:
        """Schedule a persistence write on the isolated persistence pool.

        Returns immediately -- the response chain never waits for the write.
        Concurrency is bounded by ``config.persistence_pool_size`` (R-P2-17:
        a slow DB saturates this pool, not the model-stream path).
        """
        if self._closed:
            raise RuntimeError("concurrency gate is closed")
        sem = self._persist_sem

        async def _run() -> None:
            try:
                async with sem:
                    try:
                        await coro
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("persistence task failed and was swallowed")
            except asyncio.CancelledError:
                # If we were cancelled while still waiting on the semaphore the
                # write coroutine never started -- close it so the event loop
                # does not warn about an un-awaited coroutine.
                if coro.cr_await is None:
                    coro.close()
                raise

        task = asyncio.create_task(_run())
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)
        return task

    def pending_persist(self) -> int:
        """Number of persistence writes still queued or in flight."""
        return len(self._persist_tasks)

    async def close(self, *, cancel_pending: bool = False) -> None:
        """Stop accepting new work.  By default drains gracefully; tests use
        ``cancel_pending=True`` to tear down fast without real waits."""
        self._closed = True
        pending = list(self._persist_tasks)
        if cancel_pending:
            for task in pending:
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# --------------------------------------------------------------------------- #
# HTTP mapping: queue timeout -> 429 + Retry-After (R-P2-16)
# --------------------------------------------------------------------------- #


def queue_timeout_response(exc: QueueTimeout):
    """Build the standard 429 overload response for *exc*.

    Returns:
        ``aiohttp.web.Response`` with ``status=429`` and a ``Retry-After``
        header carrying the seconds the client should wait before retrying.
    """
    from aiohttp import web

    retry_after = str(max(1, int(math.ceil(exc.retry_after))))
    return web.json_response(
        {
            "error": {
                "message": (
                    "server is overloaded; request waited for a concurrency "
                    f"slot ('{exc.layer}') longer than {exc.retry_after:g}s"
                ),
                "type": "server_overloaded",
                "retry_after": retry_after,
            }
        },
        status=429,
        headers={"Retry-After": retry_after},
    )


def make_concurrency_middleware(
    gate: ConcurrencyGate,
    *,
    tenant_provider: Callable | None = None,
    model_provider: Callable | None = None,
):
    """aiohttp middleware that gates every request through :class:`ConcurrencyGate`.

    Tenant/model are read from the request via ``tenant_provider(request)`` /
    ``model_provider(request)`` (defaults to the ``X-Workspace-Id`` /
    ``X-Model`` headers).  A queue timeout becomes the standard 429 response.
    """
    from aiohttp import web

    @web.middleware
    async def middleware(request: web.Request, handler):
        tenant = tenant_provider(request) if tenant_provider else request.headers.get("X-Workspace-Id", "")
        model = model_provider(request) if model_provider else request.headers.get("X-Model", "")
        try:
            async with gate.model_scope(tenant=tenant, model=model):
                return await handler(request)
        except QueueTimeout as exc:
            return queue_timeout_response(exc)

    return middleware


# --------------------------------------------------------------------------- #
# SQLite multi-instance detection (R-P1-64 / PRD §3.3 #15)
# --------------------------------------------------------------------------- #


def instance_lock_path(db_path: str | Path) -> Path:
    """Path of the per-DB instance lock (``<db>.instance.lock``)."""
    p = Path(db_path)
    return p.with_name(p.name + ".instance.lock")


@dataclass
class InstanceLock:
    """Handle to an acquired instance lock; :meth:`release` removes the file."""

    path: Path
    pid: int

    def release(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort cleanup
            pass


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        text = lock_path.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        return None


def _windows_pid_alive(pid: int) -> bool:
    """Check process liveness on Windows without killing it (``os.kill(pid,0)``
    on Windows would *terminate* the process -- never use it there)."""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = getattr(ctypes, "windll").kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True  # cannot query -> assume alive (fail closed)
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - defensive
        return False


def _default_is_alive(pid: int) -> bool:
    """``True`` when *pid* belongs to a live process (platform-safe)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_instance_lock(
    db_path: str | Path,
    *,
    pid: int | None = None,
    is_alive: Callable[[int], bool] | None = None,
) -> InstanceLock:
    """Acquire the exclusive per-DB instance lock.

    Creates ``<db>.instance.lock`` containing our PID, using ``O_EXCL`` so two
    processes cannot both win.  When the lock already exists:

    * held by a **live** foreign process -> :class:`MultiInstanceError`;
    * stale (dead PID / empty / ours) -> stolen and re-created.

    Args:
        db_path: Path of the SQLite database file.
        pid: Owner PID (defaults to ``os.getpid()``); tests pass an explicit
            foreign PID plus a fake ``is_alive`` to simulate another instance.
        is_alive: Liveness probe, injected by tests (default is platform-safe).
    """
    owner = os.getpid() if pid is None else pid
    probe = _default_is_alive if is_alive is None else is_alive
    lock_path = instance_lock_path(db_path)
    existing = _read_lock_pid(lock_path)
    if existing is not None and existing != owner and probe(existing):
        raise MultiInstanceError(
            f"SQLite database {db_path} is already in use by live instance "
            f"pid={existing}; SQLite is single-instance only (R-P1-64). "
            "Deploy multiple instances on TiDB/MySQL instead."
        )
    if existing is not None:
        # stale or our own lock -- steal it (best effort; O_EXCL below still
        # protects against a racing foreign creator).
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - racing unlink is fine
            pass
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, str(owner).encode("ascii"))
    finally:
        os.close(fd)
    return InstanceLock(lock_path, owner)


def guard_sqlite_single_instance(
    db_path: str | Path,
    *,
    pid: int | None = None,
    is_alive: Callable[[int], bool] | None = None,
) -> InstanceLock:
    """Startup guard for the SQLite backend.

    Call **before** opening the database connection.  When another live
    instance already holds the DB it raises :class:`MultiInstanceError`, which
    the startup path converts into a refusal to boot (startup error).
    """
    return acquire_instance_lock(db_path, pid=pid, is_alive=is_alive)
