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
    inbound_protocol: str = "",
    outbound_protocol: str = "",
    translated: bool = False,
    token_id: int = 0,
    cost: float = 0.0,
) -> None:
    rid = request_id or str(uuid.uuid4())
    await s.execute(
        """INSERT INTO request_logs(ts, client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, request_id, inbound_protocol, outbound_protocol, translated, token_id, cost)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            Store.now(),
            client_ip,
            model_name,
            resolved_model_id,
            key_id,
            status,
            latency_ms,
            tokens_in,
            tokens_out,
            error,
            rid,
            inbound_protocol,
            outbound_protocol,
            int(translated),
            token_id,
            cost,
        ),
    )


async def list_logs(
    s: Store,
    cursor: int = 0,
    limit: int = 50,
    model: str | None = None,
    status: int | None = None,
) -> dict:
    sql = "SELECT id, ts, client_ip, model_name, resolved_model_id, key_id, status, latency_ms, tokens_in, tokens_out, error, request_id, inbound_protocol, outbound_protocol, translated FROM request_logs WHERE id > ?"
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
                "id": r[0],
                "ts": r[1],
                "client_ip": r[2],
                "model_name": r[3],
                "resolved_model_id": r[4],
                "key_id": r[5],
                "status": r[6],
                "latency_ms": r[7],
                "tokens_in": r[8],
                "tokens_out": r[9],
                "error": r[10],
                "request_id": r[11],
                "inbound_protocol": r[12] if len(r) > 12 else "",
                "outbound_protocol": r[13] if len(r) > 13 else "",
                "translated": bool(r[14]) if len(r) > 14 else False,
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
    success_row = await s.fetchone(
        "SELECT COUNT(*) FROM request_logs WHERE ts>=? AND status>=200 AND status<300", (since,)
    )
    success = success_row[0] if success_row else 0
    errors = await s.fetchall(
        "SELECT status, COUNT(*) as cnt FROM request_logs WHERE ts>=? AND status>=400 GROUP BY status ORDER BY cnt DESC LIMIT 5",
        (since,),
    )

    avg_row = await s.fetchone(
        "SELECT AVG(latency_ms) FROM request_logs WHERE ts>=?",
        (since,),
    )
    avg_latency = avg_row[0] or 0 if avg_row else 0

    active_row = await s.fetchone(
        "SELECT COUNT(DISTINCT key_id) FROM request_logs WHERE ts>=?",
        (since,),
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


async def cleanup_old_logs(s: Store, retention_days: int = 14) -> int:
    """Delete ``request_logs`` rows older than *retention_days* (T06).

    Delegates to :func:`zhongzhuan.store.retention.cleanup_old_logs` so the
    TTL logic and the scheduler live in one place.  Returns the number of rows
    deleted.
    """
    from .retention import cleanup_old_logs as _cleanup

    return await _cleanup(s, retention_days)


async def get_usage_stats(s: Store, days: int = 7) -> dict:
    """获取近 N 天的用量统计（按天聚合）。

    返回:
    {
        "daily": [{"date": "2026-07-23", "requests": 100, "tokens_in": 5000, "tokens_out": 3000, "cost": 1.23}],
        "by_model": [{"model_name": "gpt-4o", "requests": 50, "tokens_in": 2000, "tokens_out": 1500, "cost": 0.5}],
        "totals": {"requests": 700, "tokens_in": 35000, "tokens_out": 21000, "cost": 8.61}
    }
    """
    since = Store.now() - days * 86400

    # 按天聚合。显式取整，避免普通除法在 TiDB 返回 Decimal，且确保同一天
    # 的时间戳确实落在同一个 UTC 日桶中。
    day_bucket = (
        "CAST(FLOOR(ts / 86400) * 86400 AS SIGNED)"
        if s.dialect == "mysql"
        else "CAST(ts / 86400 AS INTEGER) * 86400"
    )
    daily_rows = await s.fetchall(
        f"SELECT {day_bucket} AS day, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost) "
        "FROM request_logs WHERE ts>=? AND status>=200 AND status<300 "
        f"GROUP BY {day_bucket} ORDER BY {day_bucket}",
        (since,),
    )
    import datetime

    daily = []
    for r in daily_rows:
        day_ts = int(r[0] if r[0] else since)
        date_str = datetime.datetime.fromtimestamp(day_ts, datetime.UTC).strftime("%Y-%m-%d")
        daily.append(
            {
                "date": date_str,
                "requests": int(r[1] or 0),
                "tokens_in": int(r[2] or 0),
                "tokens_out": int(r[3] or 0),
                "cost": round(float(r[4] or 0), 4),
            }
        )

    # 按模型聚合。仪表盘只展示已配置模型的切片：聚合 SQL 保持方言中立，
    # 在 Python 侧剔除“未解析到任何已配置模型”的探测/脏请求（如部署自检
    # 直接写入的 model_name="x"）。已配置模型按 name + aliases 判定，避免
    # 误伤正常别名流量（mf / agnes 等）。
    model_rows = await s.fetchall(
        "SELECT model_name, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost) "
        "FROM request_logs WHERE ts>=? AND status>=200 AND status<300 "
        "GROUP BY model_name ORDER BY COUNT(*) DESC LIMIT 20",
        (since,),
    )
    configured_names: set[str] = set()
    try:
        from .models import list_models

        for m in await list_models(s):
            configured_names.add(m.name)
            if m.aliases:
                configured_names.update(a.strip() for a in m.aliases.split(",") if a.strip())
    except Exception:
        # 模型表不可用时不丢弃数据，仅保留 SQL 聚合结果。
        pass
    by_model = [
        {
            "model_name": r[0] or "unknown",
            "requests": int(r[1] or 0),
            "tokens_in": int(r[2] or 0),
            "tokens_out": int(r[3] or 0),
            "cost": round(float(r[4] or 0), 4),
        }
        for r in model_rows
        if not configured_names or (r[0] or "") in configured_names
    ]

    # 总计
    total_row = await s.fetchone(
        "SELECT COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost) "
        "FROM request_logs WHERE ts>=? AND status>=200 AND status<300",
        (since,),
    )
    totals = {
        "requests": int(total_row[0] if total_row else 0),
        "tokens_in": int((total_row[1] if total_row else 0) or 0),
        "tokens_out": int((total_row[2] if total_row else 0) or 0),
        "cost": round(float((total_row[3] if total_row else 0) or 0), 4),
    }

    return {"daily": daily, "by_model": by_model, "totals": totals, "days": days}
