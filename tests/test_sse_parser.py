"""字节级 SSEParser 的确定性单元测试（T14）。

覆盖架构文档 §11 / T14 的四条完成判据：
  ① `\\n\\n` / `\\r\\n\\r\\n`、单事件多行 data、注释行、event/id/retry
  ② 分片语义恒等（属性测试在 tests/property/test_sse_split.py，这里放定点样本）
  ③ UTF-8 多字节字符跨 chunk 边界不产生替换字符
  ④ 畸形帧使 malformed_count +1，且流仍能正确终止
"""

from __future__ import annotations

import json

import pytest

from zhongzhuan.proxy.protocol.sse_parser import (
    DEFAULT_MAX_EVENT_BYTES,
    MALFORMED_BAD_JSON,
    MALFORMED_BAD_RETRY,
    MALFORMED_INVALID_UTF8,
    MALFORMED_NUL_IN_ID,
    MALFORMED_OVERSIZE,
    SSEEvent,
    SSEParser,
    SseFrame,
)

REPLACEMENT = "\ufffd"


def parse_all(stream: bytes, chunk_size: int = 0) -> tuple[list[SseFrame], SSEParser]:
    """按 chunk_size 切分喂入（0 表示整流一次喂入），返回 (frames, parser)。"""
    parser = SSEParser()
    frames: list[SseFrame] = []
    if chunk_size <= 0:
        frames.extend(parser.feed(stream))
    else:
        for i in range(0, len(stream), chunk_size):
            frames.extend(parser.feed(stream[i : i + chunk_size]))
    frames.extend(parser.flush())
    return frames, parser


# ---------------------------------------------------------------------------
# 判据 ① 分隔符 / 多行 data / 注释行 / 字段
# ---------------------------------------------------------------------------
def test_lf_delimiter_single_frame() -> None:
    frames, parser = parse_all(b"data: hello\n\n")
    assert len(frames) == 1
    assert frames[0].data == "hello"
    assert frames[0].event is None
    assert frames[0].event_type == "message"
    assert parser.malformed_count == 0


def test_crlf_delimiter_single_frame() -> None:
    frames, _ = parse_all(b"data: hello\r\n\r\n")
    assert [f.data for f in frames] == ["hello"]


def test_cr_only_delimiter() -> None:
    """规范允许裸 CR 作为行终止符。"""
    frames, _ = parse_all(b"data: hello\r\r")
    assert [f.data for f in frames] == ["hello"]


def test_mixed_delimiters_in_one_stream() -> None:
    """同一条流里混用 \\n\\n 与 \\r\\n\\r\\n 必须都对。"""
    stream = b"data: a\n\ndata: b\r\n\r\ndata: c\n\ndata: d\r\n\r\n"
    frames, parser = parse_all(stream)
    assert [f.data for f in frames] == ["a", "b", "c", "d"]
    assert parser.malformed_count == 0


def test_multiline_data_joined_with_lf() -> None:
    """W3C 规范：单事件多行 data 用 \\n 连接。"""
    frames, _ = parse_all(b"data: line1\ndata: line2\ndata: line3\n\n")
    assert len(frames) == 1
    assert frames[0].data == "line1\nline2\nline3"


def test_multiline_data_crlf_variant() -> None:
    frames, _ = parse_all(b"data: line1\r\ndata: line2\r\n\r\n")
    assert frames[0].data == "line1\nline2"


def test_comment_line_ignored_and_not_malformed() -> None:
    stream = b": this is a heartbeat comment\ndata: payload\n\n"
    frames, parser = parse_all(stream)
    assert [f.data for f in frames] == ["payload"]
    assert parser.malformed_count == 0


def test_comment_only_block_emits_nothing() -> None:
    """纯注释块按规范不派发事件，也不算坏帧。"""
    stream = b": ping\n\n: ping\n\ndata: real\n\n"
    frames, parser = parse_all(stream)
    assert [f.data for f in frames] == ["real"]
    assert parser.malformed_count == 0


