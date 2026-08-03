"""T31 判据② -- Hypothesis 属性测试：分片语义恒等（R-P1-68 / §12.2）。

§12.2 原文（上游开发文档）：

    使用 Hypothesis 将同一 SSE 流随机切分成任意字节 chunk，验证输出语义不变：
    - UTF-8 多字节字符边界。
    - JSON 字符串转义边界。
    - ``\\r\\n`` 与 ``\\n`` 混合。
    - 一个 chunk 多事件、一个事件多 chunk。
    - tool arguments 每字节分片。

实现方式
--------
不变式就是 :class:`SSEParser` 的「分片无关性」（模块头 R-P1-68）：对同一条
完整字节流，无论如何切分成 chunk 序列，依次 ``feed()`` + ``flush()`` 得到的
帧序列与整块 ``parse()`` 完全相等，且合法 UTF-8 流切分绝不产生坏帧
（``malformed_count == 0``）。

每个场景 = 一个 ``@settings(max_examples=250)`` + ``@given(st.data())`` 的属性
测试：本环境 Hypothesis 6.165 已将旧版 ``min_examples`` 并入 ``max_examples``
语义（实测每个测试跑满 250 个 examples，≥ 判据要求的 200）。
外层 draw 出该边界下随机化的事件流内容，内层 draw 出随机切割点集合（任意
字节间隙断开），然后断言两种解析方式产生**完全相等**的 ``SseFrame`` 序列。
"""
from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from zhongzhuan.proxy.protocol.sse_parser import SSEParser, SseFrame


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _random_chunks(stream: bytes, cut_points: set[int]) -> list[bytes]:
    """把 ``stream`` 按切割点集合切成 chunk 序列（每个间隙可选断开）。

    ``cut_points`` 里每个值表示「在该字节下标之后断开」。空集合 = 单 chunk。
    """
    chunks: list[bytes] = []
    cur = bytearray()
    for i, b in enumerate(stream):
        cur.append(b)
        if i in cut_points:
            chunks.append(bytes(cur))
            cur = bytearray()
    if cur:
        chunks.append(bytes(cur))
    return chunks


def _split_invariant(stream: bytes, cut_points: set[int]) -> None:
    """断言：任意切分得到的帧序列 == 整块解析的帧序列，且零坏帧。"""
    baseline = SSEParser.parse(stream)
    parser = SSEParser()
    frames: list[SseFrame] = []
    for chunk in _random_chunks(stream, cut_points):
        frames.extend(parser.feed(chunk))
    frames.extend(parser.flush())
    assert frames == baseline, (
        f"fragmentation changed semantics; cuts={sorted(cut_points)}\n"
        f"got {frames!r}\nwant {baseline!r}"
    )
    assert parser.malformed_count == 0, "valid UTF-8 stream split must not corrupt"


#: 随机切割点：任何字节间隙都可能断开（含 1..n-1 全部）。空集合法（单 chunk）。
def _cuts(data: st.DataObject, n: int) -> set[int]:
    if n <= 1:
        return set()
    return set(data.draw(st.sets(
        st.integers(min_value=1, max_value=n - 1),
        max_size=n,
    )))


#: 多字节文本语料（中文 / 日文 / emoji / 组合字符）。
_UTF8_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs",),
        blacklist_characters=("\n", "\r"),
    ),
    max_size=200,
)


# ---------------------------------------------------------------------------
# 1. UTF-8 多字节字符边界
# ---------------------------------------------------------------------------


@settings(max_examples=250)
@given(st.data())
def test_utf8_multibyte_boundary_fragmentation(data):
    """跨多字节字符边界任意切分：解析出的 data 与原文逐字一致、无 U+FFFD。"""
    payload = data.draw(_UTF8_TEXT)
    stream = f"data: {payload}\n\n".encode("utf-8")
    _split_invariant(stream, _cuts(data, len(stream)))
    frames = SSEParser.parse(stream)
    assert [f.data for f in frames] == [payload]


# ---------------------------------------------------------------------------
# 2. JSON 字符串转义边界
# ---------------------------------------------------------------------------


