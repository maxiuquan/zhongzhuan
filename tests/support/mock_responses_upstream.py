"""可编程 mock 上游。

一个基于 aiohttp test server 的假上游，可以精确控制「线路上到底发生了什么」：

* **分片策略**：``whole`` / ``by_line`` / ``by_n_bytes(n)`` / ``random_split(seed)``
  —— Phase 2 的 Hypothesis 属性测试直接复用这里的策略对象。
* **延迟注入**：首字节延迟、chunk 间延迟。
* **断流注入**：发完第 N 个 chunk 后直接 abort 连接（不发 ``finish_reason``、
  不发 ``[DONE]``）。
* **错误注入**：429 / 500 / 502 等状态码，以及「不发响应头直接断 TCP」的网络错误。

同时提供三种上游形态的路由：

* OpenAI Chat Completions ``POST /v1/chat/completions``
* Anthropic Messages       ``POST /v1/messages``
* OpenAI Responses（原生） ``POST /v1/responses``

以及一组**确定性**的 SSE / JSON 载荷构造器（``openai_text_stream`` 等），
供 golden fixture 生成使用——它们的输出不含任何随机值或时钟值。

本模块不依赖 ``zhongzhuan``。
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

__all__ = [
    "ChunkStrategy",
    "MockUpstream",
    "RecordedRequest",
    "UpstreamBehavior",
    "anthropic_error_json",
    "anthropic_text_json",
    "anthropic_text_stream",
    "anthropic_tool_stream",
    "by_line",
    "by_n_bytes",
    "openai_error_json",
    "openai_text_json",
    "openai_text_stream",
    "openai_tool_json",
    "openai_tool_stream",
    "random_split",
    "responses_text_stream",
    "whole",
]


# ---------------------------------------------------------------------------
# 分片策略
# ---------------------------------------------------------------------------

MODE_WHOLE = "whole"
MODE_BY_LINE = "by_line"
MODE_BY_N_BYTES = "by_n_bytes"
MODE_RANDOM_SPLIT = "random_split"


@dataclass(frozen=True)
class ChunkStrategy:
    """把一段完整载荷切成若干「线路上的 chunk」。

    Attributes:
        mode: ``whole`` / ``by_line`` / ``by_n_bytes`` / ``random_split``。
        n: ``by_n_bytes`` 模式下每片的字节数，必须 > 0。
        seed: ``random_split`` 模式下的随机种子，保证可复现。
        max_pieces: ``random_split`` 模式下的最大片数上限，防止病态切分。
    """

    mode: str = MODE_WHOLE
    n: int = 16
    seed: int = 0
    max_pieces: int = 512

    def __post_init__(self) -> None:
        if self.mode not in (MODE_WHOLE, MODE_BY_LINE, MODE_BY_N_BYTES, MODE_RANDOM_SPLIT):
            raise ValueError(f"未知分片模式: {self.mode!r}")
        if self.n <= 0:
            raise ValueError(f"n 必须为正整数，收到 {self.n}")

    def split(self, payload: bytes) -> list[bytes]:
        """按策略切分 ``payload``；返回的片段拼接后必须与原载荷逐字节相等。

        Args:
            payload: 待切分的完整字节。

        Returns:
            切片列表。空载荷返回空列表。
        """
        if not payload:
            return []
        if self.mode == MODE_WHOLE:
            return [payload]
        if self.mode == MODE_BY_LINE:
            # keepends=True 保证拼接回去逐字节相等（含 \r\n 风格）
            return [line for line in payload.splitlines(keepends=True) if line]
        if self.mode == MODE_BY_N_BYTES:
            return [payload[i:i + self.n] for i in range(0, len(payload), self.n)]
        # random_split：用固定 seed 产生可复现的随机切点
        rng = random.Random(self.seed)
        total = len(payload)
        max_cuts = max(0, min(self.max_pieces - 1, total - 1))
        if max_cuts <= 0:
            return [payload]
        cut_count = rng.randint(1, max_cuts)
        cuts = sorted(rng.sample(range(1, total), cut_count))
        pieces: list[bytes] = []
        prev = 0
        for cut in cuts:
            pieces.append(payload[prev:cut])
            prev = cut
        pieces.append(payload[prev:])
        return [p for p in pieces if p]


def whole() -> ChunkStrategy:
    """整体一次性发送。"""
    return ChunkStrategy(mode=MODE_WHOLE)


def by_line() -> ChunkStrategy:
    """按行切分（每个 ``\\n`` 结尾算一片，SSE 事件会被拆成多个 chunk）。"""
    return ChunkStrategy(mode=MODE_BY_LINE)


def by_n_bytes(n: int) -> ChunkStrategy:
    """按固定字节数切分。

    Args:
        n: 每片字节数，必须 > 0。
    """
    return ChunkStrategy(mode=MODE_BY_N_BYTES, n=n)


def random_split(seed: int, *, max_pieces: int = 512) -> ChunkStrategy:
    """按固定随机种子做可复现的随机切分。

    Args:
        seed: 随机种子。
        max_pieces: 最大片数上限。
    """
    return ChunkStrategy(mode=MODE_RANDOM_SPLIT, seed=seed, max_pieces=max_pieces)


# ---------------------------------------------------------------------------
# 行为描述
# ---------------------------------------------------------------------------


@dataclass
class UpstreamBehavior:
    """描述 mock 上游对**一次**请求的完整应答行为。

    Attributes:
        status: HTTP 状态码。>=400 时走错误分支，直接返回 ``error_body``。
        stream_payload: 流式场景要发的完整 SSE 字节（请求体 ``stream=true`` 时使用）。
        json_payload: 非流式场景要返回的 JSON 字节。
        error_body: 错误场景的响应体；为 ``None`` 时用一个通用错误信封。
        content_type: 非流式响应的 Content-Type。
        stream_content_type: 流式响应的 Content-Type。
        chunk_strategy: 流式载荷的分片策略。
        first_byte_delay: 首字节前的延迟（秒）。
        inter_chunk_delay: 相邻 chunk 之间的延迟（秒）。
        truncate_after_chunks: 发完第 N 个 chunk 后直接 abort 连接；``None`` 表示不断流。
        network_error: 为 ``True`` 时连响应头都不发，直接 abort TCP。
        extra_headers: 追加到响应上的头（如 ``retry-after``、``x-ratelimit-*``）。
        force_stream: 强制走流式分支，忽略请求体里的 ``stream`` 字段。
    """

    status: int = 200
    stream_payload: bytes = b""
    json_payload: bytes = b""
    error_body: bytes | None = None
    content_type: str = "application/json"
    stream_content_type: str = "text/event-stream"
    chunk_strategy: ChunkStrategy = field(default_factory=whole)
    first_byte_delay: float = 0.0
    inter_chunk_delay: float = 0.0
    truncate_after_chunks: int | None = None
    network_error: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)
    force_stream: bool | None = None


@dataclass
class RecordedRequest:
    """mock 上游收到的一次请求的完整记录。"""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        """把请求体解析为 JSON；失败返回 ``None``。"""
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------


class MockUpstream:
    """可编程 mock 上游服务器。

    典型用法::

        up = MockUpstream()
        up.set_behavior(UpstreamBehavior(stream_payload=openai_text_stream()))
        await up.start()
        try:
            ...  # 让被测代码请求 up.url
        finally:
            await up.stop()

    也支持排队多个行为（用于重试 / 换 key 场景）::

        up.queue_behaviors([
            UpstreamBehavior(status=429),
            UpstreamBehavior(stream_payload=openai_text_stream()),
        ])
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """初始化（不启动）。

        Args:
            host: 监听地址。
            port: 监听端口，``0`` 表示由系统分配。
        """
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._url: str = ""
        self._default_behavior = UpstreamBehavior()
        self._queue: list[UpstreamBehavior] = []
        self.requests: list[RecordedRequest] = []

    # ---- 生命周期 ----

    @property
    def url(self) -> str:
        """已启动服务器的 base URL；未启动时为空串。"""
        return self._url

    @property
    def request_count(self) -> int:
        """已收到的请求总数。"""
        return len(self.requests)

    def app(self) -> web.Application:
        """构造 aiohttp 应用（三种上游形态 + 通配兜底）。"""
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_post("/v1/chat/completions", self._handle)
        app.router.add_post("/v1/messages", self._handle)
        app.router.add_post("/v1/responses", self._handle)
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        return app

    async def start(self) -> str:
        """启动服务器并返回 base URL。"""
        if self._runner is not None:
            return self._url
        self._runner = web.AppRunner(self.app())
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        sockets = self._site._server.sockets  # type: ignore[union-attr]
        actual_port = sockets[0].getsockname()[1]
        self._url = f"http://{self._host}:{actual_port}"
        return self._url

    async def stop(self) -> None:
        """关闭服务器，释放端口。"""
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._url = ""

    async def __aenter__(self) -> MockUpstream:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    # ---- 行为编排 ----

    def set_behavior(self, behavior: UpstreamBehavior) -> None:
        """设置默认行为（队列耗尽后一直用它）。"""
        self._default_behavior = behavior

    def queue_behaviors(self, behaviors: list[UpstreamBehavior]) -> None:
        """按顺序排队若干次应答行为；队列耗尽后回落到默认行为。"""
        self._queue = list(behaviors)

    def reset(self) -> None:
        """清空请求记录与行为队列，恢复默认行为。"""
        self.requests.clear()
        self._queue.clear()
        self._default_behavior = UpstreamBehavior()

    def _next_behavior(self) -> UpstreamBehavior:
        """取出下一个待用行为。"""
        if self._queue:
            return self._queue.pop(0)
        return self._default_behavior

    # ---- 请求处理 ----

    async def _handle_models(self, request: web.Request) -> web.StreamResponse:
        """``GET /v1/models``：固定返回空列表，避免影响其他断言。"""
        self.requests.append(RecordedRequest(
            method=request.method, path=request.path,
            headers=dict(request.headers), body=b"",
        ))
        return web.json_response({"object": "list", "data": []})

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """统一入口：记录请求 → 取行为 → 按行为应答。"""
        body = await request.read()
        self.requests.append(RecordedRequest(
            method=request.method, path=request.path,
            headers=dict(request.headers), body=body,
        ))
        behavior = self._next_behavior()

        # 1. 网络错误：连响应头都不发，直接断 TCP
        if behavior.network_error:
            return self._abort(request)

        # 2. 首字节延迟
        if behavior.first_byte_delay > 0:
            await asyncio.sleep(behavior.first_byte_delay)

        # 3. 错误状态码：返回 JSON 错误信封（非 SSE）
        if behavior.status >= 400:
            payload = behavior.error_body
            if payload is None:
                payload = json.dumps({
                    "error": {
                        "message": f"mock upstream error {behavior.status}",
                        "type": "mock_error",
                        "code": behavior.status,
                    }
                }, ensure_ascii=False).encode()
            headers = {"Content-Type": behavior.content_type}
            headers.update(behavior.extra_headers)
            return web.Response(status=behavior.status, body=payload, headers=headers)

        # 4. 判断走流式还是非流式
        want_stream = behavior.force_stream
        if want_stream is None:
            want_stream = self._body_wants_stream(body) and bool(behavior.stream_payload)

        if not want_stream:
            headers = {"Content-Type": behavior.content_type}
            headers.update(behavior.extra_headers)
            payload = behavior.json_payload or b"{}"
            return web.Response(status=behavior.status, body=payload, headers=headers)

        return await self._stream(request, behavior)

    @staticmethod
    def _body_wants_stream(body: bytes) -> bool:
        """从请求体判断下游是否要求流式。"""
        if not body:
            return False
        try:
            obj = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return False
        return bool(isinstance(obj, dict) and obj.get("stream"))

    @staticmethod
    def _abort(request: web.Request) -> web.StreamResponse:
        """强制断开底层 TCP 连接（模拟网络层故障 / 断流）。"""
        transport = request.transport
        if transport is not None:
            transport.abort()
        # 返回一个占位响应；连接已断，aiohttp 写入时会静默失败。
        return web.Response(status=500, body=b"")

    async def _stream(
        self, request: web.Request, behavior: UpstreamBehavior
    ) -> web.StreamResponse:
        """按分片策略 / 延迟 / 断流设置发送 SSE 流。"""
        headers = {
            "Content-Type": behavior.stream_content_type,
            "Cache-Control": "no-cache",
        }
        headers.update(behavior.extra_headers)
        resp = web.StreamResponse(status=behavior.status, headers=headers)
        await resp.prepare(request)

        chunks = behavior.chunk_strategy.split(behavior.stream_payload)
        limit = behavior.truncate_after_chunks

        for i, chunk in enumerate(chunks):
            if limit is not None and i >= limit:
                # 断流注入：不发 finish_reason、不发 [DONE]，直接砍掉连接
                transport = request.transport
                if transport is not None:
                    transport.abort()
                return resp
            if i > 0 and behavior.inter_chunk_delay > 0:
                await asyncio.sleep(behavior.inter_chunk_delay)
            try:
                await resp.write(chunk)
            except (ConnectionResetError, ConnectionError, OSError):
                return resp

        try:
            await resp.write_eof()
        except (ConnectionResetError, ConnectionError, OSError):
            pass
        return resp


