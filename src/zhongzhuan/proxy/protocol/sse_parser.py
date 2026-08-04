"""字节级 SSE (Server-Sent Events) 增量分帧器。

为什么需要它
------------
旧实现对每个 HTTP chunk 直接 ``decode()`` 再按行切，在真实网络下必然出错：

1. UTF-8 多字节字符（中文、emoji）跨 chunk 边界时，``decode()`` 要么抛异常，
   要么产生替换字符 ``U+FFFD``，内容静默损坏；
2. 一个 SSE 事件被拆到两个 chunk 时，前半截被当成完整帧解析，JSON 解析失败被
   静默吞掉 —— **工具调用参数就此丢失**；
3. ``\\r\\n\\r\\n`` 分隔符、单事件多行 ``data:``、注释行 ``:`` 全都没有处理。

设计要点
--------
1. **零 decode 假设**：缓冲区始终是 ``bytearray``，只有切出一条**完整行**之后才做
   UTF-8 decode。SSE 的行终止符（CR/LF）与事件边界（空行）全部是 ASCII，而 UTF-8
   是自同步编码 —— 续字节恒在 ``0x80..0xBF``，永远不会与 ``0x0A``/``0x0D`` 混淆。
   因此按字节找行边界绝不会切开一个多字节字符，跨 chunk 的中文/emoji 天然安全。
2. **同步纯函数**（架构文档 §0 偏差裁定 B10）：``feed()`` 是零 await 的同步方法，
   从根上消除「同步方法误调 async feed」这类缺陷（R-P1-65）。``afeed()`` 仅是满足
   文档口径的薄包装。
3. **分片无关**（R-P1-68）：对同一条完整字节流，无论如何切分成 chunk 序列，
   ``feed()`` 依次喂入再 ``flush()`` 得到的 frame 序列完全相等。
4. **不抛异常**（R-P1-12）：畸形输入只累加 ``malformed_count``，流必须能正确终止。

规范依据：WHATWG HTML Living Standard - Server-sent events - "event stream
interpretation"（原 W3C EventSource）。与规范的**有意偏差**见 ``SSEParser`` 文档串。

本模块**只依赖标准库**，完全自包含，不 import 项目内任何其他模块。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping

__all__ = [
    "DEFAULT_MAX_EVENT_BYTES",
    "MALFORMED_BAD_JSON",
    "MALFORMED_BAD_RETRY",
    "MALFORMED_INVALID_UTF8",
    "MALFORMED_NUL_IN_ID",
    "MALFORMED_OVERSIZE",
    "SSEEvent",
    "SSEParser",
    "SseFrame",
]

# --- 字节常量（全部 ASCII，UTF-8 自同步保证它们不会出现在多字节序列内部）---------
_LF: Final[int] = 0x0A  # b"\n"
_CR: Final[int] = 0x0D  # b"\r"
_COLON: Final[int] = 0x3A  # b":"
_UTF8_BOM: Final[bytes] = b"\xef\xbb\xbf"

#: 单个事件的字节上限，超过即判定为畸形并丢弃该块（防御恶意/失控上游）。
DEFAULT_MAX_EVENT_BYTES: Final[int] = 8 * 1024 * 1024

#: 默认豁免 JSON 校验的 data 值（OpenAI 流终止哨兵不是合法 JSON）。
_DEFAULT_JSON_EXEMPT: Final[frozenset[str]] = frozenset({"[DONE]"})

# --- 坏帧原因常量（供 T17 接线到指标标签使用）---------------------------------
MALFORMED_INVALID_UTF8: Final[str] = "invalid_utf8"
MALFORMED_BAD_RETRY: Final[str] = "bad_retry"
MALFORMED_NUL_IN_ID: Final[str] = "nul_in_id"
MALFORMED_OVERSIZE: Final[str] = "oversize_event"
MALFORMED_BAD_JSON: Final[str] = "bad_json"


@dataclass(frozen=True, slots=True)
class SseFrame:
    """一个已完整分帧的 SSE 事件。

    不可变（``frozen``）且带 ``slots``，可安全跨协程传递、可直接用 ``==`` 做
    golden 对比 —— 属性测试的「分片无关」不变式正是靠整体相等来断言的。

    Attributes:
        event: ``event:`` 字段值；字段缺失时为 ``None``（而非规范默认的
            ``"message"``，保留「线上原样」信息；需要规范语义请用 ``event_type``）。
        data: 多行 ``data:`` 按 W3C 规范用 ``"\\n"`` 连接后的结果。出现过
            ``data:`` 字段但值为空时是 ``""``（而不是 ``None``）。
        id: ``id:`` 字段值；本帧未出现该字段时为 ``None``。跨事件粘滞的规范语义
            见 :attr:`SSEParser.last_event_id`。
        retry: ``retry:`` 字段值（毫秒）；缺失或非法时为 ``None``。
        raw: 构成本帧的原始字节（含各行终止符与结尾空行），golden 对比用。
    """

    event: str | None = None
    data: str = ""
    id: str | None = None
    retry: int | None = None
    raw: bytes = b""

    @property
    def event_type(self) -> str:
        """规范语义的事件类型：``event:`` 缺失或为空时回落到 ``"message"``。"""
        return self.event or "message"

    @property
    def raw_bytes(self) -> int:
        """本帧原始字节数（对齐架构文档 §3.4 的 ``raw_bytes`` 口径）。"""
        return len(self.raw)

    def is_done_sentinel(self) -> bool:
        """是否为 OpenAI 风格的 ``data: [DONE]`` 流终止哨兵。"""
        return self.data.strip() == "[DONE]"


#: 架构文档 §3.4 使用 ``SSEEvent`` 命名；保留别名以免文档口径与代码打架。
SSEEvent = SseFrame


class SSEParser:
    """增量式、字节级的 SSE 分帧器。

    典型用法::

        parser = SSEParser()
        async for chunk in response.aiter_bytes():
            for frame in parser.feed(chunk):     # 注意：同步调用，无 await（B10）
                handle(frame)
        for frame in parser.flush():             # 流结束时冲刷残留
            handle(frame)

    与 WHATWG 规范的**有意偏差**（均不影响分片无关性）：

    * ``id`` 不跨事件粘滞到 :class:`SseFrame`。规范里 "last event ID buffer" 会
      被后续事件继承，但对中转代理来说「本帧线上是否真的带了 id」是更有用的信息。
      规范语义通过 :attr:`last_event_id` 暴露。
    * ``flush()`` 会把「没有结尾空行的残留完整字段」也吐成一帧。规范要求丢弃，
      但上游提前断开时丢掉最后一个事件对代理是数据损失。
    * 只有注释、或只有 ``event:``/``id:`` 而无任何 ``data:`` 字段的块，按规范
      **不派发**事件。需要派发请置 ``emit_dataless=True``。
    """

    __slots__ = (
        "_bom_done",
        "_buf",
        "_data_parts",
        "_emit_dataless",
        "_event",
        "_id",
        "_json_exempt",
        "_last_event_id",
        "_malformed",
        "_max_event_bytes",
        "_raw_block",
        "_reasons",
        "_retry",
        "_skip_block",
        "_validate_json",
    )

    def __init__(
        self,
        *,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        validate_json: bool = False,
        json_exempt: frozenset[str] | None = None,
        emit_dataless: bool = False,
    ) -> None:
        """构造分帧器。

        Args:
            max_event_bytes: 单事件字节上限。超限的块整体丢弃并计一次坏帧，
                随后跳到下一个空行恢复解析（流不中断）。
            validate_json: 是否对每帧 ``data`` 做 JSON 合法性校验。默认 ``False``：
                分帧器本职是分帧，且 SSE 允许纯文本 payload，逐帧 ``json.loads``
                在高吞吐转发路径上是纯浪费（下游还会再解析一次）。LLM 流量场景
                （data 恒为 JSON）可显式打开以满足 R-P1-12 的坏帧观测。
                注意：**校验失败也照常产出该帧**，只是计数 +1，绝不吞数据。
            json_exempt: 免于 JSON 校验的 data 值集合，默认 ``{"[DONE]"}``。
            emit_dataless: 是否为「无任何 data 字段但有 event/id/retry」的块派发帧。

        Raises:
            ValueError: ``max_event_bytes`` 非正数。
        """
        if max_event_bytes <= 0:
            raise ValueError("max_event_bytes must be positive")
        self._max_event_bytes: int = int(max_event_bytes)
        self._validate_json: bool = bool(validate_json)
        self._json_exempt: frozenset[str] = _DEFAULT_JSON_EXEMPT if json_exempt is None else frozenset(json_exempt)
        self._emit_dataless: bool = bool(emit_dataless)

        self._buf: bytearray = bytearray()
        self._raw_block: bytearray = bytearray()
        self._data_parts: list[str] = []
        self._event: str | None = None
        self._id: str | None = None
        self._retry: int | None = None
        self._last_event_id: str = ""
        self._bom_done: bool = False
        self._skip_block: bool = False
        self._malformed: int = 0
        self._reasons: dict[str, int] = {}

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def feed(self, chunk: bytes) -> list[SseFrame]:
        """喂入一段原始字节，返回本次可以完整切出的帧列表。

        **同步纯函数，零 await**（架构文档 §0 偏差裁定 B10 / R-P1-65）。
        不完整的尾部字节（包括被切断的多字节 UTF-8 字符、可能是 CRLF 前半截的
        孤立 CR）会留在内部缓冲，等待下一次 ``feed()``。

        Args:
            chunk: 任意长度的字节片段，允许为空。

        Returns:
            本次新分出的帧，顺序与流内顺序一致；无完整帧时为空列表。

        Raises:
            TypeError: ``chunk`` 不是 bytes-like 对象。
        """
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError(f"chunk must be bytes-like, got {type(chunk).__name__}")
        if len(chunk) == 0:
            return []
        self._buf += chunk
        return self._drain(final=False)

    async def afeed(self, chunk: bytes) -> list[SseFrame]:
        """:meth:`feed` 的异步薄包装（架构文档 §5.5 口径）。

        内部零 await —— 解析是纯 CPU 操作，包一层只是为了让 async 调用方读起来
        自然，不引入任何调度开销。
        """
        return self.feed(chunk)

    def flush(self) -> list[SseFrame]:
        """流结束时冲刷残留缓冲，返回还能构成完整帧的部分。

        会把最后一行（即使没有行终止符）当作完整行处理，并对残留字段强制派发
        一次 —— 上游提前断开时不丢最后一个事件。调用后解析器块状态被清空，
        可继续 ``feed()`` 新流。
        """
        frames = self._drain(final=True)
        if not self._skip_block:
            residual = self._dispatch()
            if residual is not None:
                frames.append(residual)
        self._reset_block()
        self._skip_block = False
        self._buf.clear()
        self._bom_done = True
        return frames

    def finish(self) -> list[SseFrame]:
        """:meth:`flush` 的别名（架构文档 §3.4 使用 ``finish`` 命名）。"""
        return self.flush()

    async def aflush(self) -> list[SseFrame]:
        """:meth:`flush` 的异步薄包装，内部零 await。"""
        return self.flush()

    def reset(self) -> None:
        """丢弃全部状态与计数，把解析器恢复到刚构造的样子。"""
        self._buf.clear()
        self._reset_block()
        self._last_event_id = ""
        self._bom_done = False
        self._skip_block = False
        self._malformed = 0
        self._reasons.clear()

    @property
    def malformed_count(self) -> int:
        """累计坏帧数（R-P1-12）。解析过程从不抛异常，异常只体现在这个计数上。"""
        return self._malformed

    @property
    def invalid_frames(self) -> int:
        """:attr:`malformed_count` 的别名（架构文档 §3.4 命名）。"""
        return self._malformed

    @property
    def malformed_reasons(self) -> Mapping[str, int]:
        """坏帧按原因分桶的计数，键取自本模块的 ``MALFORMED_*`` 常量。"""
        return MappingProxyType(self._reasons)

    @property
    def pending_bytes(self) -> int:
        """尚未构成完整帧、滞留在解析器内部的字节数。"""
        return len(self._buf) + len(self._raw_block)

    @property
    def last_event_id(self) -> str:
        """规范语义的 "last event ID"：跨事件粘滞，初始为 ``""``。"""
        return self._last_event_id

    @staticmethod
    def parse(data: bytes, **kwargs: object) -> list[SseFrame]:
        """一次性解析完整字节流的便捷方法（等价 ``feed(data) + flush()``）。"""
        parser = SSEParser(**kwargs)  # type: ignore[arg-type]
        frames = parser.feed(data)
        frames.extend(parser.flush())
        return frames

    # ------------------------------------------------------------------
    # 内部：分行
    # ------------------------------------------------------------------
    def _drain(self, *, final: bool) -> list[SseFrame]:
        """从缓冲里尽可能多地切出完整行并逐行消费。

        行终止符按规范支持三种：CRLF、LF、CR。位于缓冲末尾的孤立 CR 是**歧义**的
        （下一个字节可能是 LF，构成 CRLF），非 ``final`` 时必须留在缓冲里等待 ——
        这正是分片无关性的关键一环。
        """
        frames: list[SseFrame] = []
        if not self._strip_bom(final=final):
            return frames

        buf = self._buf
        pos = 0
        size = len(buf)
        while pos < size:
            idx_lf = buf.find(_LF, pos)
            idx_cr = buf.find(_CR, pos)
            if idx_lf < 0 and idx_cr < 0:
                break
            if idx_cr < 0 or (0 <= idx_lf < idx_cr):
                end = idx_lf
                nxt = idx_lf + 1
            else:
                end = idx_cr
                if idx_cr + 1 < size:
                    nxt = idx_cr + 2 if buf[idx_cr + 1] == _LF else idx_cr + 1
                elif final:
                    nxt = idx_cr + 1
                else:
                    break  # 尾部孤立 CR：等下一个 chunk 才能判定是不是 CRLF
            frame = self._consume_line(bytes(buf[pos:end]), bytes(buf[pos:nxt]))
            pos = nxt
            if frame is not None:
                frames.append(frame)

        if pos:
            del buf[:pos]

        if final and buf:
            tail = bytes(buf)
            buf.clear()
            frame = self._consume_line(tail, tail)
            if frame is not None:
                frames.append(frame)

        # 单行超长（始终等不到行终止符）时的内存安全阀：丢弃并进入跳过模式。
        if len(buf) > self._max_event_bytes:
            if not self._skip_block:
                self._note_malformed(MALFORMED_OVERSIZE)
                self._reset_block()
                self._skip_block = True
            buf.clear()
        return frames

    def _strip_bom(self, *, final: bool) -> bool:
        """按规范剥掉流首的 UTF-8 BOM。

        Returns:
            ``True`` 表示可以继续解析；``False`` 表示首部字节还不足以判定是否为
            BOM（例如首个 chunk 只有 1 字节 ``\\xef``），需要等待更多字节。
        """
        if self._bom_done:
            return True
        buf = self._buf
        if len(buf) >= 3:
            if bytes(buf[:3]) == _UTF8_BOM:
                del buf[:3]
            self._bom_done = True
            return True
        if final or not _UTF8_BOM.startswith(bytes(buf)):
            self._bom_done = True
            return True
        return False

    # ------------------------------------------------------------------
    # 内部：消费单行 / 字段解析 / 派发
    # ------------------------------------------------------------------
    def _consume_line(self, line: bytes, raw_line: bytes) -> SseFrame | None:
        """消费一条完整行。返回非 ``None`` 表示这一行触发了事件派发。"""
        if self._skip_block:
            # 超限块的恢复：一路丢弃到下一个空行为止。
            if not line:
                self._skip_block = False
                self._reset_block()
            return None

        self._raw_block += raw_line
        if not line:
            return self._dispatch()
        if len(self._raw_block) > self._max_event_bytes:
            self._note_malformed(MALFORMED_OVERSIZE)
            self._reset_block()
            self._skip_block = True
            return None
        self._parse_field(line)
        return None

    def _parse_field(self, line: bytes) -> None:
        """解析一条非空行为 SSE 字段（规范 "process the field" 步骤）。"""
        if line[0] == _COLON:
            return  # 注释行：忽略，且**不算**坏帧

        text = self._decode(line)
        idx = text.find(":")
        if idx < 0:
            # 无冒号的行：整行是字段名，值为空字符串
            name, value = text, ""
        else:
            name = text[:idx]
            value = text[idx + 1 :]
            # 规范：冒号后**恰好一个**空格被去掉，多余空格保留
            if value[:1] == " ":
                value = value[1:]

        if name == "data":
            self._data_parts.append(value)
        elif name == "event":
            self._event = value
        elif name == "id":
            if "\x00" in value:
                self._note_malformed(MALFORMED_NUL_IN_ID)  # 规范：含 NUL 的 id 忽略
            else:
                self._id = value
                self._last_event_id = value
        elif name == "retry":
            if value.isascii() and value.isdigit():
                self._retry = int(value)
            else:
                self._note_malformed(MALFORMED_BAD_RETRY)
        # 其余字段名按规范静默忽略，不计坏帧

    def _dispatch(self) -> SseFrame | None:
        """在空行处派发事件；无内容可派发时返回 ``None`` 并复位块状态。"""
        has_data = bool(self._data_parts)
        has_meta = self._event is not None or self._id is not None or self._retry is not None
        if not has_data and not (self._emit_dataless and has_meta):
            # 纯注释块 / 空块：规范要求不派发
            self._reset_block()
            return None

        # 规范等价：逐条 data 追加 "\n"，末尾再去掉一个 "\n"
        data = "\n".join(self._data_parts)
        frame = SseFrame(
            event=self._event,
            data=data,
            id=self._id,
            retry=self._retry,
            raw=bytes(self._raw_block),
        )
        if self._validate_json:
            self._check_json(data)
        self._reset_block()
        return frame

    def _check_json(self, data: str) -> None:
        """可选的 data JSON 合法性校验；只计数，不影响帧的产出。"""
        probe = data.strip()
        if not probe or probe in self._json_exempt:
            return
        try:
            json.loads(probe)
        except (ValueError, TypeError):
            self._note_malformed(MALFORMED_BAD_JSON)

    def _decode(self, raw: bytes) -> str:
        """把一条完整行解码为 str。

        走到这里时行边界已由 ASCII 终止符切定，合法 UTF-8 流**不可能**触发异常
        —— 真触发说明上游发了非法字节，此时降级为 ``errors="replace"`` 并计坏帧，
        绝不让整条流因为一个坏字节而中断。
        """
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self._note_malformed(MALFORMED_INVALID_UTF8)
            return raw.decode("utf-8", errors="replace")

    def _note_malformed(self, reason: str) -> None:
        """累加坏帧计数并按原因分桶。"""
        self._malformed += 1
        self._reasons[reason] = self._reasons.get(reason, 0) + 1

    def _reset_block(self) -> None:
        """复位「当前事件块」的累积状态（``last_event_id`` 与计数不受影响）。"""
        self._raw_block.clear()
        self._data_parts.clear()
        self._event = None
        self._id = None
        self._retry = None

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"SSEParser(pending_bytes={self.pending_bytes}, malformed_count={self._malformed})"
