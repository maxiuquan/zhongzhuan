"""T33 判据③ — OpenTelemetry tracing 5 类属性（R-P2-09）。

R-P2-09：trace span 含 5 类属性：TTFT / 每事件延迟 / 重试原因 / 能力路由决策 /
熔断原因。OTel SDK 未安装时 :class:`Tracer` 降级为内存 recorder，测试不依赖
真实 exporter。
"""
from __future__ import annotations

import time

from zhongzhuan.observability.tracing import (
    ATTR_BREAKER_REASON,
    ATTR_CAPABILITY_ROUTING,
    ATTR_EVENT,
    ATTR_EVENT_DELAY,
    ATTR_RETRY_REASON,
    ATTR_TTFT,
    Span,
    Tracer,
    record_breaker_reason,
    record_capability_routing,
    record_event_latency,
    record_retry_reason,
    record_ttft,
)


def test_tracer_works_without_otel_installed():
    """未装 OTel 时 Tracer 仍可用（内存 recorder），不抛异常。"""
    tracer = Tracer(service_name="test")
    span = tracer.start_span("stream")
    assert span.name == "stream"
    tracer.end_span(span, status="ok")
    assert len(tracer.spans) == 1
    assert tracer.spans[0].status == "ok"
    tracer.reset()
    assert tracer.spans == []


def test_span_has_all_five_attribute_classes():
    """一个 span 注入 5 类属性后，attributes 含全部 5 类键（R-P2-09）。"""
    tracer = Tracer()
    span = tracer.start_span("stream.round_1")
    record_ttft(span, 1234.5)                     # 1. TTFT
    record_event_latency(span, "output_text.delta", 42.1)  # 2. 每事件延迟
    record_retry_reason(span, "429")              # 3. 重试原因
    record_capability_routing(span, "translate", reason="deepseek_reasoning")  # 4. 能力路由
    record_breaker_reason(span, "too_many_429")   # 5. 熔断原因
    tracer.end_span(span)

    attrs = span.attributes
    five = {
        ATTR_TTFT,
        ATTR_EVENT,
        ATTR_EVENT_DELAY,
        ATTR_RETRY_REASON,
        ATTR_CAPABILITY_ROUTING,
        ATTR_BREAKER_REASON,
    }
    assert five <= set(attrs), f"missing {five - set(attrs)} in {attrs}"


def test_each_attribute_value_semantics():
    """5 类属性的值语义逐个断言。"""
    span = Span(name="s")
    record_ttft(span, 100.0)
    assert span.attributes[ATTR_TTFT] == 100.0
    record_event_latency(span, "response.output_text.delta", 1.5)
    assert span.attributes[ATTR_EVENT] == "response.output_text.delta"
    assert span.attributes[ATTR_EVENT_DELAY] == 1.5
    record_retry_reason(span, "upstream_5xx")
    assert span.attributes[ATTR_RETRY_REASON] == "upstream_5xx"
    record_capability_routing(span, "native")
    assert span.attributes[ATTR_CAPABILITY_ROUTING] == "native"
    record_capability_routing(span, "translate", reason="no_executor")
    assert span.attributes[ATTR_CAPABILITY_ROUTING] == "translate:no_executor"
    record_breaker_reason(span, "consecutive_failures")
    assert span.attributes[ATTR_BREAKER_REASON] == "consecutive_failures"


def test_spans_recorded_in_memory_for_testing():
    """内存 recorder：多个 span 顺序记录，find 按名字过滤。"""
    tracer = Tracer()
    for i in range(3):
        s = tracer.start_span(f"round_{i}")
        record_ttft(s, i * 10)
        tracer.end_span(s)
    assert len(tracer.spans) == 3
    assert len(tracer.find("round_1")) == 1
    assert tracer.find("round_1")[0].attributes[ATTR_TTFT] == 10.0


def test_span_duration_measured_in_ns_clock():
    """span 时长用单调 ns 时钟测量，end 后 duration_ms > 0。"""
    tracer = Tracer()
    span = tracer.start_span("timed")
    time.sleep(0.001)  # 1ms 真实等待——仅此一个；其余测试零等待。
    tracer.end_span(span)
    assert span.end_ns >= span.start_ns
    assert span.duration_ms >= 0.5
