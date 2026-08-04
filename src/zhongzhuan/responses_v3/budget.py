"""Execution budget, tool-loop signatures and the circuit breaker (T23).

Covers R-P0-27 (six execution ceilings), R-P0-28 (no-progress loop detection)
and R-P0-32 (the ten circuit-breaker reasons + the mandatory six-step trip).

Why this module exists
----------------------
An agentic ``/v1/responses`` turn is an *unbounded* loop by construction: the
model emits a tool call, the bridge runs it, feeds the result back and asks
again.  Nothing in the protocol stops a model from calling ``read_file`` on
the same path forever, from producing 5 MB of output text, or from keeping a
connection open for an hour.  Every one of those is a real outage: the client
sees a stream that never ends, the upstream bill grows without limit, and the
proxy's worker is pinned.

:class:`ExecutionBudget` is the *policy* (immutable, per request class),
:class:`BudgetLedger` is the *meter* (mutable, one per response), and
:class:`CircuitBreaker` is the *actuator* that turns a tripped meter into a
well-formed terminal SSE event.

Progress, not repetition (R-P0-28)
----------------------------------
A loop is only harmful when it makes no progress.  :func:`tool_signature`
therefore hashes a *canonical* view of a call: JSON key order, insignificant
whitespace and the ``call_id`` (which the model regenerates every round) are
all normalised away, so three "different-looking" calls that ask the same
question collapse onto one signature and trip ``REPEATED_TOOL_CALL``.  The
same signature plus the same failing result trips ``REPEATED_TOOL_FAILURE``:
retrying a call that already failed identically twice is definitionally
progress-free.

Honest scope note
-----------------
The live streaming pipeline does not exist yet (``pipeline.py`` is still the
T21 skeleton).  Everything here is therefore **self-contained and injectable**:
:meth:`CircuitBreaker.trip` talks to its collaborators through the narrow
:class:`SupportsUpstreamControl` / :class:`SupportsAudit` protocols and never
imports the pipeline.  Real side-effect rollback (step 3) and the real audit
sink (step 6) are wired in T24/T28; see the ``HONEST STUB`` markers below.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..proxy.protocol.responses_emitter import ResponsesEventEmitter
from ..proxy.protocol.responses_errors import to_incomplete_details
from ..proxy.protocol.responses_models import (
    CIRCUIT_BREAKER_REASONS,
    ResponseStatus,
    TerminalReason,
    canonical_json,
)

#: Logger used for the step-6 structured audit line.
LOGGER = logging.getLogger("zhongzhuan.responses_v3.budget")

#: Argument key stripped before hashing: the model mints a fresh ``call_id``
#: every round, so keeping it would defeat repeat detection entirely (§9.4).
CALL_ID_KEY: str = "call_id"

#: The six steps of a circuit-breaker trip, in the order §3.10 mandates.
TRIP_STEPS: tuple[str, ...] = (
    "stop_upstream_read",
    "stop_scheduling",
    "rollback_side_effects",
    "close_open_items",
    "emit_terminal",
    "write_audit",
)


# ---------------------------------------------------------------------------
# 1. Collaborator protocols (structural -- the real classes land in T24/T28)
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsUpstreamControl(Protocol):
    """The two pipeline hooks the breaker needs to stop work (§3.10 steps 1-2)."""

    async def cancel_upstream(self) -> None:
        """Abort the in-flight upstream read so no further bytes arrive."""

    def stop_scheduling(self) -> None:
        """Refuse to schedule any further tool round for this response."""


@runtime_checkable
class SupportsAudit(Protocol):
    """Minimal audit sink (``ResponseStore`` grows this method in T24)."""

    def record_audit(self, event: dict[str, Any]) -> Any:
        """Persist one audit event; may be sync or async."""


class SupportsRequestIdentity(Protocol):
    """The only ``RequestContext`` fields the breaker reads.

    Deliberately narrow: :class:`~zhongzhuan.proxy.context.RequestContext` is a
    ``slots`` dataclass that requires a live ``aiohttp`` request, which would
    make the breaker untestable if it were a hard dependency.
    """

    request_id: str
    workspace_id: str


# ---------------------------------------------------------------------------
# 2. Tool-call signatures (R-P0-28)
# ---------------------------------------------------------------------------


def _canonical_arguments(arguments: Any) -> str:
    """Normalise tool arguments into their canonical JSON form.

    Normalisation removes exactly the three kinds of difference §9.4 declares
    meaningless: JSON key order, insignificant whitespace and ``call_id``.
    An empty / missing argument blob is treated as ``{}`` so ``""``, ``None``
    and ``"{}"`` share a signature.  Unparseable text cannot be round-tripped,
    so it falls back to whitespace-collapsed raw text -- still stable, just
    coarser.
    """
    obj: Any
    if arguments is None:
        obj = {}
    elif isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            obj = {}
        else:
            try:
                obj = json.loads(text)
            except (ValueError, TypeError):
                return " ".join(text.split())
    else:
        obj = arguments

    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k != CALL_ID_KEY}
    try:
        return canonical_json(obj)
    except (TypeError, ValueError):
        return " ".join(str(obj).split())


def _normalize_result(result_or_error: Any) -> str:
    """Coerce a tool result / error into a stable comparison string."""
    if result_or_error is None:
        return ""
    if isinstance(result_or_error, (dict, list)):
        try:
            return canonical_json(result_or_error)
        except (TypeError, ValueError):
            return str(result_or_error).strip()
    return str(result_or_error).strip()


def tool_signature(
    name: str,
    arguments: Any = "",
    result_or_error: Any = "",
) -> str:
    """Stable ``sha256`` digest identifying one tool call (and its outcome).

    Two calls share a signature **iff** they ask the same tool the same
    question -- key order, whitespace and ``call_id`` are normalised away
    (R-P0-28).  Passing ``result_or_error`` extends the identity to "the same
    call that produced the same answer", which is what
    :meth:`BudgetLedger.charge_tool_result` needs to detect a stuck retry.

    DEVIATION (§3.10): the three parts are joined with ``\\x00`` rather than
    plain concatenation.  Bare concatenation is ambiguous -- ``("ab", "c")``
    and ``("a", "bc")`` would hash identically -- and ``\\x00`` cannot occur in
    canonical JSON, so the separator is collision-free.
    """
    parts = (
        str(name or ""),
        _canonical_arguments(arguments),
        _normalize_result(result_or_error),
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 3. ExecutionBudget (§3.10 / R-P0-27)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionBudget:
    """Immutable ceilings for one response's agentic loop (R-P0-27).

    All eight knobs are *hard* ceilings: crossing one is a circuit-breaker
    trip, never a warning.  The defaults are the §3.10 synchronous profile.

    ``max_wall_time_seconds`` has no "unlimited" encoding on purpose (Q8): a
    response with no time ceiling is exactly the outage this module exists to
    prevent, so ``0`` / ``None`` raise :class:`ValueError` at construction
    rather than silently disabling the guard.
    """

    #: Tool-call rounds (one round = one upstream turn that emitted tool calls).
    max_tool_rounds: int = 32
    #: Calls to a single tool name across the whole response.
    max_calls_per_tool: int = 8
    #: Repeats of one identical signature before it counts as a stuck loop.
    max_identical_call_repeats: int = 2
    #: Tool calls across all tools.
    max_total_tool_calls: int = 64
    #: Wall-clock ceiling for the whole response (Q8: must be > 0).
    max_wall_time_seconds: int = 900
    #: Output tokens across all items of the response.
    max_output_tokens_total: int = 200_000
    #: Informational mirror of the chain ceiling.  The authoritative depth
    #: guard lives in :mod:`.chain` (T22 / R-P0-29); the budget does not
    #: re-enforce it, it only carries the number for reporting.
    max_chain_depth: int = 64
    #: Upstream switches allowed *before the first byte* reaches the client
    #: (R-P0-30).  After the first delta no switch is ever allowed.
    max_upstream_switches: int = 2

    def __post_init__(self) -> None:
        wall = self.max_wall_time_seconds
        if wall is None or isinstance(wall, bool) or not isinstance(wall, int):
            raise ValueError("max_wall_time_seconds must be a positive int, got {0!r}".format(wall))
        if wall <= 0:
            raise ValueError("max_wall_time_seconds must be > 0 (no unlimited budget, Q8), got {0!r}".format(wall))


#: Synchronous request profile: the §3.10 defaults (15 min wall clock).
SYNC_BUDGET: ExecutionBudget = ExecutionBudget()

#: Background (``background=true``) profile: same ceilings, 1 hour wall clock.
BACKGROUND_BUDGET: ExecutionBudget = ExecutionBudget(max_wall_time_seconds=3600)

# Exposed as class attributes too, so ``ExecutionBudget.SYNC_BUDGET`` reads
# naturally at call sites.  Assigned after the class body because a value of
# the dataclass's own type inside it would become a *field*.
ExecutionBudget.SYNC_BUDGET = SYNC_BUDGET  # type: ignore[attr-defined]
ExecutionBudget.BACKGROUND_BUDGET = BACKGROUND_BUDGET  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 4. BudgetLedger (§3.10 / R-P0-27 / R-P0-28)
# ---------------------------------------------------------------------------


@dataclass
class BudgetLedger:
    """Mutable meter for one response, charged as the loop progresses.

    Every ``charge_*`` method returns the :class:`TerminalReason` that was
    tripped, or ``None`` when there is still budget left.  Charges are applied
    **before** the comparison, so a ceiling of ``N`` permits exactly ``N``
    units and the ``N+1``-th call trips.

    The caller is responsible for acting on a returned reason (normally by
    handing it to :meth:`CircuitBreaker.trip`); the ledger itself has no side
    effects and never raises.
    """

    budget: ExecutionBudget
    started_at: float = field(default_factory=time.monotonic)
    tool_rounds: int = 0
    total_tool_calls: int = 0
    calls_per_tool: dict[str, int] = field(default_factory=dict)
    identical_signatures: dict[str, int] = field(default_factory=dict)
    failure_signatures: dict[str, int] = field(default_factory=dict)
    output_tokens: int = 0
    upstream_switches: int = 0

    # -- rounds --------------------------------------------------------------

    def charge_round(self) -> TerminalReason | None:
        """Charge one tool-call round (``MAX_TOOL_ROUNDS``)."""
        self.tool_rounds += 1
        if self.tool_rounds > self.budget.max_tool_rounds:
            return TerminalReason.MAX_TOOL_ROUNDS
        return None

    # -- tool calls ----------------------------------------------------------

    def charge_tool_call(
        self,
        name: str,
        args_canonical: Any = "",
    ) -> TerminalReason | None:
        """Charge one tool call and report the first ceiling it crosses.

        Priority is deliberate: the *most specific* diagnosis wins.  An
        identical repeat is a stuck model, a per-tool flood is a stuck tool,
        and only when neither applies is the generic total the real cause.
        Both of the first two map to ``REPEATED_TOOL_CALL`` -- §9.4 has no
        separate "per-tool" reason and inventing one would break the closed
        ten-reason set of R-P0-32.
        """
        sig = tool_signature(name, args_canonical)
        self.identical_signatures[sig] = self.identical_signatures.get(sig, 0) + 1
        self.calls_per_tool[name] = self.calls_per_tool.get(name, 0) + 1
        self.total_tool_calls += 1

        if self.identical_signatures[sig] > self.budget.max_identical_call_repeats:
            return TerminalReason.REPEATED_TOOL_CALL
        if self.calls_per_tool[name] > self.budget.max_calls_per_tool:
            return TerminalReason.REPEATED_TOOL_CALL
        if self.total_tool_calls > self.budget.max_total_tool_calls:
            return TerminalReason.MAX_TOTAL_TOOL_CALLS
        return None

    def charge_tool_result(
        self,
        signature: str,
        failed: bool,
    ) -> TerminalReason | None:
        """Charge one tool *result* (``REPEATED_TOOL_FAILURE``).

        ``signature`` must include the outcome -- i.e. be built with
        ``tool_signature(name, args, result_or_error)`` -- so that "same call,
        same failure" is what accumulates.  Successful results are free: a
        loop that keeps succeeding is still making progress and is bounded by
        the call ceilings instead.
        """
        if not failed:
            return None
        self.failure_signatures[signature] = self.failure_signatures.get(signature, 0) + 1
        if self.failure_signatures[signature] > self.budget.max_identical_call_repeats:
            return TerminalReason.REPEATED_TOOL_FAILURE
        return None

    # -- output / switches / time --------------------------------------------

    def charge_output_tokens(self, n: int) -> TerminalReason | None:
        """Charge ``n`` output tokens (``MAX_OUTPUT_BUDGET``)."""
        self.output_tokens += int(n)
        if self.output_tokens > self.budget.max_output_tokens_total:
            return TerminalReason.MAX_OUTPUT_BUDGET
        return None

    def charge_upstream_switch(self) -> TerminalReason | None:
        """Charge one pre-first-byte upstream switch (R-P0-30).

        Exhaustion maps to ``RETRY_BUDGET_EXHAUSTED``: from the client's point
        of view the proxy ran out of upstreams to try, which is a retry-budget
        problem, not an upstream error.
        """
        self.upstream_switches += 1
        if self.upstream_switches > self.budget.max_upstream_switches:
            return TerminalReason.RETRY_BUDGET_EXHAUSTED
        return None

    def check_wall_time(self, now: float | None = None) -> TerminalReason | None:
        """Check the wall-clock ceiling (``MAX_RESPONSE_TIME``).

        Uses :func:`time.monotonic` so a clock adjustment mid-response cannot
        extend or shorten the budget.
        """
        current = time.monotonic() if now is None else float(now)
        if current - self.started_at > self.budget.max_wall_time_seconds:
            return TerminalReason.MAX_RESPONSE_TIME
        return None

    # -- introspection -------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Counter snapshot for logs / audit events (no budget policy inside)."""
        return {
            "tool_rounds": self.tool_rounds,
            "total_tool_calls": self.total_tool_calls,
            "distinct_tools": len(self.calls_per_tool),
            "distinct_signatures": len(self.identical_signatures),
            "output_tokens": self.output_tokens,
            "upstream_switches": self.upstream_switches,
        }