def test_event_id_retry_fields() -> None:
    stream = b"event: message_start\nid: evt-001\nretry: 3000\ndata: {}\n\n"
    frames, parser = parse_all(stream)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.event == "message_start"
    assert frame.event_type == "message_start"
    assert frame.id == "evt-001"
    assert frame.retry == 3000
    assert frame.data == "{}"
    assert parser.malformed_count == 0
    assert parser.last_event_id == "evt-001"


def test_unknown_field_ignored() -> None:
    frames, parser = parse_all(b"foo: bar\nxyzzy: 1\ndata: ok\n\n")
    assert [f.data for f in frames] == ["ok"]
    assert parser.malformed_count == 0


def test_field_without_colon_gets_empty_value() -> None:
    """规范：无冒号的整行是字段名，值为空串。"""
    frames, _ = parse_all(b"data\ndata: x\n\n")
    assert frames[0].data == "\nx"


def test_exactly_one_leading_space_removed() -> None:
    """冒号后恰好一个空格被去掉，多余空格保留。"""
    frames, _ = parse_all(b"data:   three-spaces\n\n")
    assert frames[0].data == "  three-spaces"

    frames, _ = parse_all(b"data:no-space\n\n")
    assert frames[0].data == "no-space"

    frames, _ = parse_all(b"data:\ttab\n\n")
    assert frames[0].data == "\ttab"


def test_empty_data_frame_emits_empty_string() -> None:
    """`data:` 出现但值为空 -> 派发一帧且 data == ""（规范 3.2/3.3 步骤）。"""
    for stream in (b"data:\n\n", b"data: \n\n"):
        frames, parser = parse_all(stream)
        assert len(frames) == 1, stream
        assert frames[0].data == ""
        assert parser.malformed_count == 0


def test_dataless_block_not_dispatched_by_default() -> None:
    frames, parser = parse_all(b"event: ping\n\ndata: x\n\n")
    assert [f.data for f in frames] == ["x"]
    assert parser.malformed_count == 0


def test_emit_dataless_flag_dispatches_metadata_only_block() -> None:
    parser = SSEParser(emit_dataless=True)
    frames = parser.feed(b"event: ping\n\n")
    frames.extend(parser.flush())
    assert len(frames) == 1
    assert frames[0].event == "ping"
    assert frames[0].data == ""


def test_consecutive_blank_lines_do_not_emit_phantom_frames() -> None:
    frames, parser = parse_all(b"\n\n\ndata: a\n\n\n\n\ndata: b\n\n")
    assert [f.data for f in frames] == ["a", "b"]
    assert parser.malformed_count == 0


def test_id_not_sticky_on_frame_but_sticky_on_parser() -> None:
    stream = b"id: a1\ndata: 1\n\ndata: 2\n\n"
    frames, parser = parse_all(stream)
    assert frames[0].id == "a1"
    assert frames[1].id is None  # 线上原样：本帧没带 id
    assert parser.last_event_id == "a1"  # 规范语义的粘滞值


def test_raw_bytes_preserved() -> None:
    stream = b"event: e\ndata: d\n\n"
    frames, _ = parse_all(stream)
    assert frames[0].raw == stream
    assert frames[0].raw_bytes == len(stream)


def test_raw_covers_only_its_own_block() -> None:
    stream = b"data: a\n\ndata: b\r\n\r\n"
    frames, _ = parse_all(stream)
    assert frames[0].raw == b"data: a\n\n"
    assert frames[1].raw == b"data: b\r\n\r\n"


def test_done_sentinel_helper() -> None:
    frames, _ = parse_all(b"data: {}\n\ndata: [DONE]\n\n")
    assert frames[0].is_done_sentinel() is False
    assert frames[1].is_done_sentinel() is True


def test_sse_event_alias_is_sse_frame() -> None:
    assert SSEEvent is SseFrame


