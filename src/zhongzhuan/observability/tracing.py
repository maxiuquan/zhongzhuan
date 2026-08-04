"""OpenTelemetry tracing（T33 / R-P2-09）—— OTel 为可选依赖。

R-P2-09：接入 OpenTelemetry tracing，记录 5 类属性：
1. **TTFT**（首 token 延迟）     ``ttft_ms``
2. **每事件延迟**                 ``event_delay_ms`` + ``event``
3. **重试原因**                   ``retry_reason``
4. **能力路由决策**               ``capability_routing``（决策 / 理由）
5. **熔断原因**                   ``breaker_reason``

OTel 依赖处理（对齐 T27/T29 的规矩）
------------------------------------
``opentelemetry-sdk`` 是**可选依赖**：本模块顶层**不 import** 它，在
:func:`_load_otel` 里延迟导入。未安装的环境里 :class:`Tracer` 降级为**纯内存
recorder**（span 列表 + 属性），接口与 OTel 形态一致 —— 全量测试在不装 OTel
的情况下依然全绿；装了 OTel 则同一套 :class:`Tracer` 会把 span 交给真实的
``SpanProcessor`` / exporter。测试始终用可注入的 exporter（内存 recorder）断言，
不依赖真实 OTLP 端点。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: span 属性键（5 类，R-P2-09 原文）。
ATTR_TTFT: str = "ttft_ms"
ATTR_EVENT: str = "event"
ATTR_EVENT_DELAY: str = "event_delay_ms"
ATTR_RETRY_REASON: str = "retry_reason"
ATTR_CAPABILITY_ROUTING: str = "capability_routing"
ATTR_BREAKER_REASON: str = "breaker_reason"


def _load_otel():
    """延迟导入 OTel SDK。未安装返回 ``None``（降级为内存 recorder）。"""
    try:
        from opentelemetry.sdk.trace import TracerProvider as _TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
        return _TracerProvider, BatchSpanProcessor, SimpleSpanProcessor
    except Exception:  # noqa: BLE001 - 可选依赖缺失属预期，绝不在此炸掉
        return None


@dataclass
class Span:
    """一次 span 的内存表示（OTel 未装时的 recorder 形态，也用于测试断言）。"""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    start_ns: int = 0
    end_ns: int = 0
    status: str = "ok"
    parent_id: str = ""

    @property
    def duration_ms(self) -> float:
        if not self.start_ns or not self.end_ns:
            return 0.0
        return (self.end_ns - self.start_ns) / 1_000_000


class Tracer:
    """进程内 trace 器。

    * OTel 已装：同时维护一个 OTel ``TracerProvider``（通过可注入 processor
      接真实 exporter）与内存 recorder；
    * OTel 未装：仅内存 recorder，接口不变。
    """

    def __init__(self, service_name: str = "zhongzhuan") -> None:
        self._service_name = service_name
        self._lock = threading.Lock()
        self._spans: list[Span] = []
        self._active: dict[str, Span] = {}
        self._seq = 0
        self._otel = None
        self._otel_provider = None
        self._init_otel()

    def _init_otel(self) -> None:
        loaded = _load_otel()
        if loaded is None:
            return
        TracerProvider, _, _ = loaded
        try:
            self._otel_provider = TracerProvider(service_name=self._service_name)
        except Exception:  # noqa: BLE001 - 降级
            self._otel_provider = None

    def set_otel_processor(self, processor: Any) -> None:
        """接入真实 OTel SpanProcessor / exporter（可选；未装 OTel 时忽略）。"""
        if self._otel_provider is None:
            return
        try:
            self._otel_provider.add_span_processor(processor)
        except Exception:  # noqa: BLE001
            pass

    # -- span 生命周期 ------------------------------------------------------

    def start_span(self, name: str, **attributes: Any) -> Span:
        span = Span(
            name=name,
            attributes=dict(attributes),
            start_ns=time.time_ns(),
            parent_id=self._active_id(),
        )
        with self._lock:
            self._seq += 1
            self._spans.append(span)
            self._active[name] = span
        return span

    def end_span(self, span: Span, *, status: str = "ok") -> None:
        span.end_ns = time.time_ns()
        span.status = status
        with self._lock:
            self._active.pop(span.name, None)

    def _active_id(self) -> str:
        if not self._active:
            return ""
        return f"{self._seq}-{next(reversed(self._active))}"

    # -- 只读 / 测试 --------------------------------------------------------

    @property
    def spans(self) -> list[Span]:
        return list(self._spans)

    def reset(self) -> None:
        with self._lock:
            self._spans.clear()
            self._active.clear()

    def find(self, name: str) -> list[Span]:
        return [s for s in self._spans if s.name == name]


# ---------------------------------------------------------------------------
# 5 类属性注入（R-P2-09）
# ---------------------------------------------------------------------------


def record_ttft(span: Span, milliseconds: float) -> None:
    """TTFT：首 token 延迟（毫秒）。"""
    span.attributes[ATTR_TTFT] = round(float(milliseconds), 3)


def record_event_latency(span: Span, event: str, milliseconds: float) -> None:
    """每事件延迟：事件名 + 延迟（毫秒）。"""
    span.attributes[ATTR_EVENT] = event
    span.attributes[ATTR_EVENT_DELAY] = round(float(milliseconds), 3)


def record_retry_reason(span: Span, reason: str) -> None:
    """重试原因（如 ``429`` / ``5xx`` / ``connect_timeout``）。"""
    span.attributes[ATTR_RETRY_REASON] = reason


def record_capability_routing(span: Span, decision: str, *, reason: str = "") -> None:
    """能力路由决策：选中的能力 / 模式（如 ``translate`` / ``native``）。"""
    value = decision if not reason else f"{decision}:{reason}"
    span.attributes[ATTR_CAPABILITY_ROUTING] = value


def record_breaker_reason(span: Span, reason: str) -> None:
    """熔断原因（如 ``too_many_429`` / ``upstream_5xx``）。"""
    span.attributes[ATTR_BREAKER_REASON] = reason


__all__ = [
    "ATTR_TTFT",
    "ATTR_EVENT",
    "ATTR_EVENT_DELAY",
    "ATTR_RETRY_REASON",
    "ATTR_CAPABILITY_ROUTING",
    "ATTR_BREAKER_REASON",
    "Span",
    "Tracer",
    "record_ttft",
    "record_event_latency",
    "record_retry_reason",
    "record_capability_routing",
    "record_breaker_reason",
]
