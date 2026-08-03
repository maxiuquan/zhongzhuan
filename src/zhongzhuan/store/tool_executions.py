"""hosted tool 执行记录存储（T26 / R-P1-46、R-P1-47）。

这一层是「闸门①：请求侧不丢弃」的落地位置（PRD §3 范围裁定表）。客户端在
``tools`` 数组里声明的每一个 hosted tool，无论最终能不能执行，都必须先在
``tool_executions`` 里留下一行 —— 之后无论是返回 400、走原生直通，还是运行期
才发现无法执行，都能凭这行记录回答「代理到底看见了什么、做了什么」。

与 :class:`~zhongzhuan.store.response_store.ResponseStore` 的分工
------------------------------------------------------------------
``ResponseStore.record_tool_execution`` 是 T16 建立的 **function call** 视角：
``call_id`` + ``tool_name`` + 结果摘要。本类是 **hosted tool** 视角：
``tool_seq``（在 ``tools`` 数组里的下标）+ ``tool_type`` + ``capability``。
两者共用同一张 v004 表（v006 加列），因为它们共享同一份执行语义 —— 审批状态、
幂等键、TTL、租户键。T27 的 MCP 审批只需查一张表就能拿到全貌。

主键约定
--------
``execution_id = "{response_id}#{tool_seq}"``。这是确定性构造，等价于架构任务书
要求的 ``PRIMARY KEY(response_id, tool_seq)``，但不需要在 SQLite 上重建表 ——
理由见 :mod:`.migrations.v006_tool_executions`。

列名映射
--------
对外暴露 ``approval_state``，落库写的是 v004 已有的 ``approval`` 列。不新增
``approval_state`` 列：同一个状态存两处，迟早会出现两处不一致而没人知道以哪个
为准。空字符串（v004 默认值）在读出时归一为 :data:`APPROVAL_NONE`。

HONEST STUB
-----------
:meth:`ToolExecutionStore.set_approval` 只负责状态落库。真正的
``mcp_approval_request`` / ``mcp_approval_response`` item 与事件族是 T27；
T26 只保证「审批状态可写、可读、可筛」这一个槽位是真的。
"""
from __future__ import annotations

import time
from typing import Any

from .store import Store

#: 未进入审批流程（hosted tool 默认不需要审批）。
APPROVAL_NONE: str = "none"

#: 等待客户端 ``mcp_approval_response``（T27 消费）。
APPROVAL_PENDING: str = "pending"

#: 审批通过 / 拒绝。
APPROVAL_APPROVED: str = "approved"
APPROVAL_REJECTED: str = "rejected"

#: :meth:`ToolExecutionStore.set_approval` 接受的全部取值。写死成白名单是为了
#: 让拼错的状态在**写入时**炸掉，而不是在 T27 查 ``approval='pending'`` 查不到
#: 时才表现为「审批请求凭空消失」。
APPROVAL_STATES: frozenset[str] = frozenset({
    APPROVAL_NONE, APPROVAL_PENDING, APPROVAL_APPROVED, APPROVAL_REJECTED,
})

#: 一条 hosted tool 记录刚被识别、尚未交给任何执行器时的状态。
STATUS_RECOGNIZED: str = "recognized"

#: 对外返回的字段顺序，与 :meth:`ToolExecutionStore._SELECT` 的列顺序一一对应。
RECORD_FIELDS: tuple[str, ...] = (
    "response_id", "workspace_id", "tool_seq", "tool_type", "capability",
    "status", "approval_state", "idempotency_key", "created_at", "updated_at",
)

_SELECT_COLUMNS: str = (
    "response_id, workspace_id, tool_seq, tool_type, capability, "
    "status, approval, idempotency_key, created_at, updated_at"
)


def execution_id_for(response_id: str, tool_seq: int) -> str:
    """确定性主键：同一 ``(response_id, tool_seq)`` 永远落在同一行。"""
    return "{0}#{1}".format(response_id, int(tool_seq))


def _row_to_dict(row: tuple) -> dict[str, Any]:
    """把一行 ``tool_executions`` 转成对外 dict（列名 -> 字段名映射在此收口）。"""
    record = dict(zip(RECORD_FIELDS, row))
    record["tool_seq"] = int(record["tool_seq"])
    record["created_at"] = int(record["created_at"])
    record["updated_at"] = int(record["updated_at"])
    # v004 的 ``approval`` 默认是空串；对外统一成显式的 "none"。
    record["approval_state"] = str(record["approval_state"] or APPROVAL_NONE)
    return record


