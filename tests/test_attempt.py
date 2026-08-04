"""T23 tests: upstream retry policy (R-P0-30 / R-P0-34).

Acceptance criteria -> test functions:

* ④ R-P0-30, switching budget and the truncation circuit:
  :func:`test_no_retry_after_third_first_byte_failure`,
  :func:`test_no_retry_after_delta_sent`,
  :func:`test_circuit_open_on_repeated_truncation_same_position`.
* ⑤ R-P0-34, the closed retry whitelist -- four retryable, three never:
  :func:`test_retryable_dns_connect`, :func:`test_retryable_tls`,
  :func:`test_retryable_429`, :func:`test_retryable_5xx`,
  :func:`test_retryable_first_token_timeout`,
  :func:`test_not_retryable_400`, :func:`test_not_retryable_422`,
  :func:`test_not_retryable_client_cancel`,
  :func:`test_no_retry_after_first_byte_even_if_retryable`.
"""

from __future__ import annotations

from zhongzhuan.proxy.protocol.responses_models import ErrorClass, TerminalReason
from zhongzhuan.responses_v3.attempt import AttemptManager
from zhongzhuan.responses_v3.budget import SYNC_BUDGET, ExecutionBudget


def manager(**kwargs) -> AttemptManager:
    return AttemptManager(SYNC_BUDGET, **kwargs)


# ---------------------------------------------------------------------------
# ⑤ R-P0-34 -- the whitelist
# ---------------------------------------------------------------------------


def test_retryable_dns_connect():
    """DNS / TCP failure: nothing was sent downstream, so a retry is invisible."""
    assert manager().is_retryable(ErrorClass.UPSTREAM_CONNECT_ERROR, first_byte_sent=False) is True


def test_retryable_tls():
    """TLS handshake failure.

    There is no dedicated ``UPSTREAM_TLS_ERROR`` class in §10.2's closed set of
    14 -- every connection-establishment failure (DNS, TCP, TLS) is classified
    as ``UPSTREAM_CONNECT_ERROR``, which is what a TLS failure is reported as.
    """
    assert manager().is_retryable(ErrorClass.UPSTREAM_CONNECT_ERROR, first_byte_sent=False) is True


def test_retryable_429():
    assert manager().is_retryable(ErrorClass.UPSTREAM_RATE_LIMITED, first_byte_sent=False) is True


def test_retryable_5xx():
    assert manager().is_retryable(ErrorClass.UPSTREAM_SERVER_ERROR, first_byte_sent=False) is True


def test_retryable_first_token_timeout():
    assert manager().is_retryable(ErrorClass.FIRST_TOKEN_TIMEOUT, first_byte_sent=False) is True


def test_not_retryable_400():
    """Replaying a malformed request only reproduces the same 400."""
    assert manager().is_retryable(ErrorClass.INVALID_CLIENT_REQUEST, first_byte_sent=False) is False


def test_not_retryable_422():
    assert manager().is_retryable(ErrorClass.INVALID_TOOL_ARGUMENTS, first_byte_sent=False) is False


def test_not_retryable_client_cancel():
    """Nobody is left to read the retried answer."""
    assert manager().is_retryable(ErrorClass.CLIENT_DISCONNECTED, first_byte_sent=False) is False
    assert manager().is_retryable(TerminalReason.CANCELLED_BY_CLIENT, first_byte_sent=False) is False


def test_no_retry_after_first_byte_even_if_retryable():
    """Visibility, not the error, decides: a retry would duplicate output."""
    assert manager().is_retryable(ErrorClass.UPSTREAM_SERVER_ERROR, first_byte_sent=True) is False
    assert manager().is_retryable(ErrorClass.FIRST_TOKEN_TIMEOUT, first_byte_sent=True) is False


def test_post_first_byte_classes_are_not_retryable():
    """Errors that can only happen mid-stream are never in the whitelist."""
    mgr = manager()
    assert mgr.is_retryable(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=False) is False
    assert mgr.is_retryable(ErrorClass.READ_IDLE_TIMEOUT, first_byte_sent=False) is False


def test_unknown_error_value_is_not_retryable():
    """An unrecognised classification must fail closed, not open."""
    assert manager().is_retryable("not_a_real_error", first_byte_sent=False) is False
    assert manager().is_retryable(None, first_byte_sent=False) is False


