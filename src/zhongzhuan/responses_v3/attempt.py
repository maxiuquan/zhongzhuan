"""Upstream attempt / retry policy for one response (T23).

Covers R-P0-30 (upstream switching budget + repeated-truncation circuit) and
R-P0-34 (the closed retry whitelist).

The one rule that matters
-------------------------
**Once a single delta byte has reached the client, there is no second upstream
request.**  A retry after the first byte would replay text the client already
rendered, and the Responses stream has no "discard everything so far" event --
the user would simply see duplicated output, or worse, two half-answers spliced
together.  So :meth:`AttemptManager.is_retryable` returns ``False`` for *every*
error class once ``first_byte_sent`` is true, no matter how retryable that
class looks in isolation.

Before the first byte the client has seen nothing but ``response.created`` /
``response.in_progress``, so switching upstreams is invisible and therefore
allowed -- but only :attr:`AttemptManager.max_retries_before_first_byte` times
(default 2, from :attr:`ExecutionBudget.max_upstream_switches`).  Exhausting
that budget is ``RETRY_BUDGET_EXHAUSTED``, not an upstream error: the proxy ran
out of *its own* attempts.

The truncation circuit
----------------------
A stream that dies at the *same* logical position twice is not bad luck, it is
a deterministic upstream defect (a poisoned prompt, a provider-side content
filter, a broken tool schema).  Retrying it burns quota and produces the same
truncation, so the position is latched open in :attr:`_circuit_open` and every
later attempt at that position fails fast with ``RETRY_BUDGET_EXHAUSTED``.
"""
from __future__ import annotations

from typing import Any

from ..proxy.protocol.responses_errors import (
    TERMINAL_REASON_TO_ERROR_CLASS,
    is_retryable as _class_is_retryable,
)
from ..proxy.protocol.responses_models import ErrorClass, TerminalReason
from .budget import ExecutionBudget

#: Never retried regardless of budget (R-P0-34).  The first two are the
#: client's own fault -- replaying them just reproduces the same 4xx -- and the
#: last two mean nobody is left to read the answer.
NEVER_RETRYABLE: frozenset[ErrorClass] = frozenset({
    ErrorClass.INVALID_CLIENT_REQUEST,      # 400
    ErrorClass.UNSUPPORTED_INPUT_BLOCK,     # 400
    ErrorClass.UNSUPPORTED_TOOL_CAPABILITY, # 400
    ErrorClass.INVALID_TOOL_ARGUMENTS,      # 422-class
    ErrorClass.CLIENT_DISCONNECTED,         # client cancelled / went away
})

#: Truncations at one position before the position is latched open (R-P0-30).
TRUNCATION_CIRCUIT_THRESHOLD: int = 2


def _as_error_class(err: Any) -> ErrorClass | None:
    """Coerce an error-ish value into an :class:`ErrorClass` (``None`` if alien).

    Call sites legitimately hold either an :class:`ErrorClass` or the
    :class:`TerminalReason` it produced (``CANCELLED_BY_CLIENT`` only exists on
    the latter), so both are accepted rather than forcing every caller to
    translate first.
    """
    if isinstance(err, ErrorClass):
        return err
    if isinstance(err, TerminalReason):
        return TERMINAL_REASON_TO_ERROR_CLASS.get(err)
    if isinstance(err, str):
        try:
            return ErrorClass(err)
        except ValueError:
            try:
                return TERMINAL_REASON_TO_ERROR_CLASS.get(TerminalReason(err))
            except ValueError:
                return None
    return None