# ---------------------------------------------------------------------------
# 确定性载荷构造器
#
# 这些函数刻意不使用 uuid / time，输出**完全确定**，是 golden fixture 能
# 逐字节复现的前提。
# ---------------------------------------------------------------------------


def _sse(payload: dict[str, Any]) -> bytes:
    """序列化一条 OpenAI 风格的 ``data:`` SSE 帧。"""
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def _sse_named(event: str, payload: dict[str, Any]) -> bytes:
    """序列化一条带 ``event:`` 行的 SSE 帧（Anthropic / Responses 风格）。"""
    body = json.dumps(payload, ensure_ascii=False).encode()
    return b"event: " + event.encode() + b"\ndata: " + body + b"\n\n"


# ---- OpenAI Chat Completions ----

_OAI_CHUNK_ID = "chatcmpl-fixture0001"
_OAI_CREATED = 1700000000


def openai_text_stream(
    *,
    model: str = "upstream-model",
    pieces: tuple[str, ...] = ("Hello", ", ", "world", "!"),
    include_usage: bool = True,
) -> bytes:
    """OpenAI Chat Completions 流式**纯文本**载荷（确定性）。"""
    out = bytearray()
    out += _sse({
        "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
        "created": _OAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                     "finish_reason": None}],
    })
    for piece in pieces:
        out += _sse({
            "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
            "created": _OAI_CREATED, "model": model,
            "choices": [{"index": 0, "delta": {"content": piece},
                         "finish_reason": None}],
        })
    out += _sse({
        "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
        "created": _OAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    if include_usage:
        out += _sse({
            "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
            "created": _OAI_CREATED, "model": model, "choices": [],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
        })
    out += b"data: [DONE]\n\n"
    return bytes(out)


def openai_tool_stream(
    *,
    model: str = "upstream-model",
    tool_name: str = "get_weather",
    tool_call_id: str = "call_fixture_0001",
    arg_pieces: tuple[str, ...] = ('{"cit', 'y": "Bei', 'jing"}'),
) -> bytes:
    """OpenAI Chat Completions 流式**工具调用**载荷（确定性）。"""
    out = bytearray()
    out += _sse({
        "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
        "created": _OAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": None},
                     "finish_reason": None}],
    })
    out += _sse({
        "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
        "created": _OAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": tool_call_id, "type": "function",
            "function": {"name": tool_name, "arguments": ""},
        }]}, "finish_reason": None}],
    })
    for piece in arg_pieces:
        out += _sse({
            "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
            "created": _OAI_CREATED, "model": model,
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": piece},
            }]}, "finish_reason": None}],
        })
    out += _sse({
        "id": _OAI_CHUNK_ID, "object": "chat.completion.chunk",
        "created": _OAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
    })
    out += b"data: [DONE]\n\n"
    return bytes(out)


