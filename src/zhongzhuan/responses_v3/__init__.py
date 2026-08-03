"""Responses Bridge v3 resource layer (T21 / R-P1-28..33 / R-P0-40).

The ``responses_v3`` package implements the six official ``/v1/responses``
resource endpoints on top of :class:`~zhongzhuan.store.response_store.ResponseStore`:

    POST   /v1/responses                      -> create
    GET    /v1/responses/{id}                 -> retrieve
    DELETE /v1/responses/{id}                 -> delete
    POST   /v1/responses/{id}/cancel          -> cancel
    POST   /v1/responses/compact              -> compact (honest 501 stub)
    GET    /v1/responses/{id}/input_items     -> input_items (paginated)

T22 adds :mod:`.chain`: ``previous_response_id`` recovery with the R-P0-29
cycle / depth / budget guards, feeding a reasoning-free replay array.

T23 adds :mod:`.budget` and :mod:`.attempt`: the execution ceilings of
R-P0-27, the no-progress loop signatures of R-P0-28, the six-step circuit
breaker of R-P0-32 and the upstream retry policy of R-P0-30 / R-P0-34.  They
are self-contained and injectable -- the live pipeline consumes them in T24.

T24 adds :mod:`.background` and :mod:`.catchup`: the detached ``background=true``
worker (lease / heartbeat / cooperative cancel / bounded recovery, R-P1-34..37)
running under its own :data:`BACKGROUND_BUDGET`, and the catch-up replay that
reissues the persisted event log byte-for-byte (R-P1-36).  The worker's
execution source is still injected -- the real upstream tool loop is T28.

T25 adds :mod:`.capability` and :mod:`.passthrough`: the three-tier execution
mode router (native > emulate > translate, R-P1-45) with its startup gap report
and ``strict_capability_startup`` fail-closed check, plus the native
``/v1/responses`` passthrough that refuses to be downgraded to Chat Completions
(R-P1-44).  The passthrough's upstream transport is injected -- the live HTTP
client is T28.

T26 adds :mod:`.hosted_tools`: hosted tool 的 schema 识别与持久化（R-P1-46 闸门
①）、§4-Q4 的 400 ``unsupported_tool`` 请求期拒绝（闸门④）、``tool_choice``
四形态校验（R-P1-48），以及运行期才暴露时的 ``capability_route_unavailable``
终止事件构造器 —— 事件真正进 SSE 流由 T28 接线。

It is the successor to the legacy ``/v1/responses`` handler and is selected by
the v3 feature switch (T12).  This module is a **skeleton** on the critical
path: persistence + object mapping + routing are real and tested; the live
upstream streaming pipeline is wired in T24/T28 and the SDK contract is sealed
in T37.
"""
from __future__ import annotations

from ..proxy.protocol.responses_models import TerminalReason
from ..store.background_jobs import BackgroundJobStore
from .attempt import AttemptManager
from .background import BackgroundWorker
from .budget import (
    BACKGROUND_BUDGET,
    SYNC_BUDGET,
    BudgetLedger,
    CircuitBreaker,
    ExecutionBudget,
    tool_signature,
)
from .capability import (
    CapabilityError,
    CapabilityGap,
    CapabilityRouter,
    RouteDecision,
    RouteRegistry,
    StartupCapabilityError,
    StaticRouteRegistry,
)
from .catchup import CatchupStream
from .chain import ChainResolution, ChainResolver, build_upstream_input
from .handler import ResponsesV3Handler
from .hosted_tools import (
    HOSTED_TOOL_TYPES,
    HostedToolRecognizer,
    HostedToolValidator,
    build_runtime_unavailable_event,
    build_unsupported_tool_error,
    validate_tool_choice,
)
from .passthrough import (
    NativePassthrough,
    PassthroughPathError,
    PassthroughRequest,
    RecordingTransport,
)
from .schema import to_error_object, to_input_items_list, to_response_object

__all__ = [
    "ResponsesV3Handler",
    "ChainResolution",
    "ChainResolver",
    "build_upstream_input",
    "to_response_object",
    "to_input_items_list",
    "to_error_object",
    # T23: budget / circuit breaker / retry policy
    "ExecutionBudget",
    "SYNC_BUDGET",
    "BACKGROUND_BUDGET",
    "BudgetLedger",
    "tool_signature",
    "CircuitBreaker",
    "AttemptManager",
    # T24: background worker / catch-up replay / job store
    "BackgroundWorker",
    "CatchupStream",
    "BackgroundJobStore",
    # T25: capability routing / native passthrough
    "CapabilityRouter",
    "CapabilityGap",
    "CapabilityError",
    "RouteDecision",
    "RouteRegistry",
    "StaticRouteRegistry",
    "StartupCapabilityError",
    "NativePassthrough",
    "PassthroughRequest",
    "PassthroughPathError",
    "RecordingTransport",
    # T26: hosted tool recognition / Q4 error semantics / tool_choice
    "HOSTED_TOOL_TYPES",
    "HostedToolRecognizer",
    "HostedToolValidator",
    "build_unsupported_tool_error",
    "validate_tool_choice",
    "build_runtime_unavailable_event",
    # re-exported for call sites that only import from this package
    "TerminalReason",
]