# ---------------------------------------------------------------------------
# 判据 ② 分片语义恒等（定点样本；随机化在属性测试）
# ---------------------------------------------------------------------------
ANTHROPIC_LIKE = (
    b"event: message_start\r\n"
    b'data: {"type":"message_start","message":{"id":"msg_1"}}\r\n'
    b"\r\n"
    b": ping\n\n"
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","delta":{"text":"\xe4\xbd\xa0\xe5\xa5\xbd"}}\n'
    b"\n"
    b"event: content_block_delta\n"
    b'data: {"delta":{"text":"\xf0\x9f\x9a\x80"}}\n'
    b"\n"
    b"data: [DONE]\n\n"
)


@pytest.mark.parametrize("chunk_size", [0, 1, 2, 3, 5, 7, 13, 64, 1024])
def test_split_invariance_fixed_sizes(chunk_size: int) -> None:
    baseline, base_parser = parse_all(ANTHROPIC_LIKE, 0)
    frames, parser = parse_all(ANTHROPIC_LIKE, chunk_size)
    assert frames == baseline
    assert parser.malformed_count == base_parser.malformed_count


def test_split_between_cr_and_lf() -> None:
    """切点正好落在 \\r\\n 中间 —— 最容易写错的一处。"""
    stream = b"data: a\r\n\r\ndata: b\r\n\r\n"
    for cut in range(1, len(stream)):
        parser = SSEParser()
        frames = parser.feed(stream[:cut])
        frames.extend(parser.feed(stream[cut:]))
        frames.extend(parser.flush())
        assert [f.data for f in frames] == ["a", "b"], f"cut={cut}"


def test_split_inside_field_name() -> None:
    stream = b"event: delta\ndata: value\n\n"
    for cut in range(1, len(stream)):
        parser = SSEParser()
        frames = parser.feed(stream[:cut]) + parser.feed(stream[cut:]) + parser.flush()
        assert len(frames) == 1, f"cut={cut}"
        assert frames[0].event == "delta"
        assert frames[0].data == "value"


def test_lone_cr_held_across_chunk_boundary() -> None:
    """chunk 以孤立 CR 结尾时不能立刻断行，必须等下一个字节判定是否 CRLF。"""
    parser = SSEParser()
    assert parser.feed(b"data: a\r") == []
    assert parser.pending_bytes > 0
    frames = parser.feed(b"\n\r\n")
    frames.extend(parser.flush())
    assert [f.data for f in frames] == ["a"]


# ---------------------------------------------------------------------------
# 判据 ③ UTF-8 跨 chunk 边界零替换字符
# ---------------------------------------------------------------------------
UTF8_CORPUS = "中文内容 emoji 🚀🎉 家庭👨‍👩‍👧‍👦 数学符号 ∑∫√ 韩文 한국어 日本語テスト"


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 5, 8, 16])
def test_utf8_multibyte_never_produces_replacement_char(chunk_size: int) -> None:
    stream = f"data: {UTF8_CORPUS}\n\n".encode("utf-8")
    frames, parser = parse_all(stream, chunk_size)
    assert len(frames) == 1
    assert frames[0].data == UTF8_CORPUS
    assert REPLACEMENT not in frames[0].data
    assert parser.malformed_count == 0


def test_utf8_split_at_every_continuation_byte() -> None:
    """在每一个 UTF-8 续字节位置切开，都不得产生 U+FFFD。"""
    stream = f"data: {UTF8_CORPUS}\n\n".encode("utf-8")
    cuts = [i for i, b in enumerate(stream) if 0x80 <= b <= 0xBF]
    assert cuts, "corpus must contain multi-byte characters"
    for cut in cuts:
        parser = SSEParser()
        frames = parser.feed(stream[:cut]) + parser.feed(stream[cut:]) + parser.flush()
        assert len(frames) == 1, f"cut={cut}"
        assert frames[0].data == UTF8_CORPUS, f"cut={cut}"
        assert REPLACEMENT not in frames[0].data, f"cut={cut}"


def test_four_byte_emoji_split_all_ways() -> None:
    emoji = "🚀"
    stream = f"data: {emoji}\n\n".encode("utf-8")
    for cut in range(1, len(stream)):
        parser = SSEParser()
        frames = parser.feed(stream[:cut]) + parser.feed(stream[cut:]) + parser.flush()
        assert frames[0].data == emoji, f"cut={cut}"
        assert REPLACEMENT not in frames[0].data