def openai_text_json(
    *, model: str = "upstream-model", content: str = "Hello, world!"
) -> bytes:
    """OpenAI Chat Completions **非流式**纯文本响应（确定性）。"""
    return json.dumps({
        "id": _OAI_CHUNK_ID, "object": "chat.completion",
        "created": _OAI_CREATED, "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
    }, ensure_ascii=False).encode()


def openai_tool_json(
    *,
    model: str = "upstream-model",
    tool_name: str = "get_weather",
    tool_call_id: str = "call_fixture_0001",
    arguments: str = '{"city": "Beijing"}',
) -> bytes:
    """OpenAI Chat Completions **非流式**工具调用响应（确定性）。"""
    return json.dumps({
        "id": _OAI_CHUNK_ID, "object": "chat.completion",
        "created": _OAI_CREATED, "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": tool_call_id, "type": "function",
                    "function": {"name": tool_name, "arguments": arguments},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
    }, ensure_ascii=False).encode()


def openai_error_json(
    *, message: str = "upstream rejected the request",
    err_type: str = "invalid_request_error", code: str = "bad_request",
) -> bytes:
    """OpenAI 风格错误信封（确定性）。"""
    return json.dumps({
        "error": {"message": message, "type": err_type, "param": None, "code": code}
    }, ensure_ascii=False).encode()