@settings(max_examples=250)
@given(st.data())
def test_json_escape_boundary_fragmentation(data):
    """JSON 字符串含 ``\\n``/``\\t``/引号/反斜杠等转义，任意切分后原样保留。"""
    inner = data.draw(st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),
            blacklist_characters=("\n", "\r"),
        ),
        max_size=120,
    ))
    obj = json.dumps({
        "text": inner,
        "escaped": 'line1\\nline2\\ttab\\"quote\\\\back',
        "nested": {"k": [1, "中文", True]},
    }, ensure_ascii=False)
    stream = f"data: {obj}\n\n".encode("utf-8")
    _split_invariant(stream, _cuts(data, len(stream)))
    frames = SSEParser.parse(stream)
    assert [f.data for f in frames] == [obj]


# ---------------------------------------------------------------------------
# 3. \\r\\n 与 \\n 混合
# ---------------------------------------------------------------------------


@settings(max_examples=250)
@given(st.data())
def test_crlf_lf_mixed_fragmentation(data):
    """同一流里混合 CRLF 与 LF 行终止符；切点落在 CR/LF 中间也不改变语义。"""
    body = data.draw(st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),
            blacklist_characters=("\n", "\r"),
        ),
        max_size=60,
    ))
    stream = (
        b"event: message_start\r\n"
        b'data: {"type":"message_start","message":{"id":"m1"}}\r\n'
        b"\r\n"
        b": ping\n\n"  # 注释行，LF 终止
        b"event: content_block_delta\n"
        + f'data: {{"type":"content_block_delta","text":"{body}"}}\n'.encode()
        + b"\n"
        b"data: [DONE]\r\n"
        b"\r\n"
    )
    _split_invariant(stream, _cuts(data, len(stream)))


# ---------------------------------------------------------------------------
# 4. 一个 chunk 多事件、一个事件多 chunk
# ---------------------------------------------------------------------------


@settings(max_examples=250)
@given(st.data())
def test_multi_event_chunking_fragmentation(data):
    """流含多个事件：一 chunk 可吞多事件，一事件可被拆成多 chunk。"""
    count = data.draw(st.integers(min_value=2, max_value=6))
    parts = []
    for i in range(count):
        word = data.draw(st.text(
            alphabet=st.characters(blacklist_categories=("Cs",)),
            max_size=30,
        ))
        parts.append(f'data: {{"idx":{i},"text":"{word}"}}'.encode("utf-8"))
    stream = b"\n\n".join(parts) + b"\n\n" + b"data: [DONE]\n\n"
    _split_invariant(stream, _cuts(data, len(stream)))
    frames = SSEParser.parse(stream)
    assert len(frames) == count + 1  # count 个事件 + [DONE]
    assert frames[-1].is_done_sentinel()


# ---------------------------------------------------------------------------
# 5. tool arguments 每字节分片
# ---------------------------------------------------------------------------


@settings(max_examples=250)
@given(st.data())
def test_tool_arguments_byte_by_byte_fragmentation(data):
    """OpenAI 风格 tool_calls delta 流：arguments 每字节切分后语义不变。"""
    args = json.dumps({
        "query": data.draw(st.text(alphabet=st.characters(blacklist_categories=("Cs",)),
                                   max_size=60)),
        "filters": [1, 2, {"mode": "exact"}],
        "unicode": "北京🚀",
    }, ensure_ascii=False)
    tool_id = "call_" + data.draw(st.text(alphabet=st.characters(blacklist_categories=("Cs",)),
                                          min_size=3, max_size=8))
    # 构造完整 OpenAI tool_calls 增量 SSE（一个事件多行 data）。
    delta_obj = {
        "id": "chatcmpl-1",
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": tool_id,
                    "function": {"name": "search", "arguments": args},
                }],
            },
        }],
    }
    stream = f"data: {json.dumps(delta_obj, ensure_ascii=False)}\n\n".encode("utf-8")
    _split_invariant(stream, _cuts(data, len(stream)))
    # 每字节一种切法（最极端分片）也保持语义。
    parser = SSEParser()
    frames: list[SseFrame] = []
    for i in range(len(stream)):
        frames.extend(parser.feed(stream[i:i + 1]))
    frames.extend(parser.flush())
    assert frames == SSEParser.parse(stream)
    assert parser.malformed_count == 0
    # 解析出的 data 反序列化后仍是同一个工具调用（arguments 无损）。
    parsed = json.loads(frames[0].data)
    got_args = parsed["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(got_args) == json.loads(args)