def test_utf8_bom_stripped_whole_and_split() -> None:
    stream = b"\xef\xbb\xbf" + b"data: hello\n\n"
    for chunk_size in (0, 1, 2, 3, 4):
        frames, parser = parse_all(stream, chunk_size)
        assert [f.data for f in frames] == ["hello"], chunk_size
        assert parser.malformed_count == 0


def test_leading_bytes_that_only_look_like_bom() -> None:
    """流首是多字节中文（非 BOM）时不得误删字节。"""
    stream = "data: 中\n\n".encode("utf-8")
    frames, _ = parse_all(stream, 1)
    assert frames[0].data == "中"


def test_truncated_bom_only_stream_terminates() -> None:
    parser = SSEParser()
    assert parser.feed(b"\xef\xbb") == []
    assert parser.flush() == []  # 不挂死、不抛异常


# ---------------------------------------------------------------------------
# 判据 ④ 坏帧计数 + 流仍能正确终止
# ---------------------------------------------------------------------------
def test_malformed_json_counted_and_stream_still_terminates() -> None:
    stream = (
        b'data: {"ok":1}\n\n'
        b'data: {"broken": \n\n'          # 畸形 JSON
        b"data: not json at all\n\n"       # 畸形 JSON
        b'data: {"ok":2}\n\n'
        b"data: [DONE]\n\n"                # 哨兵豁免
    )
    parser = SSEParser(validate_json=True)
    frames = parser.feed(stream)
    frames.extend(parser.flush())

    assert len(frames) == 5, "坏帧也必须照常产出，绝不吞数据"
    assert parser.malformed_count == 2
    assert parser.malformed_reasons[MALFORMED_BAD_JSON] == 2
    assert frames[-1].is_done_sentinel() is True   # 流正确终止
    assert json.loads(frames[0].data) == {"ok": 1}
    assert json.loads(frames[3].data) == {"ok": 2}


def test_malformed_json_split_invariant() -> None:
    stream = b'data: {"ok":1}\n\ndata: {oops\n\ndata: [DONE]\n\n'

    def run(size: int) -> tuple[list[SseFrame], int]:
        parser = SSEParser(validate_json=True)
        out: list[SseFrame] = []
        if size <= 0:
            out.extend(parser.feed(stream))
        else:
            for i in range(0, len(stream), size):
                out.extend(parser.feed(stream[i : i + size]))
        out.extend(parser.flush())
        return out, parser.malformed_count

    baseline = run(0)
    for size in (1, 2, 3, 7, 17):
        assert run(size) == baseline, size


def test_json_validation_off_by_default() -> None:
    frames, parser = parse_all(b"data: plain text payload\n\n")
    assert len(frames) == 1
    assert parser.malformed_count == 0


def test_bad_retry_counted_but_frame_kept() -> None:
    parser = SSEParser()
    frames = parser.feed(b"retry: soon\ndata: x\n\n")
    frames.extend(parser.flush())
    assert len(frames) == 1
    assert frames[0].retry is None
    assert frames[0].data == "x"
    assert parser.malformed_count == 1
    assert parser.malformed_reasons[MALFORMED_BAD_RETRY] == 1


def test_non_ascii_digit_retry_rejected() -> None:
    """全角/阿拉伯-印度数字不是规范认可的 ASCII digits。"""
    parser = SSEParser()
    frames = parser.feed("retry: \u0661\u0662\u0663\ndata: x\n\n".encode("utf-8"))
    frames.extend(parser.flush())
    assert frames[0].retry is None
    assert parser.malformed_reasons[MALFORMED_BAD_RETRY] == 1


def test_id_with_nul_rejected_and_counted() -> None:
    parser = SSEParser()
    frames = parser.feed(b"id: ab\x00cd\ndata: x\n\n")
    frames.extend(parser.flush())
    assert frames[0].id is None
    assert parser.malformed_count == 1
    assert parser.malformed_reasons[MALFORMED_NUL_IN_ID] == 1