# ---- Anthropic Messages ----

_ANT_MSG_ID = "msg_fixture0001"


def anthropic_text_stream(
    *,
    model: str = "upstream-claude",
    pieces: tuple[str, ...] = ("Hello", ", ", "world", "!"),
) -> bytes:
    """Anthropic Messages 流式**纯文本**载荷（确定性）。"""
    out = bytearray()
    out += _sse_named("message_start", {
        "type": "message_start",
        "message": {
            "id": _ANT_MSG_ID, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 11, "output_tokens": 0},
        },
    })
    out += _sse_named("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    for piece in pieces:
        out += _sse_named("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": piece},
        })
    out += _sse_named("content_block_stop", {"type": "content_block_stop", "index": 0})
    out += _sse_named("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 4},
    })
    out += _sse_named("message_stop", {"type": "message_stop"})
    return bytes(out)


def anthropic_tool_stream(
    *,
    model: str = "upstream-claude",
    tool_name: str = "get_weather",
    tool_use_id: str = "toolu_fixture0001",
    arg_pieces: tuple[str, ...] = ('{"cit', 'y": "Bei', 'jing"}'),
) -> bytes:
    """Anthropic Messages 流式**工具调用**载荷（确定性）。"""
    out = bytearray()
    out += _sse_named("message_start", {
        "type": "message_start",
        "message": {
            "id": _ANT_MSG_ID, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 21, "output_tokens": 0},
        },
    })
    out += _sse_named("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": tool_use_id,
                          "name": tool_name, "input": {}},
    })
    for piece in arg_pieces:
        out += _sse_named("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": piece},
        })
    out += _sse_named("content_block_stop", {"type": "content_block_stop", "index": 0})
    out += _sse_named("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 9},
    })
    out += _sse_named("message_stop", {"type": "message_stop"})
    return bytes(out)


