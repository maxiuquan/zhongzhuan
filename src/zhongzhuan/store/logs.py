"""Request logs + stats (async)."""
from __future__ import annotations

import uuid

from .store import Store


async def log_request(
    s: Store,
    *,
    client_ip: str = "",
    model_name: str = "",
    resolved_model_id: int | None = None,
    key_id: int | None = None,
    status: int = 0,
    latency_ms: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    error: str = "",
    request_id: str | None = None,
) -> None:
    rid = request_id or str(uuid.uuid4())
    await s.execute(
        """INSERT INTO request_logs(ts, client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, request_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (Store.now(), client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, rid),
    )


async def list_logs(
    s: Store,
    cursor: int = 0,
    limit: int = 50,
    model: str | None = None,
    status: int | None = None,
) -> dict:
    sql = "SELECT id, ts, client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, request_id FROM request_logs WHERE id > ?"
    params: list = [cursor]
    if model:
        sql += " AND model_name=?"
        params.append(model)
    if status is not None:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = await s.fetchall(sql, tuple(params))
    return {
        "data": [
            {
                "id": r[0], "ts": r[1], "client_ip": r[2], "model_name": r[3],
                "resolved_model_id": r[4], "key_id": r[5], "status": r[6],
                "latency_ms": r[7], "tokens_in": r[8], "tokens_out": r[9],
                "error": r[10], "request_id": r[11],
            }
            for r in rows
        ],
        "next_cursor": rows[-1][0] if rows else cursor,
    }


async def get_stats(s: Store, range_hours: int = 1) -> dict:
    """Get QPS, success rate, top errors."""
    since = Store.now() - range_hours * 3600
    total_row = await s.fetchone("SELECT COUNT(*) FROM request_logs WHERE ts>=?", (since,))
    total = total_row[0] if total_row else 0
    success_row = await s.fetchone("SELECT COUNT(*) FROM request_logs WHERE ts>=? AND status>=200 AND status<300", (since,))
    success = success_row[0] if success_row else 0
    errors = await s.fetchall(
        "SELECT status, COUNT(*) as cnt FROM request_logs WHERE ts>=? AND status>=400 GROUP BY status ORDER BY cnt DESC LIMIT 5",
        (since,),
    )

    avg_row = await s.fetchone(
        "SELECT AVG(latency_ms) FROM request_logs WHERE ts>=?", (since,),
    )
    avg_latency = avg_row[0] or 0 if avg_row else 0

    active_row = await s.fetchone(
        "SELECT COUNT(DISTINCT key_id) FROM request_logs WHERE ts>=?", (since,),
    )
    active_keys = active_row[0] if active_row else 0

    return {
        "qps": round(total / (range_hours * 3600), 2) if total else 0,
        "total_requests": total,
        "success_rate": round(success / total, 4) if total else 1.0,
        "avg_latency_ms": round(avg_latency, 1),
        "active_keys": active_keys,
        "top_errors": [{"status": e[0], "count": e[1]} for e in errors],
    }


async def cleanup_old_logs(s: Store, retention_days: int = 14) -> None:
    cutoff = Store.now() - retention_days * 86400
    await s.execute("DELETE FROM request_logs WHERE ts<?", (cutoff,))