"""统一 SSE 断言器 + 字节级分帧 + golden 归一化。

三个对外能力：

1. :func:`parse_sse_bytes` —— 把下游收到的**原始字节**切成 :class:`SseFrame`
   列表。同时支持 ``\\n\\n`` 与 ``\\r\\n\\r\\n`` 两种事件分隔风格，以及 ``\\n``
   与 ``\\r\\n`` 两种行分隔风格。

2. :func:`assert_lifecycle` —— 断言一条 SSE 流的生命周期合法：
   ``created -> ... -> terminal -> [DONE]``，各阶段出现且只出现一次、顺序正确。

3. :func:`normalize_for_golden` —— 把易变字段（随机 id、``created`` 时间戳、
   ISO8601 时间串、裸 uuid）替换为稳定占位符，让 golden 基线可复现。
   **除被规则命中的那几个字段外，其余字节逐字节原样保留**——包括空白、
   换行风格、字段顺序、JSON 分隔符风格。这是靠正则做字节级替换（而不是
   JSON 反序列化再序列化）实现的。

本模块不依赖 ``zhongzhuan``，可独立使用。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

__all__ = [
    "PROTOCOL_ANTHROPIC",
    "PROTOCOL_OPENAI",
    "PROTOCOL_RESPONSES",
    "LifecycleReport",
    "SseFrame",
    "SseLifecycleError",
    "assert_lifecycle",
    "frame_events",
    "iter_data_json",
    "normalize_for_golden",
    "parse_sse_bytes",
]


PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_RESPONSES = "responses"

_SUPPORTED_PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC, PROTOCOL_RESPONSES)

# Responses 协议的三个合法终态事件（官方 API 语义）
RESPONSES_TERMINAL_EVENTS = (
    "response.completed",
    "response.failed",
    "response.incomplete",
)


class SseLifecycleError(AssertionError):
    """SSE 生命周期断言失败。

    继承自 ``AssertionError``，因此在 pytest 里表现为普通断言失败。
    """


# ---------------------------------------------------------------------------
# 分帧
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SseFrame:
    """一个 SSE 事件帧（不含结尾的空行分隔符）。"""

    index: int
    raw: bytes
    event: str | None = None
    data: str = ""
    event_id: str | None = None
    retry: int | None = None
    comments: tuple[str, ...] = ()

    @property
    def is_done(self) -> bool:
        """是否是 OpenAI 风格的 ``data: [DONE]`` 终止帧。"""
        return self.data.strip() == "[DONE]"

    @property
    def is_comment_only(self) -> bool:
        """是否是纯注释帧（如 keepalive 的 ``: keepalive``）。"""
        return bool(self.comments) and not self.data and self.event is None

    @property
    def has_data(self) -> bool:
        """是否携带 ``data:`` 负载。"""
        return self.data != ""

    def json(self) -> Any:
        """把 ``data`` 解析为 JSON 对象；解析失败抛 :class:`SseLifecycleError`。"""
        obj = self.json_or_none()
        if obj is _SENTINEL:
            raise SseLifecycleError(
                f"frame #{self.index} data 不是合法 JSON: {self.data[:200]!r}"
            )
        return obj

    def json_or_none(self) -> Any:
        """把 ``data`` 解析为 JSON；失败返回内部哨兵值（不抛异常）。"""
        text = self.data.strip()
        if not text or text == "[DONE]":
            return _SENTINEL
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return _SENTINEL

    def __repr__(self) -> str:  # pragma: no cover - 仅用于失败信息展示
        return (
            f"SseFrame(#{self.index}, event={self.event!r}, "
            f"data={self.data[:120]!r})"
        )


class _Sentinel:
    """`json_or_none` 的「解析失败」哨兵。"""

    def __repr__(self) -> str:  # pragma: no cover
        return "<not-json>"


_SENTINEL = _Sentinel()


def _split_frames(raw: bytes) -> list[bytes]:
    """按 SSE 事件边界切分原始字节。

    事件分隔符同时支持 ``\\r\\n\\r\\n``（4 字节）与 ``\\n\\n``（2 字节）。
    两者不会互相嵌套（``\\r\\n\\r\\n`` 的字节序列里不含 ``\\n\\n``），
    因此取「最靠前出现的那个」即可正确切分混合风格的流。

    尾部若存在未以空行结尾的残帧（上游断流场景），也会作为一帧返回。
    """
    frames: list[bytes] = []
    pos = 0
    total = len(raw)
    while pos < total:
        idx_crlf = raw.find(b"\r\n\r\n", pos)
        idx_lf = raw.find(b"\n\n", pos)
        candidates: list[tuple[int, int]] = []
        if idx_crlf != -1:
            candidates.append((idx_crlf, 4))
        if idx_lf != -1:
            candidates.append((idx_lf, 2))
        if not candidates:
            tail = raw[pos:]
            if tail.strip():
                frames.append(tail)
            break
        boundary, sep_len = min(candidates)
        chunk = raw[pos:boundary]
        if chunk.strip():
            frames.append(chunk)
        pos = boundary + sep_len
    return frames


def _parse_frame(index: int, raw: bytes) -> SseFrame:
    """把单帧原始字节解析成 :class:`SseFrame`。"""
    event: str | None = None
    event_id: str | None = None
    retry: int | None = None
    data_lines: list[str] = []
    comments: list[str] = []

    # 统一按 \n 切行后再剥掉行尾的 \r，从而同时兼容 LF 与 CRLF。
    for raw_line in raw.split(b"\n"):
        line = raw_line.decode("utf-8", errors="replace")
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            continue
        if line.startswith(":"):
            comments.append(line[1:].lstrip())
            continue
        if ":" in line:
            field_name, _, value = line.partition(":")
            # SSE 规范：冒号后的单个前导空格要去掉，其余空白保留。
            if value.startswith(" "):
                value = value[1:]
        else:
            field_name, value = line, ""
        field_name = field_name.strip()
        if field_name == "data":
            data_lines.append(value)
        elif field_name == "event":
            event = value
        elif field_name == "id":
            event_id = value
        elif field_name == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None
        # 其余未知字段按 SSE 规范忽略

    return SseFrame(
        index=index,
        raw=raw,
        event=event,
        data="\n".join(data_lines),
        event_id=event_id,
        retry=retry,
        comments=tuple(comments),
    )


def parse_sse_bytes(raw: bytes) -> list[SseFrame]:
    """把 SSE 原始字节切成帧列表。

    Args:
        raw: 下游实际收到的完整 SSE 字节流。

    Returns:
        按出现顺序排列的 :class:`SseFrame` 列表。纯注释帧（keepalive）也会
        保留在结果里——需要过滤时用 :attr:`SseFrame.is_comment_only`。

    Raises:
        TypeError: ``raw`` 不是 ``bytes``/``bytearray``。
    """
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        raise TypeError(f"parse_sse_bytes 需要 bytes，收到 {type(raw).__name__}")
    return [_parse_frame(i, chunk) for i, chunk in enumerate(_split_frames(raw))]


def frame_events(frames: Iterable[SseFrame]) -> list[str]:
    """提取每帧的「事件名」，便于失败时打印出可读的事件序列。

    优先取 ``event:`` 行；没有 ``event:`` 行时退化为 ``data`` JSON 里的
    ``type`` 字段（Responses / Anthropic 都会带），再不行就用 ``<data>`` /
    ``[DONE]`` / ``<comment>`` 占位。
    """
    names: list[str] = []
    for fr in frames:
        if fr.is_done:
            names.append("[DONE]")
            continue
        if fr.is_comment_only:
            names.append("<comment>")
            continue
        if fr.event:
            names.append(fr.event)
            continue
        obj = fr.json_or_none()
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            names.append(obj["type"])
        else:
            names.append("<data>")
    return names


def iter_data_json(frames: Iterable[SseFrame]) -> Iterator[tuple[SseFrame, Any]]:
    """遍历所有「data 是合法 JSON」的帧，产出 ``(frame, obj)``。"""
    for fr in frames:
        obj = fr.json_or_none()
        if obj is not _SENTINEL:
            yield fr, obj


# ---------------------------------------------------------------------------
# 生命周期断言
# ---------------------------------------------------------------------------


@dataclass
class LifecycleReport:
    """:func:`assert_lifecycle` 的结构化结果，便于用例做进一步断言。"""

    protocol: str
    frames: list[SseFrame] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    created_index: int = -1
    terminal_index: int = -1
    terminal_name: str = ""
    done_index: int = -1

    @property
    def data_frames(self) -> list[SseFrame]:
        """排除纯注释帧后的帧列表。"""
        return [f for f in self.frames if not f.is_comment_only]


def _fail(report_events: Sequence[str], message: str) -> None:
    """抛出带完整事件序列上下文的生命周期断言错误。"""
    rendered = " -> ".join(report_events) if report_events else "<空流>"
    raise SseLifecycleError(f"{message}\n实际事件序列: {rendered}")


def _assert_openai(frames: list[SseFrame], events: list[str],
                   report: LifecycleReport, require_done: bool) -> None:
    """OpenAI Chat Completions SSE 生命周期。

    Chat Completions 没有显式的 ``created`` 事件，约定：

    * created  = 第一个带 JSON 负载的 chunk（必须存在且带 ``id``）
    * terminal = ``choices[*].finish_reason`` 非空的那个 chunk（有且仅一个）
    * done     = ``data: [DONE]``（有且仅一个，且必须是最后一帧）

    ``stream_options.include_usage`` 产生的 usage-only chunk 允许出现在
    terminal 与 ``[DONE]`` 之间。
    """
    payload = [f for f in frames if not f.is_comment_only]
    if not payload:
        _fail(events, "openai 流为空：没有任何非注释帧")

    first_json_idx = -1
    finish_indices: list[int] = []
    done_indices: list[int] = []

    for pos, fr in enumerate(payload):
        if fr.is_done:
            done_indices.append(pos)
            continue
        obj = fr.json_or_none()
        if obj is _SENTINEL:
            continue
        if first_json_idx < 0:
            first_json_idx = pos
            if isinstance(obj, dict) and not obj.get("id"):
                _fail(events, "openai 首个数据 chunk 缺少 id 字段")
        if isinstance(obj, dict):
            for choice in obj.get("choices") or []:
                if isinstance(choice, dict) and choice.get("finish_reason"):
                    finish_indices.append(pos)
                    break

    if first_json_idx < 0:
        _fail(events, "openai 流里没有任何合法 JSON chunk（created 缺失）")
    if len(finish_indices) == 0:
        _fail(events, "openai 流缺少 finish_reason 终止 chunk")
    if len(finish_indices) > 1:
        _fail(events, f"openai 流出现 {len(finish_indices)} 个 finish_reason chunk，应恰好 1 个")

    if require_done:
        if len(done_indices) == 0:
            _fail(events, "openai 流缺少 data: [DONE]")
        if len(done_indices) > 1:
            _fail(events, f"openai 流出现 {len(done_indices)} 个 [DONE]，应恰好 1 个")
        if done_indices[0] != len(payload) - 1:
            _fail(events, "openai 的 [DONE] 不是最后一帧")
        if not (first_json_idx <= finish_indices[0] < done_indices[0]):
            _fail(events, "openai 事件顺序非法：要求 created <= finish_reason < [DONE]")
        report.done_index = done_indices[0]
    elif done_indices and not (first_json_idx <= finish_indices[0]):
        _fail(events, "openai 事件顺序非法：finish_reason 早于首个 chunk")

    report.created_index = first_json_idx
    report.terminal_index = finish_indices[0]
    report.terminal_name = "finish_reason"


def _assert_anthropic(frames: list[SseFrame], events: list[str],
                      report: LifecycleReport) -> None:
    """Anthropic Messages SSE 生命周期。

    * created  = ``message_start``（有且仅一个，且是第一个非注释帧）
    * terminal = ``message_stop``（有且仅一个，且是最后一个非注释帧）
    * ``message_delta`` 至少一次，且早于 ``message_stop``
    * ``content_block_start`` / ``content_block_stop`` 按 index 配对
    * Anthropic 协议**不发** ``[DONE]``
    """
    payload = [f for f in frames if not f.is_comment_only]
    if not payload:
        _fail(events, "anthropic 流为空：没有任何非注释帧")

    names = [n for n in events if n != "<comment>"]

    start_positions = [i for i, n in enumerate(names) if n == "message_start"]
    stop_positions = [i for i, n in enumerate(names) if n == "message_stop"]
    delta_positions = [i for i, n in enumerate(names) if n == "message_delta"]
    done_positions = [i for i, n in enumerate(names) if n == "[DONE]"]

    if len(start_positions) != 1:
        _fail(events, f"anthropic 的 message_start 出现 {len(start_positions)} 次，应恰好 1 次")
    if len(stop_positions) != 1:
        _fail(events, f"anthropic 的 message_stop 出现 {len(stop_positions)} 次，应恰好 1 次")
    if done_positions:
        _fail(events, "anthropic 协议不应出现 [DONE]")
    if start_positions[0] != 0:
        _fail(events, "anthropic 的 message_start 不是第一个事件")
    if stop_positions[0] != len(names) - 1:
        _fail(events, "anthropic 的 message_stop 不是最后一个事件")
    if not delta_positions:
        _fail(events, "anthropic 流缺少 message_delta（stop_reason 载体）")
    if max(delta_positions) > stop_positions[0]:
        _fail(events, "anthropic 的 message_delta 出现在 message_stop 之后")

    # content_block_start / stop 必须按 index 严格配对
    open_indices: list[int] = []
    for fr in payload:
        obj = fr.json_or_none()
        if not isinstance(obj, dict):
            continue
        etype = fr.event or obj.get("type")
        if etype == "content_block_start":
            open_indices.append(int(obj.get("index", -1)))
        elif etype == "content_block_stop":
            idx = int(obj.get("index", -1))
            if idx not in open_indices:
                _fail(events, f"anthropic 的 content_block_stop(index={idx}) 没有对应的 start")
            open_indices.remove(idx)
    if open_indices:
        _fail(events, f"anthropic 有未关闭的 content_block: index={open_indices}")

    report.created_index = start_positions[0]
    report.terminal_index = stop_positions[0]
    report.terminal_name = "message_stop"


def _assert_responses(frames: list[SseFrame], events: list[str],
                      report: LifecycleReport, require_done: bool) -> None:
    """OpenAI Responses SSE 生命周期。

    * created  = ``response.created``（有且仅一个，且是第一个非注释帧）
    * terminal = ``response.completed`` / ``response.failed`` /
      ``response.incomplete`` 三者合计有且仅一个
    * done     = ``data: [DONE]``（有且仅一个，且是最后一帧）
    """
    payload = [f for f in frames if not f.is_comment_only]
    if not payload:
        _fail(events, "responses 流为空：没有任何非注释帧")

    names = [n for n in events if n != "<comment>"]

    created_positions = [i for i, n in enumerate(names) if n == "response.created"]
    terminal_positions = [
        (i, n) for i, n in enumerate(names) if n in RESPONSES_TERMINAL_EVENTS
    ]
    done_positions = [i for i, n in enumerate(names) if n == "[DONE]"]

    if len(created_positions) != 1:
        _fail(events, f"responses 的 response.created 出现 {len(created_positions)} 次，应恰好 1 次")
    if created_positions[0] != 0:
        _fail(events, "responses 的 response.created 不是第一个事件")
    if len(terminal_positions) != 1:
        _fail(
            events,
            f"responses 的终态事件出现 {len(terminal_positions)} 次，应恰好 1 次"
            f"（合法终态：{', '.join(RESPONSES_TERMINAL_EVENTS)}）",
        )

    terminal_index, terminal_name = terminal_positions[0]
    if terminal_index <= created_positions[0]:
        _fail(events, "responses 的终态事件出现在 response.created 之前或同位")

    if require_done:
        if len(done_positions) != 1:
            _fail(events, f"responses 的 [DONE] 出现 {len(done_positions)} 次，应恰好 1 次")
        if done_positions[0] != len(names) - 1:
            _fail(events, "responses 的 [DONE] 不是最后一帧")
        if terminal_index > done_positions[0]:
            _fail(events, "responses 的终态事件出现在 [DONE] 之后")
        report.done_index = done_positions[0]

    report.created_index = created_positions[0]
    report.terminal_index = terminal_index
    report.terminal_name = terminal_name


def assert_lifecycle(
    events: bytes | bytearray | Sequence[SseFrame],
    protocol: str,
    *,
    require_done: bool = True,
) -> LifecycleReport:
    """断言一条 SSE 流的生命周期合法。

    统一语义：``created -> ... -> terminal -> [DONE]``，各阶段出现且只出现
    一次、顺序合法。三种协议的具体事件名不同，映射见各协议的私有断言函数。

    Args:
        events: 原始 SSE 字节，或已经用 :func:`parse_sse_bytes` 分好的帧列表。
        protocol: ``"openai"`` / ``"anthropic"`` / ``"responses"``。
        require_done: 是否要求存在终止哨兵。Anthropic 协议本身没有 ``[DONE]``，
            该参数对其无效。断流 / 错误场景可置 ``False`` 只校验前半段。

    Returns:
        :class:`LifecycleReport`，含帧列表、事件名序列与三个关键位置下标。

    Raises:
        SseLifecycleError: 生命周期非法。
        ValueError: ``protocol`` 不在支持列表内。
    """
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise ValueError(
            f"不支持的 protocol={protocol!r}，可选：{_SUPPORTED_PROTOCOLS}"
        )

    if isinstance(events, (bytes, bytearray)):
        frames = parse_sse_bytes(bytes(events))
    else:
        frames = list(events)

    names = frame_events(frames)
    report = LifecycleReport(protocol=protocol, frames=frames, events=names)

    if protocol == PROTOCOL_OPENAI:
        _assert_openai(frames, names, report, require_done)
    elif protocol == PROTOCOL_ANTHROPIC:
        _assert_anthropic(frames, names, report)
    else:
        _assert_responses(frames, names, report, require_done)

    return report


# ---------------------------------------------------------------------------
# golden 归一化
# ---------------------------------------------------------------------------

# 归一化规则：只命中「确定由运行时随机 / 时钟生成」的值。
#
# 设计取舍：不做 JSON 反序列化再序列化，而是在字节层面做正则替换。
# 这样除被规则命中的片段外，**其余字节逐字节保留**——空白、换行风格、字段
# 顺序、`ensure_ascii` 风格全部原样保留，才配称为「字节级 golden」。
#
# 同时规则刻意收紧到「带随机后缀的 id」：mock 上游喂进去的确定性 id
# （如 ``call_1`` / ``msg_fixture_0``）不会被抹掉，从而保留字段级覆盖。
_GOLDEN_RULES: tuple[tuple[re.Pattern[bytes], bytes], ...] = (
    # 代理自生成的 Chat Completions chunk id：chatcmpl-<24 hex>
    (re.compile(rb"chatcmpl-[0-9a-fA-F]{8,}"), b"chatcmpl-<ID>"),
    # 代理自生成的 Anthropic message id：msg_<24 hex>
    (re.compile(rb"msg_[0-9a-fA-F]{16,}"), b"msg_<ID>"),
    # 代理自生成的 Anthropic tool_use id：toolu_<22 hex>
    (re.compile(rb"toolu_[0-9a-fA-F]{16,}"), b"toolu_<ID>"),
    # Responses 协议的 resp_ / rs_ / fc_ / item_ id（base32 或 hex 随机段）
    (re.compile(rb"resp_[0-9a-zA-Z]{16,}"), b"resp_<ID>"),
    (re.compile(rb"\brs_[0-9a-zA-Z]{16,}"), b"rs_<ID>"),
    (re.compile(rb"\bfc_[0-9a-zA-Z]{16,}"), b"fc_<ID>"),
    (re.compile(rb"\bitem_[0-9a-zA-Z]{16,}"), b"item_<ID>"),
    (re.compile(rb"\bcall_[0-9a-fA-F]{16,}"), b"call_<ID>"),
    # 带连字符的标准 uuid4
    (
        re.compile(
            rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            rb"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        ),
        b"<UUID>",
    ),
    # ISO8601 时间戳
    (
        re.compile(
            rb"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
            rb"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
        ),
        b"<TS>",
    ),
    # "created": 1712345678 / "created_at": 1712345678（10~13 位 epoch）
    (
        re.compile(rb'("created(?:_at)?"\s*:\s*)\d{10,13}'),
        rb"\g<1>0",
    ),
)


def normalize_for_golden(
    raw: bytes,
    *,
    extra_rules: Sequence[tuple[re.Pattern[bytes], bytes]] | None = None,
    post: Callable[[bytes], bytes] | None = None,
) -> bytes:
    """把 SSE 原始字节里的易变字段替换为稳定占位符。

    被归一化的内容仅限：

    * 代理运行时随机生成的 id（``chatcmpl-``/``msg_``/``toolu_``/``resp_``/
      ``rs_``/``fc_``/``item_``/``call_`` 后接足够长的随机段）
    * 标准 uuid4
    * ISO8601 时间串
    * ``"created"`` / ``"created_at"`` 的 epoch 数值

    **其余字节一律逐字节保留**，包括空白、换行风格（LF / CRLF）、字段顺序、
    转义风格。因此 golden 文件能真实反映线路上的字节。

    Args:
        raw: 原始 SSE 字节。
        extra_rules: 追加的 ``(编译好的 bytes 正则, 替换字节)`` 规则，在内置
            规则之后应用。
        post: 可选的最终后处理钩子，接收并返回 ``bytes``。

    Returns:
        归一化后的字节。

    Raises:
        TypeError: ``raw`` 不是 ``bytes``/``bytearray``。
    """
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        raise TypeError(f"normalize_for_golden 需要 bytes，收到 {type(raw).__name__}")

    out = raw
    for pattern, replacement in _GOLDEN_RULES:
        out = pattern.sub(replacement, out)
    for pattern, replacement in extra_rules or ():
        out = pattern.sub(replacement, out)
    if post is not None:
        out = post(out)
    return out