def anthropic_text_json(
    *, model: str = "upstream-claude", content: str = "Hello, world!"
) -> bytes:
    """Anthropic Messages **非流式**纯文本响应（确定性）。"""
    return json.dumps({
        "id": _ANT_MSG_ID, "type": "message", "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 4},
    }, ensure_ascii=False).encode()


def anthropic_error_json(
    *, message: str = "upstream rejected the request",
    err_type: str = "invalid_request_error",
) -> bytes:
    """Anthropic 风格错误信封（确定性）。"""
    return json.dumps({
        "type": "error", "error": {"type": err_type, "message": message}
    }, ensure_ascii=False).encode()


# ---- OpenAI Responses（原生上游） ----

_RESP_ID = "resp_fixture000000000001"


def responses_text_stream(
    *,
    model: str = "upstream-model",
    pieces: tuple[str, ...] = ("Hello", ", ", "world", "!"),
) -> bytes:
    """OpenAI Responses **原生**流式载荷（确定性）。

    覆盖官方最小事件集：``response.created`` → ``response.in_progress`` →
    output item / content part 生命周期 → ``response.output_text.delta`` ×N →
    ``response.completed`` → ``[DONE]``。
    """
    full_text = "".join(pieces)
    item_id = "msg_fixture_item_0"
    base_response: dict[str, Any] = {
        "id": _RESP_ID, "object": "response", "created_at": _OAI_CREATED,
        "model": model, "status": "in_progress", "output": [],
    }
    out = bytearray()
    seq = 0

    def _emit(event: str, payload: dict[str, Any]) -> bytes:
        nonlocal seq
        body = {"type": event, "sequence_number": seq, **payload}
        seq += 1
        return _sse_named(event, body)

    out += _emit("response.created", {"response": dict(base_response)})
    out += _emit("response.in_progress", {"response": dict(base_response)})
    out += _emit("response.output_item.added", {
        "output_index": 0,
        "item": {"id": item_id, "type": "message", "status": "in_progress",
                 "role": "assistant", "content": []},
    })
    out += _emit("response.content_part.added", {
        "item_id": item_id, "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })
    for piece in pieces:
        out += _emit("response.output_text.delta", {
            "item_id": item_id, "output_index": 0, "content_index": 0,
            "delta": piece,
        })
    out += _emit("response.output_text.done", {
        "item_id": item_id, "output_index": 0, "content_index": 0,
        "text": full_text,
    })
    out += _emit("response.content_part.done", {
        "item_id": item_id, "output_index": 0, "content_index": 0,
        "part": {"type": "output_text", "text": full_text, "annotations": []},
    })
    out += _emit("response.output_item.done", {
        "output_index": 0,
        "item": {"id": item_id, "type": "message", "status": "completed",
                 "role": "assistant",
                 "content": [{"type": "output_text", "text": full_text,
                              "annotations": []}]},
    })
    completed = dict(base_response)
    completed["status"] = "completed"
    completed["output"] = [{
        "id": item_id, "type": "message", "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": full_text, "annotations": []}],
    }]
    completed["usage"] = {"input_tokens": 11, "output_tokens": 4, "total_tokens": 15}
    out += _emit("response.completed", {"response": completed})
    out += b"data: [DONE]\n\n"
    return bytes(out)