class AttemptManager:
    """Decide whether a failed upstream attempt may be retried (R-P0-30/34).

    One instance per response.  It is deliberately *not* a general retry
    helper: it knows about the downstream stream's visibility (``first_byte_sent``)
    because that, not the error, is what makes a retry safe or unsafe.
    """

    def __init__(
        self,
        budget: ExecutionBudget,
        *,
        max_retries_before_first_byte: int | None = None,
    ) -> None:
        self.budget = budget
        self.max_retries_before_first_byte = (
            int(budget.max_upstream_switches)
            if max_retries_before_first_byte is None
            else int(max_retries_before_first_byte)
        )
        self.upstream_switches = 0
        self._truncation_positions: dict[str, int] = {}
        self._circuit_open: set[str] = set()

    # -- whitelist -----------------------------------------------------------

    def is_retryable(self, err_class: Any, *, first_byte_sent: bool) -> bool:
        """Whether ``err_class`` is retryable *given the stream's visibility*.

        Four classes are retryable before the first byte (R-P0-34):
        ``UPSTREAM_CONNECT_ERROR`` (DNS / TCP / TLS -- the transport layer has
        no separate class, all connection establishment failures land here),
        ``UPSTREAM_RATE_LIMITED`` (429), ``UPSTREAM_SERVER_ERROR`` (5xx) and
        ``FIRST_TOKEN_TIMEOUT``.  Everything else -- including
        ``UPSTREAM_TRUNCATED`` and ``READ_IDLE_TIMEOUT``, which by definition
        happen *after* bytes flowed -- is not.
        """
        if first_byte_sent:
            return False
        resolved = _as_error_class(err_class)
        if resolved is None or resolved in NEVER_RETRYABLE:
            return False
        return _class_is_retryable(resolved)

    # -- decision ------------------------------------------------------------

    def should_retry(
        self,
        err_class: Any,
        *,
        first_byte_sent: bool,
        position: str = "",
    ) -> tuple[bool, TerminalReason | None]:
        """Full retry decision: ``(retry?, terminal_reason_if_giving_up)``.

        A ``None`` reason means "do not retry, but the error class already
        explains why" -- the caller surfaces the original error.  A non-``None``
        reason means the *proxy's* budget is what ended the response, and it
        must be reported as ``incomplete_details.reason``.

        DEVIATION (§3.10): the whitelist check runs *after* the
        ``first_byte_sent`` branch rather than first.  Ordering it first would
        make the truncation circuit dead code, because every post-first-byte
        error is unretryable by definition and would short-circuit before the
        position could ever be counted.  The observable outcomes for the
        documented cases are unchanged.
        """
        # 1. This position already proved deterministic -- fail fast.
        if position and position in self._circuit_open:
            return False, TerminalReason.RETRY_BUDGET_EXHAUSTED

        # 2. After the first delta: never retry, but do count truncations so a
        #    repeat at the same position latches the circuit open.
        if first_byte_sent:
            if position:
                seen = self._truncation_positions.get(position, 0) + 1
                self._truncation_positions[position] = seen
                if seen >= TRUNCATION_CIRCUIT_THRESHOLD:
                    self._circuit_open.add(position)
                    return False, TerminalReason.RETRY_BUDGET_EXHAUSTED
            return False, None

        # 3. Before the first byte: only whitelisted classes may be retried.
        if not self.is_retryable(err_class, first_byte_sent=False):
            return False, None

        # 4. ... and only while the switching budget lasts.
        self.upstream_switches += 1
        if self.upstream_switches > self.max_retries_before_first_byte:
            return False, TerminalReason.RETRY_BUDGET_EXHAUSTED
        return True, None

    # -- helpers -------------------------------------------------------------

    @property
    def circuit_open_positions(self) -> frozenset[str]:
        """Positions latched open by repeated truncation (read-only view)."""
        return frozenset(self._circuit_open)

    def truncations_at(self, position: str) -> int:
        """How many truncations have been recorded at ``position``."""
        return self._truncation_positions.get(position, 0)

    def reset(self) -> None:
        """Clear all attempt state (new response, or test isolation)."""
        self.upstream_switches = 0
        self._truncation_positions.clear()
        self._circuit_open.clear()


__all__ = [
    "NEVER_RETRYABLE",
    "TRUNCATION_CIRCUIT_THRESHOLD",
    "AttemptManager",
]