def test_invalid_utf8_bytes_counted_and_stream_continues() -> None:
    """真正非法的字节序列降级为 replace 并计坏帧，后续帧照常解析。"""
    stream = b"data: \xff\xfe bad\n\ndata: good\n\n"
    parser = SSEParser()
    frames = parser.feed(stream)
    frames.extend(parser.flush())
    assert len(frames) == 2
    assert REPLACEMENT in frames[0].data
    assert frames[1].data == "good"
    assert parser.malformed_reasons[MALFORMED_INVALID_UTF8] == 1


def test_oversize_event_counted_and_parser_recovers() -> None:
    huge = b"x" * 500
    stream = b"data: " + huge + b"\n\ndata: after\n\n"
    for chunk_size in (0, 1, 7, 64):
        parser = SSEParser(max_event_bytes=64)
        frames: list[SseFrame] = []
        if chunk_size <= 0:
            frames.extend(parser.feed(stream))
        else:
            for i in range(0, len(stream), chunk_size):
                frames.extend(parser.feed(stream[i : i + chunk_size]))
        frames.extend(parser.flush())
        assert [f.data for f in frames] == ["after"], chunk_size
        assert parser.malformed_count == 1, chunk_size
        assert parser.malformed_reasons[MALFORMED_OVERSIZE] == 1, chunk_size


def test_never_raises_on_arbitrary_garbage() -> None:
    garbage = bytes(range(256)) * 4
    parser = SSEParser()
    parser.feed(garbage)
    parser.flush()  # 不抛异常即通过


def test_feed_rejects_str() -> None:
    parser = SSEParser()
    with pytest.raises(TypeError):
        parser.feed("data: x\n\n")  # type: ignore[arg-type]


def test_invalid_max_event_bytes() -> None:
    with pytest.raises(ValueError):
        SSEParser(max_event_bytes=0)


# ---------------------------------------------------------------------------
# flush / reset / async 包装 / 杂项
# ---------------------------------------------------------------------------
def test_flush_emits_frame_without_trailing_blank_line() -> None:
    """上游提前断开、最后一帧没有结尾空行时不丢数据。"""
    parser = SSEParser()
    frames = parser.feed(b"data: a\n\ndata: tail")
    assert [f.data for f in frames] == ["a"]
    tail = parser.flush()
    assert [f.data for f in tail] == ["tail"]


def test_flush_with_trailing_single_newline() -> None:
    parser = SSEParser()
    frames = parser.feed(b"data: tail\n")
    assert frames == []
    assert [f.data for f in parser.flush()] == ["tail"]


def test_flush_is_idempotent() -> None:
    parser = SSEParser()
    parser.feed(b"data: a\n\n")
    assert parser.flush() == []
    assert parser.flush() == []


def test_flush_drops_comment_only_residual() -> None:
    parser = SSEParser()
    parser.feed(b": just a comment")
    assert parser.flush() == []


def test_empty_chunk_is_noop() -> None:
    parser = SSEParser()
    assert parser.feed(b"") == []
    assert parser.feed(b"data: a\n\n")[0].data == "a"


def test_pending_bytes_tracks_buffer() -> None:
    parser = SSEParser()
    assert parser.pending_bytes == 0
    parser.feed(b"data: partial")
    assert parser.pending_bytes == len(b"data: partial")
    parser.flush()
    assert parser.pending_bytes == 0


def test_reset_clears_everything() -> None:
    parser = SSEParser()
    parser.feed(b"retry: nope\ndata: x")
    assert parser.malformed_count == 1
    parser.reset()
    assert parser.pending_bytes == 0
    assert parser.malformed_count == 0
    assert parser.malformed_reasons == {}
    assert parser.last_event_id == ""
    frames = parser.feed(b"data: fresh\n\n")
    assert [f.data for f in frames] == ["fresh"]


