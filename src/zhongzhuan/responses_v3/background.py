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

Chunk source (T28, now wired)
-----------------------------
The executed "loop" is driven by an **injected** ``upstream`` iterable rather
than by a provider client this module constructs itself; that inversion is
what lets GA hand over a real translated stream (``ProxyHandler``'s
``_v3_background_upstream_factory`` feeds
:class:`~.upstream_chunk_adapter.UpstreamSSEChunkAdapter` output straight in)
while the tests hand over a synthetic one.  Because both speak the same
vocabulary -- see :meth:`BackgroundWorker._charge_chunk` -- the worker cannot
tell them apart, which is precisely why a background response and a live
stream emit the same events (架构 D4).

Known remaining stub
--------------------
Step 3 of the circuit breaker (side-effect rollback) is still the T23 stub:
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
from ..proxy.protocol.responses_models import (
    ResponseStatus,
    TerminalReason,
    canonical_json,
    make_function_call_item_id_stable,
    make_message_item_id,
)
from ..proxy.protocol.tool_accumulator import ToolCallAccumulator, ToolCallCollection
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


def _arguments_text(value: Any) -> str:
    """Normalise a tool-arguments fragment to the JSON *text* the wire carries.

    The adapter already hands over fragments as strings (it forwards them
    verbatim, never parsing them).  The budget vocabulary passes a whole dict
    instead, which is serialised canonically here so that two identical calls
    stay byte-identical -- that equality is exactly what the no-progress loop
    breaker compares (R-P0-28 / §9.4).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return canonical_json(value)


def _optional_index(value: Any) -> int | None:
    """Coerce an upstream tool index to ``int``; ``None`` when absent/unusable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
    #: Assistant text fragments, in arrival order (D4: the background run must
    #: be able to reconstruct the same ``output`` array a live stream would).
    text_parts: list[str] = field(default_factory=list)
    #: Output index of the single assistant message item; ``-1`` until the
    #: first non-empty text delta opens it.
    message_output_index: int = -1
    #: Whether ``response.output_item.done`` was already written for it.
    message_done: bool = False
    #: Monotonic Responses ``output_index`` allocator, shared by the message
    #: and every tool call, exactly like :class:`ResponsePipeline`'s.
    next_index: int = 0
    #: P0-2 bookkeeping: whether the upstream positively signalled ``finish``.
    saw_finish: bool = False
    #: The *same* accumulator type the live pipeline uses, so a background run
    #: and a live stream produce identical items ids, identical ordering and
    #: identical validation outcomes (架构 D4).  Built in ``__post_init__``
    #: because it needs ``response_id``, which is an init field.
    tools: ToolCallCollection = field(init=False)

    def __post_init__(self) -> None:
        self.tools = ToolCallCollection(response_id=self.response_id)

    def allocate_output_index(self) -> int:
        """Reserve the next global ``output_index`` for a freshly opened item."""
        index = self.next_index
        self.next_index += 1
        return index

    def message_item(self, *, status: str) -> dict[str, Any]:
        """The assistant message item as it must appear in ``output``."""
        return {
            "id": make_message_item_id(self.response_id, max(self.message_output_index, 0)),
            "type": "message",
            "role": "assistant",
            "status": "completed" if status == "completed" else "incomplete",
            "content": [
                {
                    "type": "output_text",
                    "text": "".join(self.text_parts),
                    "annotations": [],
                }
            ],
        }

    def tool_item(self, acc: ToolCallAccumulator) -> dict[str, Any]:
        """One accumulated tool call as it must appear in ``output``."""
        return {
            "id": acc.item_id or make_function_call_item_id_stable(
                self.response_id, acc.output_index
            ),
            "type": "function_call",
            # ``arguments_done`` records what actually happened during the run
            # rather than re-parsing here: a fresh validation could disagree
            # with the events already written to the log.
            "status": "completed" if acc.arguments_done else "incomplete",
            "call_id": acc.call_id,
            "name": acc.name,
            "arguments": acc.arguments,
        }

    def output_items(self, *, status: str) -> list[dict[str, Any]]:
        """Rebuild the ``output`` array from what this run actually produced.

        Mirrors :meth:`ResponsePipeline.output_items` field for field -- same
        item ids, same ordering by ``output_index``, same "an unvalidated tool
        call is ``incomplete``" rule -- so a background response and a
        live-streamed one deserialize identically (架构 D4 / 铁律 2).
        """
        items: list[tuple[int, dict[str, Any]]] = []
        if self.message_output_index >= 0:
            items.append((self.message_output_index, self.message_item(status=status)))
        for acc in self.tools.list_all():
            if not acc.item_added:
                continue
            items.append((acc.output_index, self.tool_item(acc)))
        items.sort(key=lambda pair: pair[0])
        return [item for _index, item in items]

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
        """Deliberately empty -- see :meth:`BackgroundWorker._close_open_items`.

        :meth:`CircuitBreaker.trip` step 4 closes open items through the
        *emitter* only, so those terminators would never reach the event log
        and a catch-up reader would replay a stream whose items are never
        closed.  The worker therefore closes them itself, durably, before it
        trips; reporting them here as well would emit a second
        ``output_item.done`` for the same item and break 铁律 3.
        """
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

        # R-P1-35: a job cancelled while it sat in the queue must never be
        # revived.  ``_execute`` writes ``in_progress`` as its first act, so
        # checking only at the round boundaries inside ``_stream`` would flip a
        # ``cancelled`` row back to ``in_progress`` and open an upstream
        # connection whose tokens are billed and then thrown away.  The check
        # therefore happens *before* any state is written, and before the
        # heartbeat task exists -- there is nothing yet to keep alive.
        if await self._jobs.is_cancel_requested(task_id):
            run.cancelled = True
            try:
                return await self._finish_cancelled(run, emitter)
            finally:
                self._runs.pop(task_id, None)

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
        """Charge one upstream chunk against the budget and persist its events.

        Two chunk vocabularies reach this method and both are first class:

        * the **unified pipeline vocabulary** produced by
          :class:`~.upstream_chunk_adapter.UpstreamSSEChunkAdapter` --
          ``text`` / ``tool_call`` / ``tool_call_done`` / ``finish`` -- which
          is what a real provider stream looks like once normalised.  It is
          the same vocabulary :class:`ResponsePipeline` consumes, which is
          precisely what makes a background run and a live stream produce the
          same events (架构 D4);
        * the **loop vocabulary** -- ``tool_round`` / ``tool_result`` -- which
          carries the agentic-loop accounting the wire adapter has no notion
          of and only the executor can supply.

        Anything else is an unrecognised control chunk: it is charged nothing
        and, above all, it does **not** fall through to the text branch.  A
        control chunk rendered as an empty ``response.output_text.delta`` is
        indistinguishable from corruption to a catch-up reader (P0-6).
        """
        if not isinstance(chunk, dict):
            return await self._charge_text(run, str(chunk), 1, ledger, emitter)

        kind = str(chunk.get("type") or "")

        if kind == "tool_round":
            return ledger.charge_round()

        if kind == "tool_result":
            return ledger.charge_tool_result(
                str(chunk.get("signature") or ""),
                bool(chunk.get("failed")),
            )

        if kind == "tool_call":
            return await self._charge_tool_call(run, chunk, ledger, emitter, index)

        if kind == "tool_call_done":
            return await self._settle_tool_call(run, chunk, ledger, emitter)

        if kind == "finish":
            # P0-2: the only positive evidence that the upstream chose to
            # stop.  It emits no event of its own -- it is recorded so the
            # terminal handlers can tell a graceful end from a truncation.
            run.saw_finish = True
            return None

        if kind in ("text", "output_text.delta") or "delta" in chunk or "text" in chunk:
            text = str(chunk.get("delta", chunk.get("text", "")) or "")
            tokens = int(chunk.get("tokens", 1) or 0)
            return await self._charge_text(run, text, tokens, ledger, emitter)

        return None

    async def _charge_tool_call(
        self,
        run: _JobRun,
        chunk: dict[str, Any],
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
        index: int,
    ) -> TerminalReason | None:
        """Accumulate one tool-call fragment, opening its output item once.

        ``source_index`` is the join key (§5.3).  A Chat Completions upstream
        sends ``id`` on the *first* fragment only, so matching on ``call_id``
        alone would split one call into N accumulators and charge the ledger N
        times for a single call.

        A chunk that carries neither ``source_index`` nor ``call_id`` is a
        *self-contained* call from the loop vocabulary: no ``tool_call_done``
        will ever follow it, so it is charged and settled right here.  A
        streamed call is charged exactly once, at its terminator, where the
        arguments are finally known -- charging a partial fragment would make
        the loop-breaker signature depend on chunk boundaries.
        """
        streamed = ("source_index" in chunk) or bool(chunk.get("call_id"))
        source_index = _optional_index(chunk.get("source_index"))
        if source_index is None:
            source_index = index
        call_id = str(chunk.get("call_id") or "")
        name = str(chunk.get("name") or "")
        fragment = _arguments_text(chunk.get("arguments", ""))

        existing = run.tools.get(call_id=call_id, source_index=source_index)
        if existing is None:
            if not streamed:
                # R-P1-38: the shared ledger is charged *before* the
                # background envelope so a generic ceiling keeps its own,
                # more specific reason instead of being reported as a
                # background-only failure.
                reason = ledger.charge_tool_call(name, fragment)
                if reason is not None:
                    return reason
            run.bg_tool_calls += 1
            if run.bg_tool_calls > self.max_background_calls:
                return TerminalReason.BACKGROUND_BUDGET_EXHAUSTED
            output_index = run.allocate_output_index()
        else:
            output_index = existing.output_index

        acc = run.tools.ensure(
            output_index=output_index,
            call_id=call_id,
            source_index=source_index,
        )
        acc.replace_name(name)
        acc.append_arguments(fragment)

        if not acc.item_added:
            await self._emit(
                run,
                emitter,
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": acc.output_index,
                    "item": {
                        "id": acc.item_id,
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": acc.call_id,
                        "name": acc.name,
                        "arguments": "",
                    },
                },
            )
            acc.item_added = True

        await self._emit(
            run,
            emitter,
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "output_index": acc.output_index,
                "call_id": acc.call_id,
                "delta": fragment,
            },
        )

        if not streamed:
            await self._close_tool_item(run, acc, emitter)
        return None

    async def _settle_tool_call(
        self,
        run: _JobRun,
        chunk: dict[str, Any],
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
    ) -> TerminalReason | None:
        """Handle a ``tool_call_done``: charge the call once, then close it."""
        acc = run.tools.get(
            call_id=str(chunk.get("call_id") or ""),
            source_index=_optional_index(chunk.get("source_index")),
        )
        if acc is None or acc.item_done:
            # A terminator for a call we never saw open, or a duplicate one
            # (§9.3).  Nothing to charge, nothing to close -- and emphatically
            # not a text delta.
            return None
        acc.append_arguments(_arguments_text(chunk.get("arguments", "")))
        reason = ledger.charge_tool_call(acc.name, acc.arguments)
        if reason is not None:
            return reason
        await self._close_tool_item(run, acc, emitter)
        return None

    async def _close_tool_item(
        self,
        run: _JobRun,
        acc: ToolCallAccumulator,
        emitter: ResponsesEventEmitter,
    ) -> None:
        """Emit the terminator pair for one tool call (铁律 2 / R-P1-22).

        ``function_call_arguments.done`` is emitted **only** when the
        accumulated arguments parse as a JSON object.  A client that
        ``JSON.parse``\\ s a truncated fragment would execute a mangled tool
        call, so an invalid call is closed as ``incomplete`` and never
        announced as done.
        """
        if acc.item_done:
            return
        valid = acc.validate_arguments()
        if valid:
            await self._emit(
                run,
                emitter,
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": acc.output_index,
                    "call_id": acc.call_id,
                    "arguments": acc.arguments,
                },
            )
        await self._emit(
            run,
            emitter,
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": acc.output_index,
                "item": run.tool_item(acc),
            },
        )
        acc.mark_item_done()

    async def _charge_text(
        self,
        run: _JobRun,
        text: str,
        tokens: int,
        ledger: BudgetLedger,
        emitter: ResponsesEventEmitter,
    ) -> TerminalReason | None:
        """Charge output tokens and append one delta to the message item.

        The text is accumulated on the run as well as emitted: ``output`` is
        rebuilt from :attr:`_JobRun.text_parts` at the terminal, so a delta
        that is only *sent* would be lost to every later ``retrieve`` (P0-6).
        """
        reason = ledger.charge_output_tokens(tokens)
        if reason is not None:
            return reason
        if not text:
            # An empty delta is not an event: replaying it would show a
            # catch-up reader a frame the live client never received.
            return None
        if run.message_output_index < 0:
            run.message_output_index = run.allocate_output_index()
            await self._emit(
                run,
                emitter,
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": run.message_output_index,
                    "item": {
                        "id": make_message_item_id(run.response_id, run.message_output_index),
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                    },
                },
            )
        run.text_parts.append(text)
        await self._emit(
            run,
            emitter,
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "output_index": run.message_output_index,
                "delta": text,
            },
        )
        return None

    async def _close_open_items(
        self,
        run: _JobRun,
        emitter: ResponsesEventEmitter,
        *,
        status: str,
    ) -> None:
        """Close every item this run opened, persisting each terminator.

        Every ``output_item.added`` needs its ``.done`` or the stream is
        malformed.  This is done here rather than left to
        :meth:`CircuitBreaker.trip` step 4 because the breaker writes through
        the emitter only: its frames never reach the event log, so a catch-up
        reader would replay a stream whose items are never closed (R-P1-36).
        """
        if run.message_output_index >= 0 and not run.message_done:
            run.message_done = True
            await self._emit(
                run,
                emitter,
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": run.message_output_index,
                    "item": run.message_item(status=status),
                },
            )
        for acc in run.tools.list_all():
            if acc.item_added and not acc.item_done:
                await self._close_tool_item(run, acc, emitter)

    async def _persist_output(self, run: _JobRun, *, status: str) -> list[dict[str, Any]]:
        """Write the rebuilt ``output`` array everywhere a reader looks for it.

        Both destinations are mandatory (P0-6): the ``responses.output``
        column is what :func:`endpoints.retrieve` deserialises, and
        ``response_output_items`` is what the items endpoint pages over.  The
        column is written by the caller's ``update_status`` -- which is why
        this returns the items instead of writing them alone.
        """
        items = run.output_items(status=status)
        if items:
            await self._store.save_output_items(run.response_id, items)
        return items

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
        # Before the breaker, never after: step 5 emits the single terminal
        # event, and an ``output_item.done`` written behind it would put a
        # frame after the end of the stream (铁律 3).
        await self._close_open_items(run, emitter, status="incomplete")
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
        # A truncated answer is still an answer: whatever was generated before
        # the ceiling is persisted, marked ``incomplete`` item by item, rather
        # than silently dropped (P0-6 + 铁律 2).
        items = await self._persist_output(run, status="incomplete")
        await self._store.update_status(
            run.response_id,
            "incomplete",
            workspace_id=run.workspace_id,
            terminal_reason=reason.value,
            incomplete_details=details,
            output=items,
        )
        await self._persist_terminal(
            run,
            "incomplete",
            {
                "terminal_reason": reason.value,
                "incomplete_details": details,
                "output": items,
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
        """P0-6: a ``completed`` background response carries its answer.

        The ``output`` array is rebuilt from what the run actually produced
        and written to *both* places a reader looks: the ``responses.output``
        column that ``retrieve`` deserialises and the ``response_output_items``
        table the items endpoint pages over.  A ``completed`` row with an
        empty output is not a finished job -- it is a lost one.
        """
        usage = {"output_tokens": ledger.output_tokens}
        await self._close_open_items(run, emitter, status="completed")
        items = await self._persist_output(run, status="completed")
        await self._store.update_status(
            run.response_id,
            "completed",
            workspace_id=run.workspace_id,
            terminal_reason=TerminalReason.NORMAL_FINISH.value,
            usage=usage,
            output=items,
        )
        emitter.terminate(
            ResponseStatus.COMPLETED,
            terminal_reason=TerminalReason.NORMAL_FINISH.value,
        )
        await self._persist_terminal(run, "completed", {"usage": usage, "output": items})
        await self._jobs.mark_terminal(run.task_id, "completed")
        return "completed"

    async def _finish_cancelled(
        self,
        run: _JobRun,
        emitter: ResponsesEventEmitter,
    ) -> str:
        """R-P1-35: a cancelled job keeps the partial answer it paid for.

        The tokens were already generated and already billed, so discarding
        them would mean the client pays for output it can never read.  Every
        item is closed as ``incomplete`` first -- a cancelled run is by
        definition not a finished one.
        """
        await run.cancel_upstream()
        reason = TerminalReason.CANCELLED_BY_CLIENT.value
        await self._close_open_items(run, emitter, status="incomplete")
        items = await self._persist_output(run, status="incomplete")
        await self._store.update_status(
            run.response_id,
            "cancelled",
            workspace_id=run.workspace_id,
            terminal_reason=reason,
            output=items,
        )
        emitter.terminate(ResponseStatus.CANCELLED, terminal_reason=reason)
        await self._persist_terminal(
            run,
            "cancelled",
            {"terminal_reason": reason, "output": items},
        )
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
        # This path is already handling one failure; a second one raised while
        # salvaging the partial output must not prevent the row from reaching
        # ``failed``.  A job stuck ``in_progress`` forever is strictly worse
        # than a failed job with an empty output.
        items: list[dict[str, Any]] = []
        try:
            await self._close_open_items(run, emitter, status="incomplete")
            items = await self._persist_output(run, status="incomplete")
        except Exception as salvage_exc:  # noqa: BLE001 - best effort only
            LOGGER.warning(
                "persisting partial output for failed job %s failed: %r",
                run.task_id,
                salvage_exc,
            )
        await self._store.update_status(
            run.response_id,
            "failed",
            workspace_id=run.workspace_id,
            terminal_reason=TerminalReason.UPSTREAM_ERROR.value,
            error=type(exc).__name__,
            output=items,
        )
        emitter.terminate(
            ResponseStatus.FAILED,
            terminal_reason=TerminalReason.UPSTREAM_ERROR.value,
            error=error,
        )
        await self._persist_terminal(run, "failed", {"error": error, "output": items})
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
