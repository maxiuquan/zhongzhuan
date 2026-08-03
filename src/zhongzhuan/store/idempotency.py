"""工具副作用幂等键存储（T26 / R-P1-47）。

R-P1-47 的判据是「同一幂等键重复请求**不二次执行**」。要做到这一点，判定必须
发生在执行之前，并且判定结果必须跨进程可见 —— 内存里的一个 ``set`` 在多 worker
部署下等于没做。所以幂等键落在 v004 建的 ``idempotency_records`` 表里，主键是
``(workspace_id, idempotency_key)``：租户隔离由主键本身保证，A 租户的键不可能
挡住 B 租户的请求。

三个状态
--------
``in_flight``
    已被某个执行者占住，但还没跑完。**同样阻断**第二次执行 —— 幂等的目的是
    「至多执行一次」，两个并发请求同时看到「没跑完」就双双开跑，正是要防的那
    件事。
``done``
    已执行完毕，``response_id`` 指向那次执行的结果，可直接回放。
``conflict``
    同一个键配了不同的请求体（T27 消费）。

为什么不直接复用 ``ResponseStore`` 的两个方法
---------------------------------------------
``ResponseStore.save_idempotency_record`` / ``get_idempotency_record`` 确实存在
（T16 建立），但后者只投影 ``(response_id, status_code, state)`` 三列，**丢掉了
``expires_at``**。而 ``idempotency_records`` 的 TTL 清扫器要到 T34 才落地
（见 :mod:`.retention` 模块头），在那之前，一个只按行存在性判定的 ``seen()``
会让过期的幂等键**永久**阻断同名请求。本类因此直接持有 :class:`Store` 并在读取
时比较 ``expires_at`` —— 与 :class:`~.background_jobs.BackgroundJobStore` 对
``background_jobs`` 的做法一致（``expires_at = 0`` 表示永不过期）。

T16 的两个方法保持原样，仍是 handler 回放路径的接口；本类是同一张表的
TTL 感知视图，不改它们的行为。

HONEST STUB
-----------
:meth:`IdempotencyStore.reserve` 只做「不存在则占位」的单条 CAS，够 T26 的
判定用；T27 接 MCP 工具副作用时若需要「占位失败后等待原执行者的结果」，那部分
（轮询 / 超时 / conflict 判定）不在 T26 范围内。
"""
from __future__ import annotations

import time
from typing import Any

from .store import Store

#: 默认 TTL：24 小时。与 OpenAI 官方 ``Idempotency-Key`` 的保留窗口同量级。
DEFAULT_TTL_SECONDS: int = 86400

#: 记录状态（见模块头）。
STATE_IN_FLIGHT: str = "in_flight"
STATE_DONE: str = "done"
STATE_CONFLICT: str = "conflict"

#: 阻断二次执行的状态集合。``conflict`` 不在其中：它表示同键异体，调用方要看到
#: 的是一个显式冲突错误，而不是「静默当作已执行」。
BLOCKING_STATES: frozenset[str] = frozenset({STATE_IN_FLIGHT, STATE_DONE})


class IdempotencyStore:
    """``idempotency_records`` 表的 TTL 感知视图。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- 判定 ----------------------------------------------------------------

    async def seen(
        self, key: str, *, workspace_id: str = "", now: int | None = None,
    ) -> bool:
        """该幂等键是否已被占用 —— ``True`` 表示**不得**再执行一次。

        空 key 恒为 ``False``：没带幂等键的请求本来就没有幂等承诺，把它们
        全部映射到同一个空串键会让不相干的请求互相阻断。
        """
        if not key:
            return False
        record = await self.lookup(key, workspace_id=workspace_id, now=now)
        return record is not None and record["state"] in BLOCKING_STATES

    async def lookup(
        self, key: str, *, workspace_id: str = "", now: int | None = None,
    ) -> dict[str, Any] | None:
        """返回未过期的记录，没有则 ``None``。

        过期行不会被删除（清扫是 T34 的事），只是不再被看见 —— 下一次
        :meth:`mark_executed` 会用 ``INSERT OR REPLACE`` 直接覆盖它。
        """
        if not key:
            return None
        row = await self._store.fetchone(
            "SELECT response_id, status_code, state, created_at, expires_at "
            "FROM idempotency_records "
            "WHERE workspace_id = ? AND idempotency_key = ?",
            (workspace_id, key),
        )
        if row is None:
            return None
        expires_at = int(row[4] or 0)
        ts = int(time.time()) if now is None else int(now)
        if expires_at and expires_at <= ts:
            return None
        return {
            "idempotency_key": key,
            "workspace_id": workspace_id,
            "response_id": str(row[0] or ""),
            "status_code": int(row[1] or 0),
            "state": str(row[2] or STATE_IN_FLIGHT),
            "created_at": int(row[3] or 0),
            "expires_at": expires_at,
        }

    # -- 写入 ----------------------------------------------------------------

    async def mark_executed(
        self,
        key: str,
        *,
        workspace_id: str = "",
        response_id: str = "",
        status_code: int = 200,
        request_digest: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: int | None = None,
    ) -> None:
        """记录「这个键已经执行过了」，之后 :meth:`seen` 返回 ``True``。"""
        await self._write(
            key,
            workspace_id=workspace_id,
            state=STATE_DONE,
            response_id=response_id,
            status_code=status_code,
            request_digest=request_digest,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    async def reserve(
        self,
        key: str,
        *,
        workspace_id: str = "",
        response_id: str = "",
        request_digest: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: int | None = None,
    ) -> bool:
        """占位：键未被占用时写入 ``in_flight`` 并返回 ``True``。

        返回 ``False`` 表示已有人占住 —— 调用方**不要**执行。
        """
        if not key:
            return True
        if await self.seen(key, workspace_id=workspace_id, now=now):
            return False
        await self._write(
            key,
            workspace_id=workspace_id,
            state=STATE_IN_FLIGHT,
            response_id=response_id,
            status_code=0,
            request_digest=request_digest,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        return True

    async def _write(
        self,
        key: str,
        *,
        workspace_id: str,
        state: str,
        response_id: str,
        status_code: int,
        request_digest: str,
        ttl_seconds: int,
        now: int | None,
    ) -> None:
        if not key:
            return
        ts = int(time.time()) if now is None else int(now)
        expires_at = ts + int(ttl_seconds) if ttl_seconds > 0 else 0
        await self._store.execute(
            "INSERT OR REPLACE INTO idempotency_records "
            "(workspace_id, idempotency_key, request_digest, response_id, "
            " status_code, state, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, key, request_digest, response_id,
             int(status_code), state, ts, expires_at),
        )


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "STATE_IN_FLIGHT",
    "STATE_DONE",
    "STATE_CONFLICT",
    "BLOCKING_STATES",
    "IdempotencyStore",
]
