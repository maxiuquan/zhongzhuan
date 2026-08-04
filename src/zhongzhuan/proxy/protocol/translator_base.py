"""流翻译器统一收尾接口 (T18)。

定义 ``StreamTranslator`` 协议与唯一的收尾入口 :func:`finish_translator`。

背景：各流翻译器（``ResponsesStreamTranslator`` / ``CompositeStreamTranslator`` /
``StreamA2O`` / ``StreamO2A`` / ``ResponsesTurnBridge``）历史上收尾方式不一，
有的暴露 ``finish_safely()``（sync），有的暴露 ``afinish()``（async）。为了让
handler 收尾逻辑单一、无分支，这里统一为 :func:`finish_translator`：

* 优先调用 ``afinish()``（async），否则退回 ``finish_safely()``（sync）；
* 任何异常都被吞掉并记日志（日志含 ``terminal_reason=upstream_truncated``），
  最低限度返回 ``[b"data: [DONE]\\n\\n"]``，保证下游不会挂起。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loguru import logger


@runtime_checkable
class StreamTranslator(Protocol):
    """流翻译器统一接口（用于类型标注，运行时可选）。"""

    async def feed(self, chunk: bytes) -> list[bytes]:
        """喂入一个原始字节块，返回需要发送给下游的字节列表。"""
        ...

    @property
    def done(self) -> bool:
        """流是否已结束（已发出终态事件）。"""
        ...

    @property
    def usage(self) -> dict:
        """本次通话的 token 用量。"""
        ...


async def finish_translator(tr: object) -> list[bytes]:
    """所有流翻译器收尾的唯一入口。

    优先调用 ``afinish()``（async），否则退回 ``finish_safely()``（sync）。
    任何异常吞掉并记日志（日志含 ``terminal_reason=upstream_truncated``），
    最低限度返回 ``[b"data: [DONE]\\n\\n"]``，保证下游不挂起。

    Args:
        tr: 任意实现了 ``afinish()`` 或 ``finish_safely()`` 的翻译器对象。

    Returns:
        需要发送给下游的字节列表；异常时最低返回 ``[b"data: [DONE]\\n\\n"]``。
    """
    # 优先 async 收尾：afinish() 存在即 await 它。
    afinish = getattr(tr, "afinish", None)
    if afinish is not None:
        try:
            return await afinish()
        except Exception as exc:  # noqa: BLE001 - 收尾兜底，任何异常都不外抛
            logger.warning(
                "finish_translator: afinish() failed, falling back to [DONE] terminal_reason=upstream_truncated err={}",
                exc,
            )
            return [b"data: [DONE]\n\n"]

    # 退回 sync 收尾：finish_safely()。
    # 注意：个别翻译器（如 CompositeStreamTranslator）的 finish_safely 本身是
    # async，返回协程对象。统一在此 await 可等待结果，保证 sync 与 async 两种
    # 签名都正确收尾。
    finish_safely = getattr(tr, "finish_safely", None)
    if finish_safely is not None:
        try:
            result = finish_safely()
            if hasattr(result, "__await__"):
                return await result
            return result
        except Exception as exc:  # noqa: BLE001 - 收尾兜底，任何异常都不外抛
            logger.warning(
                "finish_translator: finish_safely() failed, falling back to [DONE] "
                "terminal_reason=upstream_truncated err={}",
                exc,
            )
            return [b"data: [DONE]\n\n"]

    # 既无 afinish 也无 finish_safely：无法安全收尾，直接返回 [DONE]。
    logger.warning(
        "finish_translator: translator has no afinish()/finish_safely(), "
        "falling back to [DONE] terminal_reason=upstream_truncated "
        "type={}",
        type(tr).__name__,
    )
    return [b"data: [DONE]\n\n"]


__all__ = [
    "StreamTranslator",
    "finish_translator",
]