class ToolExecutionStore:
    """``tool_executions`` 表的 hosted tool 视图。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- 写入 ----------------------------------------------------------------

    async def record(
        self,
        *,
        response_id: str,
        workspace_id: str = "",
        tool_seq: int,
        tool_type: str,
        capability: str,
        status: str = STATUS_RECOGNIZED,
        approval_state: str = APPROVAL_NONE,
        idempotency_key: str = "",
        expires_at: int = 0,
    ) -> None:
        """登记一个被识别到的 hosted tool。

        重复调用同一 ``(response_id, tool_seq)`` 是幂等的：整行被覆盖，但
        ``created_at`` 保留首次写入的时间 —— 审计记录的「第一次看见」不该被一次
        重试抹掉（R-P1-47）。

        ``tool_name`` 列刻意留空：hosted tool 本来就没有 name，把 type 塞进去
        会让「这是不是一个 function call」在事后无法分辨。
        """
        if approval_state not in APPROVAL_STATES:
            raise ValueError(
                "unknown approval_state: {0!r} (expected one of {1})".format(
                    approval_state, sorted(APPROVAL_STATES),
                )
            )
        now = int(time.time())
        execution_id = execution_id_for(response_id, tool_seq)
        created_at = await self._created_at(execution_id, default=now)
        await self._store.execute(
            "INSERT OR REPLACE INTO tool_executions "
            "(execution_id, response_id, workspace_id, call_id, tool_name, "
            " idempotency_key, status, approval, result_digest, "
            " created_at, updated_at, expires_at, tool_seq, tool_type, capability) "
            "VALUES (?, ?, ?, '', '', ?, ?, ?, '', ?, ?, ?, ?, ?, ?)",
            (
                execution_id, response_id, workspace_id,
                idempotency_key, status, approval_state,
                created_at, now, int(expires_at),
                int(tool_seq), tool_type, capability,
            ),
        )

    async def set_approval(
        self, response_id: str, tool_seq: int, decision: str,
    ) -> None:
        """把一条记录的审批状态置为 ``approved`` / ``rejected``（往返的后半程）。

        不存在的记录是静默 no-op：UPDATE 影响 0 行。调用方要区分「批了」和
        「批了个不存在的东西」时，用 :meth:`get_for_response` 复核。
        """
        if decision not in APPROVAL_STATES:
            raise ValueError(
                "unknown approval decision: {0!r} (expected one of {1})".format(
                    decision, sorted(APPROVAL_STATES),
                )
            )
        await self._store.execute(
            "UPDATE tool_executions SET approval = ?, updated_at = ? "
            "WHERE response_id = ? AND tool_seq = ?",
            (decision, int(time.time()), response_id, int(tool_seq)),
        )

    async def set_status(
        self, response_id: str, tool_seq: int, status: str,
    ) -> None:
        """推进执行状态（``recognized`` -> ``rejected`` / ``dispatched`` / ...）。

        状态取值不设白名单：``status`` 的枚举归执行器所有，本层只负责落库。
        """
        await self._store.execute(
            "UPDATE tool_executions SET status = ?, updated_at = ? "
            "WHERE response_id = ? AND tool_seq = ?",
            (status, int(time.time()), response_id, int(tool_seq)),
        )

    # -- 读取 ----------------------------------------------------------------

    async def get_for_response(
        self, response_id: str, *, workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        """按 ``tool_seq`` 升序返回该 response 的全部 hosted tool 记录。

        ``tool_seq >= 0`` 过滤掉 v004 风格的 function call 行 —— 那些行属于
        :meth:`ResponseStore.record_tool_execution` 的视角，混进来只会让调用方
        看到 ``tool_type=''`` 的幽灵条目。
        """
        rows = await self._store.fetchall(
            "SELECT " + _SELECT_COLUMNS + " FROM tool_executions "
            "WHERE response_id = ? AND workspace_id = ? AND tool_seq >= 0 "
            "ORDER BY tool_seq",
            (response_id, workspace_id),
        )
        return [_row_to_dict(row) for row in rows]

    async def get_pending_approvals(
        self, *, workspace_id: str = "",
    ) -> list[dict[str, Any]]:
        """租户内所有 ``approval_state='pending'`` 的记录（T27 的输入）。"""
        rows = await self._store.fetchall(
            "SELECT " + _SELECT_COLUMNS + " FROM tool_executions "
            "WHERE workspace_id = ? AND approval = ? AND tool_seq >= 0 "
            "ORDER BY response_id, tool_seq",
            (workspace_id, APPROVAL_PENDING),
        )
        return [_row_to_dict(row) for row in rows]

    # -- 内部 ----------------------------------------------------------------

    async def _created_at(self, execution_id: str, *, default: int) -> int:
        row = await self._store.fetchone(
            "SELECT created_at FROM tool_executions WHERE execution_id = ?",
            (execution_id,),
        )
        return int(row[0]) if row is not None else default


__all__ = [
    "APPROVAL_NONE",
    "APPROVAL_PENDING",
    "APPROVAL_APPROVED",
    "APPROVAL_REJECTED",
    "APPROVAL_STATES",
    "STATUS_RECOGNIZED",
    "RECORD_FIELDS",
    "execution_id_for",
    "ToolExecutionStore",
]
