"""Request logs + stats."""
from __future__ import annotations

import uuid

from .store import Store


def log_request(
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
    s.connect().execute(
        """INSERT INTO request_logs(ts, client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, request_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (Store.now(), client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, rid),
    )


def list_logs(
    s: Store,
    cursor: int = 0,
    limit: int = 50,
    model: str | None = None,
    status: int | None = None,
) -> dict:
    conn = s.connect()
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
    rows = conn.execute(sql, params).fetchall()
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


def get_stats(s: Store, range_hours: int = 1) -> dict:
    """Get QPS, success rate, top errors."""
    since = Store.now() - range_hours * 3600
    conn = s.connect()
    total = conn.execute("SELECT COUNT(*) FROM request_logs WHERE ts>=?", (since,)).fetchone()[0]
    success = conn.execute("SELECT COUNT(*) FROM request_logs WHERE ts>=? AND status>=200 AND status<300", (since,)).fetchone()[0]
    errors = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM request_logs WHERE ts>=? AND status>=400 GROUP BY status ORDER BY cnt DESC LIMIT 5",
        (since,),
    ).fetchall()

    avg_latency_row = conn.execute(
        "SELECT AVG(latency_ms) FROM request_logs WHERE ts>=?", (since,),
    ).fetchone()
    avg_latency = avg_latency_row[0] or 0

    active_keys = conn.execute(
        "SELECT COUNT(DISTINCT key_id) FROM request_logs WHERE ts>=?", (since,),
    ).fetchone()[0]

    return {
        "qps": round(total / (range_hours * 3600), 2) if total else 0,
        "total_requests": total,
        "success_rate": round(success / total, 4) if total else 1.0,
        "avg_latency_ms": round(avg_latency, 1),
        "active_keys": active_keys,
        "top_errors": [{"status": e[0], "count": e[1]} for e in errors],
    }


def cleanup_old_logs(s: Store, retention_days: int = 14) -> None:
    cutoff = Store.now() - retention_days * 86400
    s.connect().execute("DELETE FROM request_logs WHERE ts<?", (cutoff,))