# ---------------------------------------------------------------------------
# 5. CircuitBreaker (§3.10 / R-P0-32)
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Turn a tripped budget into exactly one well-formed terminal event.

    The six steps of :meth:`trip` are ordered, not incidental (§3.10):

    1. **stop_upstream_read** -- stop paying for and buffering bytes we have
       already decided to discard;
    2. **stop_scheduling** -- make sure no new tool round starts while we tear
       down (otherwise step 4 races a freshly opened item);
    3. **rollback_side_effects** -- record / undo work that must not be left
       half-applied, *before* the client is told the turn is over;
    4. **close_open_items** -- every ``output_item.added`` needs its ``.done``
       or the stream is malformed;
    5. **emit_terminal** -- the single ``response.incomplete`` +
       ``incomplete_details.reason`` + ``[DONE]``;
    6. **write_audit** -- durable record, last because it must not delay the
       client's terminal frame.

    铁律: a trip emits **exactly one** terminal event and one ``[DONE]``.
    Every step is individually guarded so an exception in steps 1-4 can never
    prevent step 5, and the emitter's own exactly-once latch makes a second
    trip on the same emitter a safe no-op.
    """

    def __init__(self) -> None:
        #: Step names appended in execution order; reset on every trip.
        self.last_steps: list[str] = []
        #: Side effects observed at step 3 (HONEST STUB: recorded, not undone).
        self.side_effects_log: list[dict[str, Any]] = []
        #: Exceptions swallowed during the last trip, for diagnostics.
        self.step_errors: list[str] = []

    async def trip(
        self,
        reason: TerminalReason,
        ctx: Any,
        pipeline: Any,
        emitter: ResponsesEventEmitter,
        turn: Any,
        store: Any,
        *,
        message: str = "",
    ) -> list[bytes]:
        """Run the six-step teardown and return the frames to write.

        DEVIATION (§3.10): the return value is the concatenation of the step-4
        ``output_item.done`` frames and the step-5 terminal frames, not step 5
        alone.  Dropping step 4's frames would leave the caller unable to write
        a well-formed stream, which contradicts the very reason step 4 exists.
        """
        self.last_steps = []
        self.step_errors = []
        text = message or "terminated: {0}".format(reason.value)
        frames: list[bytes] = []

        await self._run_async_step("stop_upstream_read", self._stop_upstream_read, pipeline)
        self._run_step("stop_scheduling", self._stop_scheduling, pipeline)
        self._run_step("rollback_side_effects", self._rollback_side_effects, ctx, reason, store)
        frames += self._run_step("close_open_items", self._close_open_items, turn, emitter) or []
        frames += self._run_step("emit_terminal", self._emit_terminal, reason, text, emitter) or []
        self._run_step("write_audit", self._write_audit, ctx, reason, text, store)
        return frames

    # -- step runners --------------------------------------------------------

    def _run_step(self, step: str, fn: Any, *args: Any) -> Any:
        """Run one sync step, always recording its name, never propagating."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            self.step_errors.append("{0}: {1!r}".format(step, exc))
            LOGGER.warning("circuit breaker step %s failed: %r", step, exc)
            return None
        finally:
            self.last_steps.append(step)

    async def _run_async_step(self, step: str, fn: Any, *args: Any) -> Any:
        """Await one async step, always recording its name, never propagating."""
        try:
            return await fn(*args)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            self.step_errors.append("{0}: {1!r}".format(step, exc))
            LOGGER.warning("circuit breaker step %s failed: %r", step, exc)
            return None
        finally:
            self.last_steps.append(step)

    # -- the six steps -------------------------------------------------------

    @staticmethod
    async def _stop_upstream_read(pipeline: Any) -> None:
        """Step 1: abort the upstream read (no-op if the pipeline lacks it)."""
        hook = getattr(pipeline, "cancel_upstream", None)
        if hook is None:
            return
        result = hook()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _stop_scheduling(pipeline: Any) -> None:
        """Step 2: forbid any further tool round for this response."""
        hook = getattr(pipeline, "stop_scheduling", None)
        if hook is not None:
            hook()

    def _rollback_side_effects(
        self,
        ctx: Any,
        reason: TerminalReason,
        store: Any,
    ) -> None:
        """Step 3: account for uncommitted side effects.

        HONEST STUB: the bridge owns no transactional side effects yet, so this
        records the intent (audit event + :attr:`side_effects_log`) instead of
        undoing anything.  T24 replaces the body with the real rollback once
        the tool executor exists; the step slot and its ordering are already
        contractual so that change cannot reorder the teardown.
        """
        entry = {
            "reason": reason.value,
            "request_id": _identity(ctx, "request_id"),
            "workspace_id": _identity(ctx, "workspace_id"),
            "rolled_back": 0,
            "stub": True,
        }
        self.side_effects_log.append(entry)
        _record_audit(store, {"event": "circuit_breaker.rollback", **entry})

    @staticmethod
    def _close_open_items(turn: Any, emitter: ResponsesEventEmitter) -> list[bytes]:
        """Step 4: emit ``output_item.done`` for every still-open item."""
        opener = getattr(turn, "open_items", None)
        if opener is None:
            return []
        frames: list[bytes] = []
        for item in opener() or []:
            frames += emitter.close_item(item, status="incomplete")
        return frames

    @staticmethod
    def _emit_terminal(
        reason: TerminalReason,
        message: str,
        emitter: ResponsesEventEmitter,
    ) -> list[bytes]:
        """Step 5: the single terminal event + ``[DONE]`` (exactly once)."""
        return emitter.terminate(
            ResponseStatus.INCOMPLETE,
            terminal_reason=reason.value,
            incomplete_details=to_incomplete_details(reason, message),
        )

    def _write_audit(
        self,
        ctx: Any,
        reason: TerminalReason,
        message: str,
        store: Any,
    ) -> None:
        """Step 6: structured log line + durable audit event."""
        event = {
            "event": "circuit_breaker.trip",
            "reason": reason.value,
            "is_circuit_breaker_reason": reason in CIRCUIT_BREAKER_REASONS,
            "request_id": _identity(ctx, "request_id"),
            "workspace_id": _identity(ctx, "workspace_id"),
            "message": message,
            "steps": list(self.last_steps),
        }
        LOGGER.info(
            "circuit breaker tripped reason=%s request_id=%s workspace_id=%s",
            event["reason"],
            event["request_id"],
            event["workspace_id"],
            extra={"circuit_breaker": event},
        )
        _record_audit(store, event)


