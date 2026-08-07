"""Responses Bridge v3 tool-call aggregation (T11).

Owns the :class:`ToolCallAccumulator` and the collection that groups them, per
§5.3 of the v3 architecture document ("ToolCallAccumulator").

Why this must exist as its own module:
    A tool call's ``name``, ``call_id`` and ``arguments`` can arrive across
    arbitrary SSE chunk boundaries and may be interleaved with parallel tool
    calls (§9.3, 铁律 2).  The accumulator guarantees that:

    * a name is accumulated either by append or by full-value replacement
      (both upstream families are legal, §5.3);
    * arguments fragments are appended verbatim and only validated at the end
      (never "completed" early just because they happen to be valid JSON);
    * a call is matched by ``call_id`` first, then by ``source_index``, then a
      new accumulator is created (matching priority, §5.3);
    * a stable ``call_id`` is synthesised when the upstream never sends one
      (``call_{response_id}_{source_index}``, R-P1-07);
    * exactly one ``finish()`` is emitted per call (duplicate chunks are
      dropped, §9.3).

This module imports only from :mod:`.responses_models` so it stays free of
cycles and can be reused by the stream parser, the turn accumulator and the
v3 handlers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .responses_models import (
    canonical_json,
    make_function_call_item_id_stable,
    make_synthetic_call_id,
)


#: namespace 摊平分隔符，与 ``responses.py`` 的 :data:`NAMESPACE_FLAT_SEP` 保持一致。
#: 摊平名形如 ``mcp__subagents__-spawn_agent`` —— ``mcp__{server}__`` 是 namespace，
#: 连字符后是子工具名。回包还原只依赖「连字符分隔 + 前缀含 ``mcp__``」这个约定，
#: 不需要额外的映射表。（为什么是 ``-`` 而不是 ``.``：OpenAI Chat Completions
#: 的 function name 正则 ``^[a-zA-Z0-9_-]+$`` 不允许点，2026-08-07 实测上游 400。）
NAMESPACE_FLAT_SEP: str = "-"
_NAMESPACE_PREFIX: str = "mcp__"


def split_namespace_name(flat_name: str) -> tuple[str, str]:
    """把摊平的 function 名还原成 ``(namespace, subtool_name)``。

    规则（与 codex-relay #17 结论一致，分隔符按上游约束用连字符）：

    * ``mcp__subagents__-spawn_agent`` -> ``("mcp__subagents__", "spawn_agent")``；
    * 嵌套 namespace 摊平后是 ``mcp__outer__-mcp__inner__-subtool``，取**最后**
      一个连字符前段做 namespace（Codex 只认一层 ``mcp__{server}__``），名字
      保留最后一段——用 ``rpartition`` 而非 ``partition``，因为 namespace 名
      本身可能含连字符（``mcp__a-b__``）；
    * 不是摊平名（无连字符、或前缀不是 ``mcp__``）-> ``("", 原样)`` —— 普通
      function 调用不受影响。

    Args:
        flat_name: 上游返回的 function 名。

    Returns:
        ``(namespace, subtool_name)``；namespace 为空表示非 namespace 调用。
    """
    name = flat_name or ""
    if NAMESPACE_FLAT_SEP in name and name.startswith(_NAMESPACE_PREFIX):
        ns, _, rest = name.rpartition(NAMESPACE_FLAT_SEP)
        return ns, rest
    return "", name


@dataclass
class ToolCallAccumulator:
    """Accumulate one tool call across arbitrary SSE fragmentation.

    ``source_index`` is the upstream Chat/Anthropic tool index (stable within a
    choice); ``output_index`` is the global Responses output index allocated
    once when the call is first created (§5.4).  ``call_id`` binds late -- the
    upstream may reveal it after the first fragment (§5.3).
    """

    source_index: int
    output_index: int
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    item_added: bool = False
    arguments_done: bool = False
    item_done: bool = False

    #: Codex 26.x MCP 子代理命名空间（如 ``mcp__subagents__``）。
    #:
    #: TRANSLATE 路径把请求里的 ``type:"namespace"`` 容器摊平成点分隔 function
    #: 名（``mcp__subagents__-spawn_agent``），回包时要把摊平名**还原**成
    #: ``name=spawn_agent`` + ``namespace=mcp__subagents__``，Codex 才能把这次
    #: function_call 路由回对应的 MCP server（codex-relay #17 / Palantir 修法）。
    #: 该字段由 :meth:`split_namespace_name` 解析摊平名得到，pipeline 发射
    #: ``output_item.added/done`` 时写进 item。
    namespace: str = ""

    #: Accumulation mode for ``name``: append or full-value replace (§5.3).
    name_mode: str = "replace"

    #: P0-4: the Responses ``item.id`` for this call.  Fixed by
    #: :meth:`ToolCallCollection.ensure` at creation time from
    #: ``response_id + output_index`` and never rewritten by
    #: :meth:`bind_call_id`, so ``output_item.added`` and ``output_item.done``
    #: always carry the same id even when ``call_id`` binds late (AC-4.1).
    item_id: str = ""

    # -- mutation helpers ----------------------------------------------------

    def bind_call_id(self, call_id: str) -> None:
        """Bind (or rebind) the call id.  Late binding is idempotent.

        ``item_id`` is deliberately left untouched: the Responses item identity
        must survive a late ``call_id`` (P0-4 / AC-4.2).
        """
        if call_id:
            self.call_id = call_id

    def append_name(self, fragment: str) -> None:
        """Append a name fragment (chunked-name upstream)."""
        if not fragment:
            return
        if self.name_mode == "append":
            self.name += fragment
        else:
            self.name = fragment

    def replace_name(self, name: str) -> None:
        """Replace the name wholesale (repeated-full-name upstream)."""
        if name:
            self.name = name
            self.name_mode = "replace"

    def append_arguments(self, fragment: str) -> None:
        """Append an arguments fragment verbatim (never parsed eagerly)."""
        if fragment:
            self.arguments += fragment

    # -- lifecycle -----------------------------------------------------------

    @property
    def has_name(self) -> bool:
        return bool(self.name)

    @property
    def has_arguments(self) -> bool:
        return bool(self.arguments)

    def is_complete(self) -> bool:
        """A call is complete when it has a name and its arguments validated."""
        return self.has_name and self.arguments_done

    def validate_arguments(self, *, require_object: bool = True) -> bool:
        """Parse ``.arguments`` and decide whether it is a complete call.

        ``require_object`` defaults to ``True`` (the top level must be a JSON
        object, §5.3).  On success the accumulators are marked done; on failure
        ``item_done`` stays ``False`` so the caller never emits a runnable
        function call.
        """
        text = self.arguments.strip()
        if not text:
            return False
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return False
        if require_object and not isinstance(parsed, dict):
            return False
        self.arguments_done = True
        return True

    def mark_item_done(self) -> None:
        """Mark the output item done.  Idempotent (duplicate chunks dropped)."""
        self.item_done = True

    # -- signature -----------------------------------------------------------

    def signature(self) -> str:
        """Stable digest of tool name + arguments (loop-breaker, §9.4).

        The arguments are parsed and canonically re-serialised so that a model
        which only changes JSON key order, whitespace or trivia is still judged
        as the *same* call (§9.4: "模型仅改变无意义空白、JSON 键顺序或 call ID
        时，仍视为同一调用").  Unparseable arguments fall back to the raw text.
        """
        try:
            parsed = json.loads(self.arguments)
        except (ValueError, TypeError):
            parsed = self.arguments
        return canonical_json({"name": self.name, "arguments": parsed})


class ToolCallCollection:
    """The group of in-flight tool calls, keyed by both call id and index.

    §5.3 matching priority:
    1. an existing ``call_id``;
    2. an established ``source_index`` -> accumulator mapping;
    3. a brand-new accumulator.
    """

    def __init__(self, *, response_id: str = "", require_json_object: bool = True) -> None:
        self._response_id = response_id
        self._require_json_object = require_json_object
        self.tools_by_call_id: dict[str, ToolCallAccumulator] = {}
        self.tools_by_source_index: dict[int, ToolCallAccumulator] = {}

    # -- lookup --------------------------------------------------------------

    def get(self, call_id: str = "", source_index: int | None = None) -> ToolCallAccumulator | None:
        """Resolve by call id, then by source index (priority order)."""
        if call_id:
            acc = self.tools_by_call_id.get(call_id)
            if acc is not None:
                return acc
        if source_index is not None:
            return self.tools_by_source_index.get(source_index)
        return None

    def ensure(
        self,
        *,
        output_index: int,
        call_id: str = "",
        source_index: int | None = None,
    ) -> ToolCallAccumulator:
        """Return the accumulator for a fragment, creating one if needed.

        A newly created accumulator gets its ``item_id`` fixed here from
        ``response_id + output_index`` (P0-4).  Re-resolving an existing
        accumulator only ever binds the ``call_id``; the item identity that has
        already been published to the client is never rewritten.
        """
        existing = self.get(call_id=call_id, source_index=source_index)
        if existing is not None:
            if call_id:
                existing.bind_call_id(call_id)
                # Re-index under the freshly bound id so later fragments that
                # carry only the call id resolve to the same accumulator.
                self.tools_by_call_id[call_id] = existing
            return existing

        idx = source_index if source_index is not None else output_index
        acc = ToolCallAccumulator(
            source_index=idx,
            output_index=output_index,
            call_id=call_id or self._synthetic(source_index),
            item_id=make_function_call_item_id_stable(self._response_id, output_index),
        )
        self._index(acc)
        return acc

    def finalize_call_id(self, call_id: str, source_index: int | None = None) -> None:
        """Bind a late-arriving call id to an index-keyed accumulator."""
        if source_index is None:
            return
        acc = self.tools_by_source_index.get(source_index)
        if acc is not None and call_id:
            acc.bind_call_id(call_id)
            self.tools_by_call_id[call_id] = acc

    def list_all(self) -> list[ToolCallAccumulator]:
        """All live accumulators, deduplicated by object identity."""
        seen: set[int] = set()
        out: list[ToolCallAccumulator] = []
        for acc in list(self.tools_by_call_id.values()) + list(self.tools_by_source_index.values()):
            if id(acc) not in seen:
                seen.add(id(acc))
                out.append(acc)
        return out

    def completed(self) -> list[ToolCallAccumulator]:
        """Calls whose arguments validated — runnable tool calls."""
        return [a for a in self.list_all() if a.is_complete()]

    def incomplete(self) -> list[ToolCallAccumulator]:
        """Calls that never validated (truncated / invalid arguments)."""
        return [a for a in self.list_all() if not a.is_complete()]

    # -- internals -----------------------------------------------------------

    def _synthetic(self, source_index: int | None) -> str:
        return make_synthetic_call_id(self._response_id, source_index if source_index is not None else 0)

    def _index(self, acc: ToolCallAccumulator) -> None:
        if acc.call_id:
            self.tools_by_call_id[acc.call_id] = acc
        self.tools_by_source_index[acc.source_index] = acc


__all__ = [
    "ToolCallAccumulator",
    "ToolCallCollection",
    "split_namespace_name",
]
