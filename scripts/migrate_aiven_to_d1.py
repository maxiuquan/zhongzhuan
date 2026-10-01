# -*- coding: utf-8 -*-
"""Aiven MySQL → Cloudflare D1 一次性数据迁移（在 VPS 上执行）。

用法（VPS，/root/zhongzhuan 目录，env 已含 Aiven + D1 凭据）：
    cd /root/zhongzhuan
    set -a; source .env; set +a
    PYTHONPATH=src python scripts/migrate_aiven_to_d1.py [--write]

* 默认 dry-run：只导出计数 + 逐表试写 0 行，不落数据。
* --write 才真正灌数据。表间按外键依赖顺序导入（models → api_keys …）。
* 不迁移 schema_migrations（D1 侧已由 migration engine 建好 sqlite digest 版本）
  与 sqlite_sequence（AUTOINCREMENT 计数器按导入行自动推进）。
* 幂等：导入前 DELETE 目标表全部行（--write 时），重跑安全。

值序列化：MySQL 行 → SQLite 字面量
* None → NULL；int/float → repr；str → '…'（单引号翻倍转义）；
* bytes（key_cipher/token_cipher VARBINARY/BLOB）→ X'hex'；
* 多语句批量：每请求 ≤50 条 INSERT（/raw 支持多语句，实测探针确认）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import aiomysql

from zhongzhuan.store.d1_store import D1Store

#: 导入顺序（外键依赖：被引用表在前）。
TABLES: tuple[str, ...] = (
    "models",
    "model_groups",
    "admin_users",
    "api_keys",
    "group_models",
    "access_tokens",
    "system_config",
    "key_health",
    "model_pricing",
    "route_bindings",
    "background_jobs",
    "idempotency_records",
    "request_logs",
    "tool_executions",
    "responses",
    "response_input_items",
    "response_output_items",
    "response_events",
    "response_state_chain",
)

#: 每个批量请求携带的最大 INSERT 语句数（D1 单请求 100KB 上限，保守取值）。
BATCH_STATEMENTS: int = 50

SKIP_TABLES = {"schema_migrations", "sqlite_sequence", "_cf_KV"}


def quote_value(v) -> str:
    """MySQL 值 → SQLite SQL 字面量。"""
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "X'" + bytes(v).hex() + "'"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    text = str(v)
    return "'" + text.replace("'", "''") + "'"


def insert_statement(table: str, columns: list[str], row: tuple) -> str:
    cols = ", ".join(f'"{c}"' for c in columns)
    vals = ", ".join(quote_value(v) for v in row)
    return f'INSERT INTO "{table}" ({cols}) VALUES ({vals})'


async def fetch_mysql_tables(mysql_params) -> dict[str, tuple[list[str], list[tuple]]]:
    """从 Aiven 逐表导出 {table: (columns, rows)}，跳过 SKIP_TABLES。"""
    out: dict[str, tuple[list[str], list[tuple]]] = {}
    conn = await aiomysql.connect(**mysql_params)
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name"
            )
            all_tables = [r[0] for r in await cur.fetchall()]
            for table in all_tables:
                if table in SKIP_TABLES:
                    continue
                await cur.execute(f"SELECT * FROM `{table}`")
                rows = list(await cur.fetchall())
                cols = [d[0] for d in cur.description]
                out[table] = (cols, rows)
    finally:
        conn.close()
    return out


async def import_table(store: D1Store, table: str, columns: list[str], rows: list[tuple]) -> int:
    """清空目标表后批量灌入，返回写入行数。"""
    await store.execute(f'DELETE FROM "{table}"')
    written = 0
    for i in range(0, len(rows), BATCH_STATEMENTS):
        chunk = rows[i : i + BATCH_STATEMENTS]
        stmts = [insert_statement(table, columns, r) for r in chunk]
        # /raw 多语句：分号连接（探针确认依序执行、结果按语句展开）
        await store.execute_script("; ".join(stmts))
        written += len(chunk)
    return written


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="真正写数据（默认 dry-run）")
    args = parser.parse_args()

    mysql_params = dict(
        host=os.environ["ZHONGZHUAN_TIDB_HOST"],
        port=int(os.environ.get("ZHONGZHUAN_TIDB_PORT", "3306")),
        user=os.environ["ZHONGZHUAN_TIDB_USER"],
        password=os.environ["ZHONGZHUAN_TIDB_PASSWORD"],
        db=os.environ.get("ZHONGZHUAN_TIDB_DATABASE", "zhongzhuan"),
    )
    store = await D1Store.create(
        account_id=os.environ["ZHONGZHUAN_D1_ACCOUNT_ID"],
        database_id=os.environ["ZHONGZHUAN_D1_DATABASE_ID"],
        api_token=os.environ["ZHONGZHUAN_D1_API_TOKEN"],
    )

    data = await fetch_mysql_tables(mysql_params)
    print(f"exported {len(data)} tables from MySQL")
    total_src, total_dst = 0, 0
    for table in TABLES:
        if table not in data:
            print(f"  {table:<24} MISSING in source (skip)")
            continue
        cols, rows = data[table]
        if args.write:
            # D1 侧实际列可能与导出顺序不同 → 按列名对齐，缺列报错
            d1_cols = {r[1] for r in await store.fetchall(f'PRAGMA table_info("{table}")')}
            missing = set(cols) - d1_cols
            if missing:
                raise RuntimeError(f"{table}: columns not in D1: {sorted(missing)}")
            written = await import_table(store, table, cols, rows)
        else:
            written = 0
        total_src += len(rows)
        total_dst += written
        mode = "WRITE" if args.write else "DRY  "
        print(f"  {table:<24} src={len(rows):>6}  dst={written:>6}  [{mode}]")

    extra = set(data) - set(TABLES)
    if extra:
        print(f"  WARNING: source tables not in import list: {sorted(extra)}")

    if args.write:
        # 行数核对
        mismatch = []
        for table in TABLES:
            if table not in data:
                continue
            n = (await store.fetchone(f'SELECT count(*) FROM "{table}"'))[0]
            if n != len(data[table][1]):
                mismatch.append((table, len(data[table][1]), n))
            total_dst = total_dst  # noqa: PLW0127
        if mismatch:
            raise SystemExit(f"ROW COUNT MISMATCH: {mismatch}")
        print("row counts verified OK")

    print(f"TOTAL: src={total_src} dst={total_dst}")
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
