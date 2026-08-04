"""原生 Responses 直通单测（T25 / R-P1-44）。

对齐任务表 T25 的完成判据：

* 判据② 配置 ``upstream_mode: responses_native`` 后**抓包**断言请求路径为
  ``/v1/responses`` 且 output item 未被改写
  -> ``test_passthrough_hits_responses_endpoint``
     / ``test_passthrough_does_not_rewrite_output_items``
* 判据③ 原生模式**不先降级**为 Chat Completions
  -> ``test_native_mode_never_downgrades_to_chat``
     / ``test_build_request_refuses_non_native_path``

「抓包」在这里由 :class:`RecordingTransport` 承担：它记录 method / url /
headers / body 四元组，断言的是**真正发出去的那一份字节**，而不是中间对象。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from zhongzhuan.proxy.protocol.responses_models import (
    Capability,
    ExecutionMode,
    HostedToolSpec,
    SanitizedRequest,
)
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.responses_v3.capability import (
    PATH_CHAT_COMPLETIONS,
    PATH_RESPONSES,
    CapabilityRouter,
    RouteDecision,
    StaticRouteRegistry,
)
from zhongzhuan.responses_v3.passthrough import (
    NativePassthrough,
    PassthroughPathError,
    PassthroughRequest,
    RecordingTransport,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

#: 一个带 output item 的请求体：``input`` 里回放了上一轮的 function_call 与
#: 它的 output，直通时这两块结构必须逐字节原样送出。
OUTPUT_ITEMS: list[dict] = [
    {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "查一下今天的汇率"}],
    },
    {
        "type": "function_call",
        "id": "fc_001",
        "call_id": "call_abc",
        "name": "get_rate",
        "arguments": '{"pair":"USD/CNY"}',
        "status": "completed",
    },
    {
        "type": "function_call_output",
        "call_id": "call_abc",
        "output": '{"rate":7.21}',
    },
    {
        "type": "web_search_call",
        "id": "ws_001",
        "status": "completed",
        "action": {"type": "search", "query": "USD CNY"},
    },
]


def make_native_req(*, stream: bool = False) -> SanitizedRequest:
    payload = {
        "model": "gpt-4o",
        "input": [dict(item) for item in OUTPUT_ITEMS],
        "tools": [{"type": "web_search"}],
        "stream": stream,
        "metadata": {"tenant": "acme"},
    }
    return SanitizedRequest(
        payload=payload,
        hosted_tools=[
            HostedToolSpec(
                tool_type="web_search",
                raw={"type": "web_search"},
                required_capability=Capability.WEB_SEARCH,
                param_path="tools[0].type",
            ),
        ],
        required_capabilities=frozenset({Capability.WEB_SEARCH}),
    )


def native_key() -> KeyHealth:
    return KeyHealth(
        key_id=1,
        api_key="sk-native-1",
        window=SlidingWindow(60, 0),
        capabilities={"web_search"},
        upstream_mode="responses_native",
    )


def drain(passthrough: NativePassthrough, req, transport, **kwargs) -> list[bytes]:
    """跑完 forward() 并收集全部字节块。"""

    async def _run() -> list[bytes]:
        return [chunk async for chunk in passthrough.forward(req, transport, **kwargs)]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 判据② —— 抓包断言路径 /v1/responses
# ---------------------------------------------------------------------------


def test_passthrough_hits_responses_endpoint():
    transport = RecordingTransport(chunks=[b"data: {}\n\n"])
    drain(
        NativePassthrough(),
        make_native_req(),
        transport,
        base_url="https://api.example.com",
        api_key="sk-upstream",
    )

    assert len(transport.calls) == 1
    sent = transport.last
    assert sent.method == "POST"
    assert sent.url == "https://api.example.com" + PATH_RESPONSES
    assert sent.path == PATH_RESPONSES
    assert sent.headers["Authorization"] == "Bearer sk-upstream"


def test_passthrough_base_url_with_v1_suffix_is_not_doubled():
    transport = RecordingTransport()
    drain(
        NativePassthrough(),
        make_native_req(),
        transport,
        base_url="https://api.example.com/v1/",
    )

    assert transport.last.url == "https://api.example.com/v1/responses"
    assert transport.last.path == PATH_RESPONSES


def test_passthrough_without_base_url_uses_bare_path():
    transport = RecordingTransport()
    drain(NativePassthrough(), make_native_req(), transport)

    assert transport.last.url == PATH_RESPONSES


def test_passthrough_does_not_rewrite_output_items():
    """判据②后半：body 里的 output item 结构逐字段与原始请求一致。"""
    req = make_native_req()
    transport = RecordingTransport()
    drain(NativePassthrough(), req, transport)

    sent_items = transport.last.payload["input"]
    assert len(sent_items) == len(OUTPUT_ITEMS)
    for sent, original in zip(sent_items, OUTPUT_ITEMS):
        assert sent == original  # 逐字段比对
        assert set(sent.keys()) == set(original.keys())
    # function_call 的 arguments 是字符串，不得被解析后重新序列化。
    assert sent_items[1]["arguments"] == '{"pair":"USD/CNY"}'
    # hosted tool 的 call item 也不得被降级成 function_call。
    assert sent_items[3]["type"] == "web_search_call"
    assert sent_items[3]["action"] == {"type": "search", "query": "USD CNY"}


def test_passthrough_sends_the_payload_verbatim():
    """除鉴权头外，整个 payload 一字不改（tools / metadata / stream 都在）。"""
    req = make_native_req(stream=True)
    transport = RecordingTransport()
    drain(NativePassthrough(), req, transport, api_key="sk-x")

    assert transport.last.payload == req.payload
    assert transport.last.headers["Accept"] == "text/event-stream"


def test_passthrough_non_stream_accepts_json():
    transport = RecordingTransport()
    drain(NativePassthrough(), make_native_req(stream=False), transport)

    assert transport.last.headers["Accept"] == "application/json"


def test_passthrough_applies_model_mapping_only():
    """模型映射是 R-P1-44 明确许可的唯一 body 改写。"""
    req = make_native_req()
    transport = RecordingTransport()
    drain(NativePassthrough(), req, transport, upstream_model="gpt-4o-2024-11-20")

    sent = transport.last.payload
    assert sent["model"] == "gpt-4o-2024-11-20"
    assert sent["input"] == req.payload["input"]
    assert {k: v for k, v in sent.items() if k != "model"} == {k: v for k, v in req.payload.items() if k != "model"}


def test_passthrough_does_not_mutate_the_sanitized_request():
    """模型映射写在副本上，原请求对象不被污染（背景任务会重放它）。"""
    req = make_native_req()
    original = json.loads(json.dumps(req.payload))
    drain(
        NativePassthrough(),
        req,
        RecordingTransport(),
        upstream_model="gpt-4o-mini",
    )

    assert req.payload == original


def test_passthrough_streams_upstream_bytes_unchanged():
    chunks = [b"event: response.created\n", b'data: {"a":1}\n\n', b"data: [DONE]\n\n"]
    got = drain(NativePassthrough(), make_native_req(), RecordingTransport(chunks=chunks))

    assert got == chunks


def test_passthrough_accepts_an_async_generator_transport():
    """transport.send 直接写成 async generator 也能用。"""

    class GenTransport:
        def __init__(self) -> None:
            self.calls: list[PassthroughRequest] = []

        async def send(self, method, url, headers, body):
            self.calls.append(PassthroughRequest(method=method, url=url, headers=dict(headers), body=body))
            yield b"chunk-1"
            yield b"chunk-2"

    transport = GenTransport()
    got = drain(NativePassthrough(), make_native_req(), transport)

    assert got == [b"chunk-1", b"chunk-2"]
    assert transport.calls[0].path == PATH_RESPONSES


def test_extra_headers_are_forwarded():
    transport = RecordingTransport()
    drain(
        NativePassthrough(),
        make_native_req(),
        transport,
        extra_headers={"X-Tenant": "acme", "OpenAI-Beta": "responses=v1"},
    )

    assert transport.last.headers["X-Tenant"] == "acme"
    assert transport.last.headers["OpenAI-Beta"] == "responses=v1"


# ---------------------------------------------------------------------------
# 判据③ —— 绝不降级为 Chat Completions
# ---------------------------------------------------------------------------


def test_native_mode_never_downgrades_to_chat():
    """抓到的 URL 里不得出现 chat/completions 的任何痕迹。"""
    transport = RecordingTransport()
    drain(
        NativePassthrough(),
        make_native_req(),
        transport,
        base_url="https://api.example.com",
    )

    sent = transport.last
    assert PATH_CHAT_COMPLETIONS not in sent.url
    assert "chat/completions" not in sent.url
    assert "/v1/messages" not in sent.url
    assert sent.path == PATH_RESPONSES
    # body 也没有被 chat 化：不存在 messages 字段，input 还在。
    assert "messages" not in sent.payload
    assert "input" in sent.payload


def test_build_request_refuses_non_native_path():
    """base_url 自带 chat/completions 时构造阶段就炸，不静默转发。"""
    passthrough = NativePassthrough()

    with pytest.raises(PassthroughPathError):
        passthrough.build_request(
            make_native_req(),
            base_url="https://api.example.com/v1/chat/completions",
        )


def test_forward_refuses_non_native_path():
    transport = RecordingTransport()

    with pytest.raises(PassthroughPathError):
        drain(
            NativePassthrough(),
            make_native_req(),
            transport,
            base_url="https://api.example.com/v1/chat/completions",
        )
    assert transport.calls == []  # 一个字节都没发出去


# ---------------------------------------------------------------------------
# router + passthrough 串起来（判据②③的端到端形态）
# ---------------------------------------------------------------------------


class _Cfg:
    upstream_mode = "responses_native"
    strict_capability_startup = False
    required_capabilities = ("web_search",)


def test_router_decision_drives_passthrough_to_responses_path():
    """配置 responses_native -> router 给出 NATIVE -> 抓包落在 /v1/responses。"""
    key = native_key()
    router = CapabilityRouter(StaticRouteRegistry([key]), _Cfg())
    req = make_native_req()

    decision = router.route(req, [key])
    assert isinstance(decision, RouteDecision)
    assert decision.mode is ExecutionMode.NATIVE
    assert decision.upstream_path == PATH_RESPONSES

    transport = RecordingTransport()
    drain(
        NativePassthrough(),
        req,
        transport,
        base_url="https://api.example.com",
        api_key=decision.key.api_key,
    )

    assert transport.last.path == decision.upstream_path == PATH_RESPONSES
    assert transport.last.payload["input"] == OUTPUT_ITEMS
    assert router.assert_startup_ok() == []


def test_build_request_is_pure_and_repeatable():
    """同一份请求构造两次得到完全相同的字节（审计/重放依赖这一点）。"""
    req = make_native_req()
    passthrough = NativePassthrough()
    first = passthrough.build_request(req, base_url="https://x.test", api_key="k")
    second = passthrough.build_request(req, base_url="https://x.test", api_key="k")

    assert first == second
    assert first.body == second.body
