"""原生 Responses 直通（T25 / R-P1-44）。

R-P1-44 的原文约束有三条，缺一不可：

1. ``upstream_mode: responses_native`` 时请求**原样**发到 ``/v1/responses``；
2. **不得**先降级为 Chat Completions；
3. 代理只做鉴权替换、模型映射、租户策略、审计和必要 schema 兼容 —— output
   item 的结构不被改写。

第 2 条是这里最容易悄悄失守的一条：只要有人在转发链路上「顺手」复用了 chat
翻译器，客户端拿到的仍然是 200 + 一段文本，没有任何测试会自然地失败。所以
:meth:`NativePassthrough.build_request` 在构造阶段就把路径钉死为
:data:`~.capability.PATH_RESPONSES`，任何解析出别的路径的输入都直接抛
:class:`PassthroughPathError` —— 让它在第一次跑就炸，而不是在生产里慢慢腐烂。

可注入 transport
----------------
本模块**不做**真实网络 IO，也不引入任何新依赖：``forward()`` 接受一个
``transport``（``send(method, url, headers, body)`` 返回字节流的对象）。
这样直通逻辑可以被逐字节断言（判据②的「抓包」在单测里等价为一个记录型
fake transport），而真实的 aiohttp 客户端在 T28 / T37 接入时只需满足同一个
协议，不必回头改这里。

HONEST STUB：真实上游连接、超时六件套与重试策略由 T28 的 pipeline 提供；
本模块只负责「发出去的那一份请求长什么样」。
"""
from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from ..proxy.protocol.responses_models import SanitizedRequest
from .capability import PATH_CHAT_COMPLETIONS, PATH_RESPONSES

#: 直通时允许覆写的**唯一**请求体字段：模型映射（R-P1-44 明确许可）。
#: 除它之外，``SanitizedRequest.payload`` 逐字段原样送出。
MODEL_FIELD: str = "model"


class PassthroughPathError(RuntimeError):
    """直通被解析到非 ``/v1/responses`` 路径时抛出（判据③的编码化断言）。"""


@runtime_checkable
class Transport(Protocol):
    """最小上游传输接口。

    ``send`` 既可以是 async generator，也可以是返回 ``AsyncIterable`` 的
    coroutine —— :meth:`NativePassthrough.forward` 两种都吃。
    """

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class PassthroughRequest:
    """即将发往上游的一份原生请求（构造与发送分离，便于断言）。"""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes

    @property
    def path(self) -> str:
        """URL 的 path 部分；相对 URL 时即其本身。"""
        return urlsplit(self.url).path or self.url

    @property
    def payload(self) -> dict[str, Any]:
        """把 :attr:`body` 解析回 dict，供测试逐字段比对。"""
        return json.loads(self.body.decode("utf-8")) if self.body else {}


class NativePassthrough:
    """把 Responses 请求原样转发到上游 ``/v1/responses``。"""

    def __init__(self, *, model_field: str = MODEL_FIELD) -> None:
        self._model_field = model_field

    # -- 构造 ------------------------------------------------------------

    def build_request(
        self,
        req: SanitizedRequest,
        *,
        base_url: str = "",
        api_key: str = "",
        upstream_model: str = "",
        extra_headers: Mapping[str, str] | None = None,
    ) -> PassthroughRequest:
        """构造直通请求；除鉴权头与模型映射外不改动任何内容。

        ``base_url`` 可以带或不带 ``/v1`` 后缀，也可以为空（此时 URL 就是
        ``/v1/responses``，由 transport 自行补全 host）。
        """
        url = _join_url(base_url, PATH_RESPONSES)
        _assert_native_path(url)

        payload = dict(req.payload)
        if upstream_model:
            payload[self._model_field] = upstream_model

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": (
                "text/event-stream" if payload.get("stream") else "application/json"
            ),
        }
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        for name, value in (extra_headers or {}).items():
            headers[str(name)] = str(value)

        return PassthroughRequest(
            method="POST", url=url, headers=headers, body=body,
        )

    # -- 发送 ------------------------------------------------------------

    async def forward(
        self,
        req: SanitizedRequest,
        transport: Transport,
        *,
        base_url: str = "",
        api_key: str = "",
        upstream_model: str = "",
        extra_headers: Mapping[str, str] | None = None,
    ) -> AsyncIterable[bytes]:
        """转发并逐块 yield 上游返回的原始字节。

        上游字节**不经过任何翻译层**：直通模式下 output item 的结构必须与上游
        产出的完全一致（R-P1-44），任何「顺手规范化一下」都是破坏兼容性。
        """
        prepared = self.build_request(
            req,
            base_url=base_url,
            api_key=api_key,
            upstream_model=upstream_model,
            extra_headers=extra_headers,
        )
        stream = transport.send(
            prepared.method, prepared.url, prepared.headers, prepared.body,
        )
        if inspect.isawaitable(stream):
            stream = await stream
        async for chunk in stream:
            yield chunk


@dataclass
class RecordingTransport:
    """记录型 transport：测试用的「抓包器」，也可用于本地演练。

    保存每一次 :meth:`send` 的四元组，并按 :attr:`chunks` 回放响应字节。
    """

    chunks: list[bytes] = field(default_factory=list)
    calls: list[PassthroughRequest] = field(default_factory=list)

    async def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> AsyncIterable[bytes]:
        self.calls.append(
            PassthroughRequest(
                method=method, url=url, headers=dict(headers), body=body,
            )
        )
        return _aiter(self.chunks)

    @property
    def last(self) -> PassthroughRequest:
        """最后一次请求；没有请求时抛 ``IndexError``（测试想要的失败方式）。"""
        return self.calls[-1]


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


async def _aiter(chunks: list[bytes]) -> AsyncIterable[bytes]:
    for chunk in chunks:
        yield chunk


def _join_url(base_url: str, path: str) -> str:
    """拼接 ``base_url`` 与 ``path``，容忍 base 自带 ``/v1`` 或结尾斜杠。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return path
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + path


def _assert_native_path(url: str) -> None:
    """判据③：直通 URL 必须落在 ``/v1/responses``，绝不能是 chat completions。"""
    path = urlsplit(url).path or url
    if path != PATH_RESPONSES:
        raise PassthroughPathError(
            "native passthrough must target {0}, refused: {1}".format(
                PATH_RESPONSES, path,
            )
        )
    if PATH_CHAT_COMPLETIONS in url:
        raise PassthroughPathError(
            "native passthrough must never be downgraded to " + PATH_CHAT_COMPLETIONS
        )


__all__ = [
    "MODEL_FIELD",
    "PassthroughPathError",
    "Transport",
    "PassthroughRequest",
    "NativePassthrough",
    "RecordingTransport",
]
