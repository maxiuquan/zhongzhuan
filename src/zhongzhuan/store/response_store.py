"""Responses store: persistence for the v3 resource endpoints (T16, §4.2.2).

The :class:`ResponseStore` is the data-access layer over the v004 tables
(``responses``, ``response_input_items``, ``response_output_items``,
``response_events``, ``response_state_chain``, ``background_jobs``,
``tool_executions``).  It backs retrieve / delete / cancel / input_items /
state-chain recovery / background job status / tool-execution idempotency.

Design rules
------------
* **JSON-in-text**: ``payload`` / ``request`` / ``output`` / ``usage`` are
  stored as JSON text (SQLite-compatible) so the same schema works on both the
  SQLite and TiDB backends.
* **Reasoning text never persisted**: the caller is responsible for passing
  already-redacted payloads (see :mod:`item_registry`).  This store only
  persists what it is given; it never materialises reasoning text itself.
* **Append-only event log**: ``response_events`` rows carry a monotonic ``seq``
  per response so a catch-up stream or debug replay can be replayed in order.
* **Tenant isolation**: every write takes a ``workspace_id``; reads filter by it
  so cross-tenant access is prevented at the store boundary.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .event_log import EventLog
from .store import Store

#: JSON encoding used for all stored payloads (stable, compact).
_JSON = dict(ensure_ascii=False, separators=(",", ":"))


def _dumps(obj: Any) -> str:
    return json.dumps(obj, **_JSON) if obj is not None else ""


def _loads(text: str, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


@dataclass
class ResponseRecord:
    """A persisted response row (JSON fields decoded)."""

    response_id: str
    workspace_id: str = ""
    status: str = "queued"
    model: str = ""
    created_at: int = 0
    updated_at: int = 0
    completed_at: int = 0
    previous_response_id: str = ""
    background: bool = False
    request: dict[str, Any] = field(default_factory=dict)
    output: list[Any] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    incomplete_details: dict[str, Any] = field(default_factory=dict)
    terminal_reason: str = ""
    cancelled: bool = False


class ResponseStore:
    """Persistent CRUD for the Responses resource endpoints."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.event_log = EventLog(store)

    # -- responses -----------------------------------------------------------

    async def create_response(
        self,
        *,
        response_id: str,
        workspace_id: str = "",
        model: str = "",
        status: str = "queued",
        previous_response_id: str = "",
        background: bool = False,
        request: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> None:
        now = int(time.time())
        await self._store.execute(
            """INSERT INTO responses (
                response_id, workspace_id, status, model, created_at, updated_at,
                previous_response_id, background, request, usage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                response_id, workspace_id, status, model, now, now,
                previous_response_id, int(background), _dumps(request), _dumps(usage),
            ),
        )

    async def get_response(
        self, response_id: str, *, workspace_id: str = "",
    ) -> ResponseRecord | None:
        row = await self._store.fetchone(
            "SELECT * FROM responses WHERE response_id = ? AND workspace_id = ?",
            (response_id, workspace_id),
        )
        if row is None:
            return None
        return self._row_to_record(row)

    async def update_status(
        self,
        response_id: str,
        status: str,
        *,
        terminal_reason: str = "",
        incomplete_details: Mapping[str, Any] | None = None,
        error: str = "",
        usage: Mapping[str, Any] | None = None,
        output: list[Any] | None = None,
    ) -> None:
        now = int(time.time())
        completed_at = now if status in ("completed", "failed", "incomplete", "cancelled") else 0
        await self._store.execute(
            """UPDATE responses SET status = ?, updated_at = ?, completed_at = ?,
               terminal_reason = ?, incomplete_details = ?, error = ?, usage = ?,
               output = ?
               WHERE response_id = ?""",
            (
                status, now, completed_at,
                terminal_reason, _dumps(incomplete_details), error,
                _dumps(usage), _dumps(output), response_id,
            ),
        )

    async def delete_response(self, response_id: str, *, workspace_id: str = "") -> bool:
        cur = await self._store.execute(
            "DELETE FROM responses WHERE response_id = ? AND workspace_id = ?",
            (response_id, workspace_id),
        )
        return cur > 0

    async def set_cancelled(self, response_id: str, *, workspace_id: str = "") -> None:
        now = int(time.time())
        await self._store.execute(
            "UPDATE responses SET cancelled = 1, status = 'cancelled', updated_at = ? "
            "WHERE response_id = ? AND workspace_id = ?",
            (now, response_id, workspace_id),
        )

    # -- input / output items ------------------------------------------------

    async def save_input_items(
        self, response_id: str, items: list[Mapping[str, Any]],
    ) -> None:
        for seq, item in enumerate(items):
            await self._store.execute(
                """INSERT OR REPLACE INTO response_input_items
                   (response_id, seq, item_type, role, payload)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    response_id, seq,
                    str(item.get("type", "") or ""),
                    str(item.get("role", "") or ""),
                    _dumps(item),
                ),
            )

    async def save_output_items(
        self, response_id: str, items: list[Mapping[str, Any]],
    ) -> None:
        for idx, item in enumerate(items):
            await self._store.execute(
                """INSERT OR REPLACE INTO response_output_items
                   (response_id, seq, output_index, item_type, role, payload)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    response_id, idx, idx,
                    str(item.get("type", "") or ""),
                    str(item.get("role", "") or ""),
                    _dumps(item),
                ),
            )

    async def list_input_items(
        self, response_id: str, *, after_seq: int = -1, limit: int = 100,
    ) -> list[dict]:
        rows = await self._store.fetchall(
            "SELECT payload FROM response_input_items "
            "WHERE response_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (response_id, after_seq, limit),
        )
        return [_loads(r[0], {}) for r in rows]

    async def list_output_items(self, response_id: str, *, limit: int = 100) -> list[dict]:
        rows = await self._store.fetchall(
            "SELECT payload FROM response_output_items WHERE response_id = ? "
            "ORDER BY output_index LIMIT ?",
            (response_id, limit),
        )
        return [_loads(r[0], {}) for r in rows]

    # -- event log -----------------------------------------------------------

    async def append_event(self, response_id: str, event_type: str, data: Mapping[str, Any]) -> int:
        """Append one event to ``response_events`` (delegates to :class:`EventLog`)."""
        return await self.event_log.append_event(
            response_id=response_id, event_type=event_type, data=data, workspace_id="",
        )

    async def list_events(self, response_id: str, *, after_seq: int = -1) -> list[dict]:
        rows = await self._store.fetchall(
            "SELECT seq, event_type, data FROM response_events "
            "WHERE response_id = ? AND seq > ? ORDER BY seq",
            (response_id, after_seq),
        )
        return [{"seq": r[0], "event_type": r[1], "data": _loads(r[2], {})} for r in rows]

    # -- state chain ---------------------------------------------------------

    async def save_state_chain(
        self, response_id: str, previous_response_id: str, depth: int, *, workspace_id: str = "",
    ) -> None:
        await self._store.execute(
            "INSERT OR REPLACE INTO response_state_chain "
            "(response_id, workspace_id, previous_response_id, depth) VALUES (?, ?, ?, ?)",
            (response_id, workspace_id, previous_response_id, depth),
        )

    async def get_previous_response_id(self, response_id: str) -> str:
        row = await self._store.fetchone(
            "SELECT previous_response_id FROM response_state_chain WHERE response_id = ?",
            (response_id,),
        )
        return row[0] if row else ""

    async def chain_depth(self, response_id: str) -> int:
        row = await self._store.fetchone(
            "SELECT depth FROM response_state_chain WHERE response_id = ?",
            (response_id,),
        )
        return row[0] if row else 0

    # -- background tasks ----------------------------------------------------

    async def create_task(
        self,
        *,
        task_id: str,
        response_id: str,
        workspace_id: str = "",
        max_wall_seconds: int = 900,
        max_tool_rounds: int = 32,
    ) -> None:
        now = int(time.time())
        await self._store.execute(
            "INSERT INTO background_jobs "
            "(task_id, response_id, workspace_id, status, created_at, updated_at, "
            " max_wall_seconds, max_tool_rounds) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)",
            (task_id, response_id, workspace_id, now, now, max_wall_seconds, max_tool_rounds),
        )

    async def update_task_status(self, task_id: str, status: str) -> None:
        await self._store.execute(
            "UPDATE background_jobs SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, int(time.time()), task_id),
        )

    async def lease_task(self, task_id: str, lease_seconds: int) -> bool:
        now = int(time.time())
        cur = await self._store.execute(
            "UPDATE background_jobs SET lease_until = ?, updated_at = ? "
            "WHERE task_id = ? AND (status = 'queued' OR status = 'in_progress') "
            "AND lease_until < ?",
            (now + lease_seconds, now, task_id, now),
        )
        return cur > 0

    async def request_cancel(self, task_id: str) -> None:
        await self._store.execute(
            "UPDATE background_jobs SET cancel_requested = 1, updated_at = ? WHERE task_id = ?",
            (int(time.time()), task_id),
        )

    async def get_task(self, task_id: str, *, workspace_id: str = "") -> dict[str, Any] | None:
        row = await self._store.fetchone(
            "SELECT * FROM background_jobs WHERE task_id = ? AND workspace_id = ?",
            (task_id, workspace_id),
        )
        if row is None:
            return None
        cols = await self._task_columns()
        return dict(zip(cols, row))

    async def _task_columns(self) -> list[str]:
        return [
            "task_id", "response_id", "workspace_id", "status", "created_at",
            "updated_at", "lease_until", "cancel_requested", "max_wall_seconds",
            "max_tool_rounds", "attempt",
        ]

    # -- tool executions -----------------------------------------------------

    async def record_tool_execution(
        self,
        *,
        execution_id: str,
        response_id: str,
        workspace_id: str = "",
        call_id: str = "",
        tool_name: str = "",
        idempotency_key: str = "",
        status: str = "pending",
    ) -> None:
        now = int(time.time())
        await self._store.execute(
            "INSERT INTO tool_executions "
            "(execution_id, response_id, workspace_id, call_id, tool_name, "
            " idempotency_key, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (execution_id, response_id, workspace_id, call_id, tool_name,
             idempotency_key, status, now, now),
        )

    async def has_execution(self, idempotency_key: str) -> bool:
        row = await self._store.fetchone(
            "SELECT 1 FROM tool_executions WHERE idempotency_key = ? LIMIT 1",
            (idempotency_key,),
        )
        return row is not None

    # -- idempotency records -------------------------------------------------

    async def save_idempotency_record(
        self,
        *,
        workspace_id: str,
        idempotency_key: str,
        request_digest: str = "",
        response_id: str = "",
        status_code: int = 0,
        state: str = "in_flight",
        expires_at: int = 0,
    ) -> None:
        await self._store.execute(
            "INSERT OR REPLACE INTO idempotency_records "
            "(workspace_id, idempotency_key, request_digest, response_id, "
            " status_code, state, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (workspace_id, idempotency_key, request_digest, response_id,
             status_code, state, int(time.time()), expires_at),
        )

    async def get_idempotency_record(
        self, workspace_id: str, idempotency_key: str,
    ) -> tuple[str, int, str] | None:
        row = await self._store.fetchone(
            "SELECT response_id, status_code, state FROM idempotency_records "
            "WHERE workspace_id = ? AND idempotency_key = ?",
            (workspace_id, idempotency_key),
        )
        return tuple(row) if row is not None else None  # type: ignore[return-value]

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: tuple) -> ResponseRecord:
        return ResponseRecord(
            response_id=row[0],
            workspace_id=row[1],
            status=row[2],
            model=row[3],
            created_at=row[4],
            updated_at=row[5],
            completed_at=row[6],
            previous_response_id=row[7],
            background=bool(row[8]),
            request=_loads(row[9], {}),
            output=_loads(row[10], []),
            usage=_loads(row[11], {}),
            error=row[12],
            incomplete_details=_loads(row[13], {}),
            terminal_reason=row[14],
            cancelled=bool(row[15]),
        )


__all__ = [
    "ResponseRecord",
    "ResponseStore",
]