"""T17 acceptance tests for the Responses turn bridge (门面 + 组合).

Covers the v3-specific acceptance criteria that are not exercised by the
legacy ``test_responses.py``:

* ③ 流结束后 reasoning buffer 字段清空（R-P1-04 内存增长测试）；
* ④ ``reasoning_event_mode`` 三档各断言下游事件类型（Q1）；
* ⑤ mock 上游立即断开，事件序列首个为 ``response.created``。
"""

import json
import re

import pytest

from zhongzhuan.proxy.protocol.responses_bridge import ResponsesTurnBridge
from zhongzhuan.proxy.protocol.responses_models import ReasoningEventMode


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _event_names(text: str) -> list[str]:
    return [ln.split("event:", 1)[1].strip() for ln in text.splitlines() if ln.startswith("event:")]


async def _run(chunks: list[bytes], **kw):
    tr = ResponsesTurnBridge(**kw)
    out: list[bytes] = []
    for c in chunks:
        out.extend(await tr.feed(c))
    out.extend(await tr.afinish())
    return tr, b"".join(out).decode()


class TestReasoningBufferReleased:
    async def test_reasoning_buffer_cleared_after_finish(self):
        """③ R-P1-04: reasoning text is released at turn end."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "think step 1"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": " think step 2"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        tr, _ = await _run(chunks)
        # After finish, the ephemeral reasoning accumulator is released (R-P1-04).
        assert tr._acc.reasoning is None
        # Reasoning text no longer retained anywhere on the turn.
        assert tr._acc.reasoning is None or tr._acc.reasoning.text == ""

    async def test_no_reasoning_leak_into_next_turn(self):
        """A fresh bridge must not retain the previous turn's reasoning."""
        tr = ResponsesTurnBridge(model="m")
        await tr.feed(_sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "secret"}}]}))
        await tr.afinish()
        # New bridge -> no reasoning state at all.
        tr2 = ResponsesTurnBridge(model="m")
        assert tr2._acc.reasoning is None


class TestReasoningEventMode:
    async def test_summary_text_mode(self):
        """④ default SUMMARY_TEXT -> reasoning_summary_text.* family."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "t"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run(chunks)
        names = _event_names(text)
        assert "response.reasoning_summary_text.delta" in names
        assert "response.reasoning_summary_text.done" in names
        assert "response.reasoning_summary_part.added" in names
        assert "response.reasoning_summary_part.done" in names
        assert "response.reasoning_text.delta" not in names
        assert "response.reasoning_text_part.added" not in names

    async def test_text_mode(self):
        """④ reasoning_text mode -> reasoning_text.* family."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "t"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run(chunks, model="m", reasoning_event_mode=ReasoningEventMode.TEXT.value)
        names = _event_names(text)
        assert "response.reasoning_text.delta" in names
        assert "response.reasoning_text.done" in names
        assert "response.reasoning_text_part.added" in names
        assert "response.reasoning_text_part.done" in names
        assert "response.reasoning_summary_text.delta" not in names

    async def test_disabled_mode(self):
        """④ disabled -> no reasoning events at all."""
        chunks = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"reasoning_content": "t"}}]}),
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ]
        _, text = await _run(chunks, model="m", reasoning_event_mode=ReasoningEventMode.DISABLED.value)
        names = _event_names(text)
        assert not any("reasoning" in n for n in names)
        # Message still flows.
        assert "response.output_text.delta" in names


