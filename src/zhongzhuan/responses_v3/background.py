"""Background response worker (T24 / R-P1-34, R-P1-35, R-P1-37, R-P1-38, R-P0-32).

A ``background=true`` request must return an id *immediately* and keep running
after the HTTP connection that created it is gone.  That splits one response
into two independent lifetimes:

* the **enqueue** path (:meth:`BackgroundWorker.enqueue`) -- persist the
  response as ``queued``, persist a ``response.queued`` event, insert the job,
  return.  No upstream call happens here, which is what makes the sub-second
  guarantee of R-P1-34 ① structural rather than aspirational;
* the **execution** path (:meth:`BackgroundWorker.run_job`) -- claim a lease,
  heartbeat it, drive the loop under an :class:`ExecutionBudget`, and land on
  exactly one of the five terminal states.

Everything the client can later observe (``retrieve``, catch-up replay) comes
from the store, never from worker memory: the worker is allowed to die at any
instant, and the only reason that is survivable is that every event was
persisted *before* it was of any use to anyone.

Cancellation is cooperative (R-P1-35)
-------------------------------------
``cancel`` sets a flag in the database **and** closes the in-flight upstream if
the job happens to be running in this process.  Both halves are needed: the
flag is what a *different* worker process sees on its next round boundary, and
closing the upstream is what stops the money burning right now.  A cancel that
only set a flag would keep paying for tokens nobody will ever read.

Budget mapping (R-P1-38)
------------------------
Five distinct ceilings must be individually diagnosable.  Four come straight
from :class:`BudgetLedger` (rounds / total calls / output tokens / wall clock).
The fifth, ``BACKGROUND_BUDGET_EXHAUSTED``, is the *background envelope*: a cap
on the total tool work one detached job may do, checked **after** the shared
ledger so a tighter envelope is reported as a background-specific failure while
the generic ceilings keep their own reasons.  Without a separate counter the
two would be indistinguishable and R-P1-38 could not be satisfied.

HONEST STUB
-----------
* The executed "loop" is driven by an **injected** ``upstream`` iterable, not
  by a real provider stream.  The real upstream translation + tool loop is
  T28; this module deliberately never imports the provider layer, so wiring
  T28 in means replacing the source of the chunks and nothing else.
* Step 3 of the circuit breaker (side-effect rollback) is still the T23 stub:
  transactional tool rollback / idempotency replay is T26.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Callable

from ..proxy.protocol.responses_emitter import ResponsesEventEmitter
from ..proxy.protocol.responses_errors import to_incomplete_details
from ..proxy.protocol.responses_models import ResponseStatus, TerminalReason
from ..store.background_jobs import TERMINAL_STATUSES
from ..store.response_store import ResponseRecord, ResponseStore
from .budget import BACKGROUND_BUDGET, BudgetLedger, CircuitBreaker, ExecutionBudget

LOGGER = logging.getLogger("zhongzhuan.responses_v3.background")

#: Default lease length.  Long enough that a slow tool round cannot lose the
#: lease, short enough that a crashed worker's job is recovered promptly.
DEFAULT_LEASE_SECONDS: int = 300

#: Default heartbeat period; a third of the lease gives two chances to renew
#: before it lapses.
DEFAULT_HEARTBEAT_SECONDS: float = 30.0

#: Terminal ``response.*`` event name per terminal status.
_TERMINAL_EVENT: dict[str, str] = {
    "completed": "response.completed",
    "failed": "response.failed",
    "incomplete": "response.incomplete",
    "cancelled": "response.cancelled",
    "expired": "response.incomplete",
}


@dataclass
class _JobIdentity:
    """The two identity fields :class:`CircuitBreaker` reads off a context."""

    request_id: str = ""
    workspace_id: str = ""


@dataclass
class _JobRun:
    """Live state for one running job; also the breaker's pipeline handle.

    It implements ``cancel_upstream`` / ``stop_scheduling`` so the six-step
    trip of R-P0-32 can stop this job without knowing anything about workers.
    """

    task_id: str
    response_id: str
    workspace_id: str = ""
    upstream: Any = None
    stopped: bool = False
    cancelled: bool = False
    bg_tool_calls: int = 0
    open_item_ids: list[str] = field(default_factory=list)

    async def cancel_upstream(self) -> None:
        """Close the injected upstream, whatever shape it has."""
        self.cancelled = True
        source = self.upstream
        if source is None:
            return
        for hook_name in ("aclose", "close", "cancel"):
            hook = getattr(source, hook_name, None)
            if hook is None:
                continue
            try:
                result = hook()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - teardown must not raise
                LOGGER.warning("closing upstream via %s failed: %r", hook_name, exc)
            return

    def stop_scheduling(self) -> None:
        self.stopped = True

    def open_items(self) -> list[Any]:
        """No structured items are opened by the T24 skeleton (see HONEST STUB)."""
        return []


class BackgroundWorker:
    """Owns the ``background=true`` lifecycle end to end."""

    def __init__(
        self,
        store: ResponseStore,
        *,
        budget: ExecutionBudget = BACKGROUND_BUDGET,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        max_background_calls: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._jobs = store.jobs
        self._budget = budget
        self._lease_seconds = int(lease_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)
        #: Background-only envelope on total tool calls (see module docstring).
        self.max_background_calls = (
            int(budget.max_total_tool_calls) if max_background_calls is None else int(max_background_calls)
        )
        self._clock = clock
        self._runs: dict[str, _JobRun] = {}
        self._running = False

    # -- accessors -----------------------------------------------------------

    @property
    def store(self) -> ResponseStore:
        return self._store

    @property
    def jobs(self) -> Any:
        """The :class:`~zhongzhuan.store.background_jobs.BackgroundJobStore`."""
        return self._jobs

    # -- 1. enqueue (R-P1-34 ①) ---------------------------------------------

    async def enqueue(
        self,
        *,
        response_id: str,
        workspace_id: str = "",
        model: str = "",
        request: dict[str, Any] | None = None,
        previous_response_id: str = "",
        budget: ExecutionBudget | None = None,
        expires_at: int = 0,
    ) -> ResponseRecord | None:
        """Persist a queued background response and return it immediately.

        Three writes, no network: the response row, the ``response.queued``
        event (emitted **only** for background requests, before
        ``response.created`` -- PRD ruling) and the job row.
        """
        effective = budget or self._budget
        await self._store.create_response(
            response_id=response_id,
            workspace_id=workspace_id,
            model=model,
            status="queued",
            background=True,
            request=request or {},
            previous_response_id=previous_response_id,
        )
        await self._store.append_event(
            response_id,
            "response.queued",
            {
                "type": "response.queued",
                "response": {"id": response_id, "status": "queued"},
            },
        )
        await self._jobs.create_job(
            task_id=response_id,
            response_id=response_id,
            workspace_id=workspace_id,
            max_wall_seconds=effective.max_wall_time_seconds,
            max_tool_rounds=effective.max_tool_rounds,
            expires_at=expires_at,
        )
        return await self._store.get_response(response_id, workspace_id=workspace_id)

    # -- 2. execution --------------------------------------------------------

    async def run_job(
        self,
        task_id: str,
        *,
        upstream: Any,
        now: int | None = None,
        budget: ExecutionBudget | None = None,
    ) -> str | None:
        """Claim ``task_id`` and drive it to exactly one terminal state.

        Returns the terminal status.  ``None`` means the job was **not run
        here** and is not finished either -- another worker holds the lease.
        A job that was already terminal (expired by TTL, failed by exhausted
        recovery) reports that status rather than a bare ``None``, so the
        caller can tell "someone else has it" from "it is over".
        """
        wall_now = int(time.time()) if now is None else int(now)
        for expired_id in await self._jobs.expire_stale(now=wall_now):
            await self._finalize_expired(expired_id)
            if expired_id == task_id:
                return "expired"

        claimed = await self._jobs.claim_job(
            self._lease_seconds,
            now=wall_now,
            task_id=task_id,
        )
        if claimed is None:
            existing = await self._jobs.get_job_any_tenant(task_id) or {}
            status = str(existing.get("status") or "")
            return status if status in TERMINAL_STATUSES else None

        job = await self._jobs.get_job_any_tenant(task_id) or {}
        response_id = str(job.get("response_id") or task_id)
        workspace_id = str(job.get("workspace_id") or "")
        effective = budget or self._budget

        run = _JobRun(
            task_id=task_id,
            response_id=response_id,
            workspace_id=workspace_id,
            upstream=upstream,
        )
        self._runs[task_id] = run
        ledger = BudgetLedger(effective, started_at=self._clock())
        emitter = ResponsesEventEmitter(response_id=response_id)
        heartbeat = asyncio.create_task(self._heartbeat(task_id))

        try:
            reason = await self._execute(run, upstream, ledger, emitter)
            if reason is not None:
                status = await self._trip(
                    run,
                    reason,
                    emitter,
                    workspace_id=workspace_id,
                )
            elif run.cancelled or await self._jobs.is_cancel_requested(task_id):
                status = await self._finish_cancelled(run, emitter)
            else:
                status = await self._finish_completed(run, emitter, ledger)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one job must not kill the worker
            LOGGER.warning("background job %s failed: %r", task_id, exc)
            status = await self._finish_failed(run, emitter, exc)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._runs.pop(task_id, None)
        return status

    async def _heartbeat(self, task_id: str) -> None:
        """Renew the lease until the job leaves the active states."""
        try:
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                if not await self._jobs.renew_lease(task_id, self._lease_seconds):
                    return
        except asyncio.CancelledError:
            return

    # -- 3. the loop ---------------------------------------------------------

    async def _execute(
        self,
        run: _JobRun,
        upstream: Any,
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
    ) -> TerminalReason | None:
        """Stream the job, charging the budget; return the first ceiling hit."""
        await self._store.update_status(
            run.response_id,
            "in_progress",
            workspace_id=run.workspace_id,
        )
        # ``start()`` drives the emitter's own created/in_progress pair; the
        # store copies below are what a catch-up reader replays.
        emitter.start()
        await self._store.append_event(
            run.response_id,
            "response.created",
            {
                "type": "response.created",
                "response": {"id": run.response_id, "status": "in_progress"},
            },
        )
        await self._store.append_event(
            run.response_id,
            "response.in_progress",
            {
                "type": "response.in_progress",
                "response": {"id": run.response_id, "status": "in_progress"},
            },
        )
        return await self._stream(run, upstream, ledger, emitter)

    async def _stream(
        self,
        run: _JobRun,
        upstream: Any,
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
    ) -> TerminalReason | None:
        """Consume the injected upstream, persisting every frame it produces.

        ``upstream`` may be an ``AsyncIterable`` or a zero-argument callable
        returning one, so a test can hand over a fresh generator per attempt
        (an already-started async generator cannot be re-entered after a
        recovery).
        """
        source = upstream() if callable(upstream) else upstream
        run.upstream = source
        index = 0
        async for chunk in source:
            # Cancellation and the wall clock are checked at every boundary --
            # they are the two ceilings that can be crossed while the loop is
            # merely *waiting*, so charging them per chunk is not enough.
            if run.stopped:
                break
            if run.cancelled or await self._jobs.is_cancel_requested(run.task_id):
                run.cancelled = True
                await run.cancel_upstream()
                return None
            reason = ledger.check_wall_time(now=self._clock())
            if reason is not None:
                return reason
            reason = await self._charge_chunk(run, chunk, ledger, emitter, index)
            if reason is not None:
                return reason
            index += 1
        return ledger.check_wall_time(now=self._clock())

    async def _charge_chunk(
        self,
        run: _JobRun,
        chunk: Any,
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
        index: int,
    ) -> TerminalReason | None:
        """Charge one upstream chunk against the budget and persist its event.

        HONEST STUB: the chunk vocabulary below is the *test* vocabulary, not
        a provider wire format.  T28 replaces this with the real translator;
        the budget calls it makes are the contract that survives.
        """
        if not isinstance(chunk, dict):
            return await self._charge_text(run, str(chunk), 1, ledger, emitter, index)

        kind = str(chunk.get("type") or "")

        if kind == "tool_round":
            reason = ledger.charge_round()
            if reason is not None:
                return reason
            return None

        if kind == "tool_call":
            name = str(chunk.get("name") or "")
            reason = ledger.charge_tool_call(name, chunk.get("arguments", ""))
            if reason is not None:
                return reason
            run.bg_tool_calls += 1
            if run.bg_tool_calls > self.max_background_calls:
                return TerminalReason.BACKGROUND_BUDGET_EXHAUSTED
            await self._emit(
                run,
                emitter,
                "response.function_call.persisted",
                {
                    "type": "response.function_call.persisted",
                    "output_index": index,
                    "name": name,
                },
            )
            return None

        if kind == "tool_result":
            return ledger.charge_tool_result(
                str(chunk.get("signature") or ""),
                bool(chunk.get("failed")),
            )

        text = str(chunk.get("delta", chunk.get("text", "")) or "")
        tokens = int(chunk.get("tokens", 1) or 0)
        return await self._charge_text(run, text, tokens, ledger, emitter, index)

    async def _charge_text(
        self,
        run: _JobRun,
        text: str,
        tokens: int,
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
        index: int,
    ) -> TerminalReason | None:
        reason = ledger.charge_output_tokens(tokens)
        if reason is not None:
            return reason
        await self._emit(
            run,
            emitter,
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "output_index": index,
                "delta": text,
            },
        )
        return None

    async def _emit(
        self,
        run: _JobRun,
        emitter: ResponsesEventEmitter,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Persist one event (catch-up) and mirror it to the live emitter.

        Persistence comes first on purpose: a crash between the two must lose
        the live frame (whose reader may already be gone) rather than the
        durable record (which is the only thing a reconnect can replay).
        """
        await self._store.append_event(run.response_id, event_type, data)
        emitter.delta(event_type, dict(data))

    # -- 4. terminal states --------------------------------------------------

    async def _trip(
        self,
        run: _JobRun,
        reason: TerminalReason,
        emitter: ResponsesEventEmitter,
        *,
        workspace_id: str = "",
    ) -> str:
        """Budget ceiling crossed: six-step teardown -> ``incomplete``."""
        breaker = CircuitBreaker()
        await breaker.trip(
            reason,
            _JobIdentity(request_id=run.response_id, workspace_id=workspace_id),
            run,
            emitter,
            run,
            self._store,
        )
        details = to_incomplete_details(
            reason,
            "background job terminated: {0}".format(reason.value),
        )
        await self._store.update_status(
            run.response_id,
            "incomplete",
            workspace_id=run.workspace_id,
            terminal_reason=reason.value,
            incomplete_details=details,
        )
        await self._persist_terminal(
            run,
            "incomplete",
            {
                "terminal_reason": reason.value,
                "incomplete_details": details,
            },
        )
        await self._jobs.mark_terminal(run.task_id, "incomplete")
        return "incomplete"

    async def _finish_completed(
        self,
        run: _JobRun,
        emitter: ResponsesEventEmitter,
        ledger: BudgetLedger,
    ) -> str:
        usage = {"output_tokens": ledger.output_tokens}
        await self._store.update_status(
            run.response_id,
            "completed",
            workspace_id=run.workspace_id,
            terminal_reason=TerminalReason.NORMAL_FINISH.value,
            usage=usage,
        )
        emitter.terminate(
            ResponseStatus.COMPLETED,
            terminal_reason=TerminalReason.NORMAL_FINISH.value,
        )
        await self._persist_terminal(run, "completed", {"usage": usage})
        await self._jobs.mark_terminal(run.task_id, "completed")
        return "completed"

    async def _finish_cancelled(
        self,
        run: _JobRun,
        emitter: ResponsesEventEmitter,
    ) -> str:
        await run.cancel_upstream()
        reason = TerminalReason.CANCELLED_BY_CLIENT.value
        await self._store.update_status(
            run.response_id,
            "cancelled",
            workspace_id=run.workspace_id,
            terminal_reason=reason,
        )
        emitter.terminate(ResponseStatus.CANCELLED, terminal_reason=reason)
        await self._persist_terminal(run, "cancelled", {"terminal_reason": reason})
        await self._jobs.mark_terminal(run.task_id, "cancelled")
        return "cancelled"

    async def _finish_failed(
        self,
        run: _JobRun,
        emitter: ResponsesEventEmitter,
        exc: BaseException,
    ) -> str:
        """R-P0-32: an exception is attributed, never laundered into success."""
        await run.cancel_upstream()
        error = {"type": "server_error", "message": "background job failed"}
        await self._store.update_status(
            run.response_id,
            "failed",
            workspace_id=run.workspace_id,
            terminal_reason=TerminalReason.UPSTREAM_ERROR.value,
            error=type(exc).__name__,
        )
        emitter.terminate(
            ResponseStatus.FAILED,
            terminal_reason=TerminalReason.UPSTREAM_ERROR.value,
            error=error,
        )
        await self._persist_terminal(run, "failed", {"error": error})
        await self._jobs.mark_terminal(run.task_id, "failed")
        return "failed"

    async def _finalize_expired(self, task_id: str) -> None:
        """Mirror a TTL expiry onto the response row + event log."""
        job = await self._jobs.get_job_any_tenant(task_id) or {}
        response_id = str(job.get("response_id") or task_id)
        workspace_id = str(job.get("workspace_id") or "")
        await self._store.update_status(
            response_id,
            "expired",
            workspace_id=workspace_id,
            terminal_reason="expired",
        )
        await self._store.append_event(
            response_id,
            "response.incomplete",
            {
                "type": "response.incomplete",
                "response": {"id": response_id, "status": "expired"},
                "incomplete_details": {"reason": "expired"},
            },
        )

    async def _persist_terminal(
        self,
        run: _JobRun,
        status: str,
        extra: dict[str, Any],
    ) -> None:
        """Write the terminal event to the log so catch-up can replay it."""
        event_type = _TERMINAL_EVENT.get(status, "response.completed")
        data: dict[str, Any] = {
            "type": event_type,
            "response": {"id": run.response_id, "status": status},
        }
        data.update(extra)
        await self._store.append_event(run.response_id, event_type, data)

    # -- 5. cancel (R-P1-35) -------------------------------------------------

    async def cancel(self, task_id: str) -> None:
        """Request cancellation and close the upstream if we own the job."""
        await self._jobs.request_cancel(task_id)
        run = self._runs.get(task_id)
        if run is not None:
            run.cancelled = True
            run.stop_scheduling()
            await run.cancel_upstream()

    # -- 6. worker loop ------------------------------------------------------

    async def start(
        self,
        *,
        poll_interval: float = 1.0,
        upstream_factory: Callable[[str], Any] | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Poll for claimable jobs and run them until stopped.

        ``upstream_factory`` builds the execution source for a task id; it is
        injected because the real one (T28) does not exist yet, and a worker
        that hard-codes its executor cannot be tested at all.
        """
        self._running = True
        iterations = 0
        try:
            while self._running:
                if max_iterations is not None and iterations >= max_iterations:
                    return
                iterations += 1
                # Peek, then let ``run_job`` do the claiming: a double claim
                # would burn a recovery attempt for every poll.
                task_id = await self._jobs.peek_claimable()
                if task_id is None:
                    await asyncio.sleep(poll_interval)
                    continue
                source = upstream_factory(task_id) if upstream_factory else _empty
                await self.run_job(task_id, upstream=source)
        finally:
            self._running = False

    def stop(self) -> None:
        """Ask :meth:`start` to leave its loop after the current job."""
        self._running = False


async def _empty() -> AsyncIterable[Any]:
    """Zero-chunk upstream (HONEST STUB default for :meth:`BackgroundWorker.start`)."""
    return
    yield  # pragma: no cover - makes this an async generator


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_HEARTBEAT_SECONDS",
    "BackgroundWorker",
]