# ---------------------------------------------------------------------------
# ④ R-P0-30 -- switching budget and the truncation circuit
# ---------------------------------------------------------------------------


def test_no_retry_after_third_first_byte_failure():
    """Two pre-first-byte switches are allowed; the third exhausts the budget."""
    mgr = manager()
    assert mgr.should_retry(ErrorClass.FIRST_TOKEN_TIMEOUT, first_byte_sent=False) == (True, None)
    assert mgr.should_retry(ErrorClass.FIRST_TOKEN_TIMEOUT, first_byte_sent=False) == (True, None)
    assert mgr.should_retry(ErrorClass.FIRST_TOKEN_TIMEOUT, first_byte_sent=False) == (
        False,
        TerminalReason.RETRY_BUDGET_EXHAUSTED,
    )
    assert mgr.upstream_switches == 3


def test_no_retry_after_delta_sent():
    """R-P0-30: once a delta is out there is no second upstream request."""
    mgr = manager()
    assert mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True) == (False, None)
    assert mgr.upstream_switches == 0, "a post-delta failure must not spend budget"


def test_circuit_open_on_repeated_truncation_same_position():
    """The same position dying twice is a deterministic defect, not bad luck."""
    mgr = manager()
    first = mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True, position="item_3/args")
    assert first == (False, None)
    assert "item_3/args" not in mgr._circuit_open

    second = mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True, position="item_3/args")
    assert second == (False, TerminalReason.RETRY_BUDGET_EXHAUSTED)
    assert "item_3/args" in mgr._circuit_open
    assert mgr.truncations_at("item_3/args") == 2


def test_open_circuit_short_circuits_later_attempts():
    """A latched position fails fast, before any budget is spent."""
    mgr = manager()
    for _ in range(2):
        mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True, position="p")
    assert mgr.should_retry(ErrorClass.FIRST_TOKEN_TIMEOUT, first_byte_sent=False, position="p") == (
        False,
        TerminalReason.RETRY_BUDGET_EXHAUSTED,
    )
    assert mgr.upstream_switches == 0


def test_distinct_positions_do_not_share_a_circuit():
    """Two different failure points are two independent signals."""
    mgr = manager()
    assert mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True, position="a") == (False, None)
    assert mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True, position="b") == (False, None)
    assert mgr.circuit_open_positions == frozenset()


def test_non_whitelisted_error_never_spends_budget():
    """A 400 must not consume a switch -- it would mask a later real retry."""
    mgr = manager()
    assert mgr.should_retry(ErrorClass.INVALID_CLIENT_REQUEST, first_byte_sent=False) == (False, None)
    assert mgr.upstream_switches == 0
    assert mgr.should_retry(ErrorClass.UPSTREAM_SERVER_ERROR, first_byte_sent=False) == (True, None)


def test_retry_budget_defaults_to_budget_max_upstream_switches():
    """The manager inherits its ceiling from the execution budget by default."""
    assert manager().max_retries_before_first_byte == SYNC_BUDGET.max_upstream_switches == 2
    narrowed = AttemptManager(SYNC_BUDGET, max_retries_before_first_byte=0)
    assert narrowed.should_retry(ErrorClass.UPSTREAM_SERVER_ERROR, first_byte_sent=False) == (
        False,
        TerminalReason.RETRY_BUDGET_EXHAUSTED,
    )


def test_custom_budget_widens_the_switch_ceiling():
    mgr = AttemptManager(ExecutionBudget(max_upstream_switches=4))
    for _ in range(4):
        assert mgr.should_retry(ErrorClass.UPSTREAM_CONNECT_ERROR, first_byte_sent=False) == (True, None)
    assert mgr.should_retry(ErrorClass.UPSTREAM_CONNECT_ERROR, first_byte_sent=False) == (
        False,
        TerminalReason.RETRY_BUDGET_EXHAUSTED,
    )


def test_reset_clears_all_attempt_state():
    mgr = manager()
    mgr.should_retry(ErrorClass.UPSTREAM_SERVER_ERROR, first_byte_sent=False)
    for _ in range(2):
        mgr.should_retry(ErrorClass.UPSTREAM_TRUNCATED, first_byte_sent=True, position="p")
    assert mgr.upstream_switches == 1 and mgr.circuit_open_positions

    mgr.reset()
    assert mgr.upstream_switches == 0
    assert mgr.circuit_open_positions == frozenset()
    assert mgr.truncations_at("p") == 0