def _identity(ctx: Any, attr: str) -> str:
    """Read one identity field off any context-shaped object (never raises)."""
    return str(getattr(ctx, attr, "") or "")


def _record_audit(store: Any, event: dict[str, Any]) -> None:
    """Best-effort audit write; a failing sink must never break a teardown.

    An async ``record_audit`` is scheduled rather than awaited so the audit
    sink can never delay the client's terminal frame (§3.10 step 6 is last for
    exactly that reason).
    """
    hook = getattr(store, "record_audit", None)
    if hook is None:
        return
    try:
        result = hook(event)
        if inspect.isawaitable(result):
            asyncio.get_running_loop().create_task(_await_quietly(result))
    except Exception as exc:  # noqa: BLE001 - audit is best-effort
        LOGGER.warning("audit sink rejected event %s: %r", event.get("event"), exc)


async def _await_quietly(awaitable: Any) -> None:
    """Await a fire-and-forget audit write, swallowing its failure."""
    try:
        await awaitable
    except Exception as exc:  # noqa: BLE001 - audit is best-effort
        LOGGER.warning("async audit sink failed: %r", exc)


__all__ = [
    "LOGGER",
    "CALL_ID_KEY",
    "TRIP_STEPS",
    "SupportsUpstreamControl",
    "SupportsAudit",
    "SupportsRequestIdentity",
    "ExecutionBudget",
    "SYNC_BUDGET",
    "BACKGROUND_BUDGET",
    "BudgetLedger",
    "CircuitBreaker",
    "tool_signature",
]