def test_parser_reusable_after_flush() -> None:
    parser = SSEParser()
    parser.feed(b"data: s1\n\n")
    parser.flush()
    frames = parser.feed(b"data: s2\n\n")
    assert [f.data for f in frames] == ["s2"]


def test_invalid_frames_alias() -> None:
    parser = SSEParser()
    parser.feed(b"retry: bad\ndata: x\n\n")
    assert parser.invalid_frames == parser.malformed_count == 1


def test_malformed_reasons_is_read_only() -> None:
    parser = SSEParser()
    with pytest.raises(TypeError):
        parser.malformed_reasons["boom"] = 1  # type: ignore[index]


def test_parse_static_helper() -> None:
    frames = SSEParser.parse(b"data: a\n\ndata: b\n\n")
    assert [f.data for f in frames] == ["a", "b"]


def test_frame_is_frozen_and_hashable() -> None:
    frame = SseFrame(event="e", data="d")
    with pytest.raises(Exception):
        frame.data = "other"  # type: ignore[misc]
    assert hash(frame) == hash(SseFrame(event="e", data="d"))


def test_default_max_event_bytes_constant() -> None:
    assert DEFAULT_MAX_EVENT_BYTES == 8 * 1024 * 1024


async def test_afeed_matches_feed() -> None:
    """afeed 是零 await 的薄包装，语义必须与 feed 完全一致（B10）。"""
    stream = ANTHROPIC_LIKE
    sync_parser = SSEParser()
    expected = sync_parser.feed(stream) + sync_parser.flush()

    async_parser = SSEParser()
    actual: list[SseFrame] = []
    for i in range(0, len(stream), 3):
        actual.extend(await async_parser.afeed(stream[i : i + 3]))
    actual.extend(await async_parser.aflush())

    assert actual == expected


async def test_feed_is_not_a_coroutine_function() -> None:
    """B10 硬性裁定：feed 必须是同步方法，不得是 async def。"""
    import inspect

    assert not inspect.iscoroutinefunction(SSEParser.feed)
    assert not inspect.iscoroutinefunction(SSEParser.flush)
    assert inspect.iscoroutinefunction(SSEParser.afeed)


# ---------------------------------------------------------------------------
# 真实场景回归样本
# ---------------------------------------------------------------------------
def test_openai_tool_call_arguments_split_by_one_byte() -> None:
    """工具调用 arguments 被逐字节切分后必须完整还原（Codex 间歇失败的根因）。"""
    payload = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_abc",
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": "F:/tmp/测试.py", "content": "def f():\n    return '🚀'\n"},
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ]
                }
            }
        ]
    }
    body = json.dumps(payload, ensure_ascii=False)
    stream = f"data: {body}\n\ndata: [DONE]\n\n".encode("utf-8")

    frames, parser = parse_all(stream, 1)
    assert len(frames) == 2
    assert REPLACEMENT not in frames[0].data
    assert json.loads(frames[0].data) == payload
    assert frames[1].is_done_sentinel()
    assert parser.malformed_count == 0


def test_anthropic_stream_shape() -> None:
    frames, parser = parse_all(ANTHROPIC_LIKE)
    assert [f.event for f in frames] == [
        "message_start",
        "content_block_delta",
        "content_block_delta",
        None,
    ]
    assert json.loads(frames[2].data)["delta"]["text"] == "🚀"
    assert parser.malformed_count == 0


def test_json_string_value_containing_escaped_newline() -> None:
    body = json.dumps({"text": "first\nsecond\nthird"})
    frames, _ = parse_all(f"data: {body}\n\n".encode("utf-8"), 1)
    assert json.loads(frames[0].data)["text"] == "first\nsecond\nthird"


def test_pretty_printed_json_across_multiple_data_lines() -> None:
    body = json.dumps({"a": 1, "b": [1, 2]}, indent=2)
    lines = b"".join(b"data: " + ln.encode("utf-8") + b"\n" for ln in body.split("\n"))
    frames, _ = parse_all(lines + b"\n", 1)
    assert len(frames) == 1
    assert json.loads(frames[0].data) == {"a": 1, "b": [1, 2]}