class TestToolCallCloseSafety:
    """证明 3 / P0: 工具调用收尾安全（铁律 2 / P0-4）。

    截断/非法参数绝不 emit ``arguments.done``、不产生可执行 function call；
    合法参数 emit 且 item_id 固定（即使 call_id 延迟绑定，output_item.added 与
    done 的 id 一致）。
    """

    def _tool_stream(self, arg_pieces: list[str], *, call_id: str = "call_1") -> list[bytes]:
        """构造一个带工具调用的 Chat Completions 流。

        ``call_id`` 故意在**首帧之后**才绑定（延迟绑定，§5.3），以便验证
        output_item.added 与 done 的 id 不随 call_id 变化。
        """
        chunks: list[bytes] = [
            _sse({"id": "c1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]}),
            # 首帧：只有 index，没有 id（call_id 延迟绑定）。
            _sse(
                {
                    "id": "c1",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "type": "function",
                                        "function": {"name": "get_weather", "arguments": ""},
                                    }
                                ]
                            },
                        }
                    ],
                }
            ),
        ]
        # 后续帧：携带延迟绑定的 call_id + arguments 片段。
        for piece in arg_pieces:
            chunks.append(
                _sse(
                    {
                        "id": "c1",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [{"index": 0, "id": call_id, "function": {"arguments": piece}}]
                                },
                            }
                        ],
                    }
                )
            )
        chunks.append(_sse({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}))
        return chunks

    def _events(self, text: str) -> list[dict]:
        out: list[dict] = []
        for raw in text.split("event:"):
            if "\ndata: " not in raw:
                continue
            _, data = raw.split("\ndata: ", 1)
            payload = data.strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def _by_type(self, text: str, event_type: str) -> list[dict]:
        return [e for e in self._events(text) if e.get("type") == event_type]

    async def test_valid_arguments_emit_done_and_close_completed(self):
        """合法参数：emit arguments.done + output_item.done(completed)。"""
        chunks = self._tool_stream(['{"city": "Bei', 'jing"}'], call_id="call_x")
        _, text = await _run(chunks)

        done_events = self._by_type(text, "response.function_call_arguments.done")
        assert len(done_events) == 1
        payload = json.loads(done_events[0]["arguments"])
        assert payload == {"city": "Beijing"}

        item_dones = self._by_type(text, "response.output_item.done")
        assert len(item_dones) == 1
        assert item_dones[0]["item"]["status"] == "completed"
        assert item_dones[0]["item"]["type"] == "function_call"

    async def test_truncated_arguments_never_emit_done(self):
        """截断参数：绝不 emit arguments.done，收尾为 incomplete。"""
        chunks = self._tool_stream(['{"cit', 'y": "Bei'], call_id="call_x")
        _, text = await _run(chunks)

        # 铁律 2: arguments 不完整 -> 绝不发出可执行的 function call。
        assert self._by_type(text, "response.function_call_arguments.done") == []
        item_dones = self._by_type(text, "response.output_item.done")
        assert len(item_dones) == 1
        assert item_dones[0]["item"]["status"] == "incomplete"
        assert item_dones[0]["item"]["type"] == "function_call"

    async def test_non_object_arguments_never_emit_done(self):
        """顶层非对象（如数组/标量）：同样收尾 incomplete，不 emit done。"""
        chunks = self._tool_stream(["[1, 2, 3]"], call_id="call_x")
        _, text = await _run(chunks)

        assert self._by_type(text, "response.function_call_arguments.done") == []
        item_dones = self._by_type(text, "response.output_item.done")
        assert len(item_dones) == 1
        assert item_dones[0]["item"]["status"] == "incomplete"

    async def test_item_id_stable_across_late_call_id_binding(self):
        """P0-4: call_id 延迟绑定时 added/done/arguments.done 的 item.id 全一致。

        仅断言 added.id == done.id 不够：旧逻辑下 open 用空 call_id（``fc_``）
        而 close 用迟到的 call_id（``fc_call_late``），close 事件被 emitter
        丢弃（close_without_open），item 由 ``terminate()`` 自动以 incomplete
        收尾 —— 此时 added.id 与 done.id 反而“碰巧”相等（都是 ``fc_``），
        恒真掩盖了断链。因此必须同时断言：
        1. ``arguments.done.item_id`` 等于已 announce 的 item id（旧逻辑下为
           ``fc_call_late`` ≠ ``fc_``，直接失败）；
        2. item 以 ``completed`` 收尾（旧逻辑下 done 被丢弃、自动收为
           ``incomplete``，直接失败）。
        """
        # 首帧无 id，后续帧才带 call_id —— 触发延迟绑定。
        chunks = self._tool_stream(['{"city": "Beijing"}'], call_id="call_late")
        _, text = await _run(chunks)

        added = self._by_type(text, "response.output_item.added")
        done = self._by_type(text, "response.output_item.done")
        args_done = self._by_type(text, "response.function_call_arguments.done")
        assert len(added) == 1 and len(done) == 1
        # 固定 item_id（来自 acc.item_id），不随 call_id 变化。
        assert added[0]["item"]["id"] == done[0]["item"]["id"]
        assert done[0]["item"]["id"] != "fc_call_late"  # 不再是 call_id 派生的 id
        # 铁律 2 / AC-4.1: arguments.done 必须引用同一个已 announce 的 item id。
        assert len(args_done) == 1
        assert args_done[0]["item_id"] == added[0]["item"]["id"]
        # 旧逻辑下 done 事件被 emitter 丢弃后自动收为 incomplete —— 必须 completed。
        assert done[0]["item"]["status"] == "completed"
        assert done[0]["item"]["type"] == "function_call"


class TestUpstreamDisconnect:
    async def test_first_event_is_response_created(self):
        """⑤ upstream dies immediately; first event is response.created."""
        tr = ResponsesTurnBridge(model="m")
        # No chunks at all -> upstream 0 bytes, immediate disconnect.
        out = tr.finish_safely()
        text = b"".join(out).decode()
        names = _event_names(text)
        assert names[0] == "response.created"
        assert "response.in_progress" in names
        assert "response.completed" in names

    async def test_immediate_disconnect_with_emitter(self):
        """⑤ bridge with a partial chunk then disconnect still starts cleanly."""
        tr = ResponsesTurnBridge(model="m")
        feed_out = await tr.feed(_sse({"id": "c1", "choices": [{"index": 0, "delta": {"content": "x"}}]}))
        out = await tr.afinish()
        text = b"".join(feed_out + out).decode()
        names = _event_names(text)
        assert names[0] == "response.created"
        assert "response.completed" in names
