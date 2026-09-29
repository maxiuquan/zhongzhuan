#!/usr/bin/env python3
"""一次性迁移脚本：TiDB Cloud → Aiven MySQL。

用法（在 VPS 上执行）：
    python3 _migrate_tidb_to_aiven.py

前提：
  - /root/zhongzhuan/.env 里 ZHONGZHUAN_TIDB_* 仍指向旧 TiDB（读源）
  - 环境变量 AIVEN_HOST / AIVEN_PORT / AIVEN_PASSWORD 覆盖式传入目标
    （目标 database 固定 zhongzhuan，user 固定 avnadmin）
  - 目标 schema 由 zhongzhuan 自带 migration engine 预先建好
    （本脚本第 1 步通过 zhongzhuan.store.tidb_store.TiDBStore.create 触发）

行为：
  1. 用 zhongzhuan 的 TiDBStore 对 Aiven 建池 → 跑 v1..v17 全部 migration
  2. SHOW TABLES 枚举 TiDB 侧全部表（跳过 schema_version，由引擎写入）
  3. 逐表 SELECT * → 分批 REPLACE INTO Aiven（列名/类型自适应）
  4. 逐表打印行数对照，任何一张表行数不一致 → exit 1

只做插入、不改写源端；密文列（key_cipher 等）按原字节搬运，
AES key 在 system_config 里原样迁移，crypto 无感。
"""
import asyncio
import os
import sys

import pymysql

SRC = dict(
    host=os.environ["ZHONGZHUAN_TIDB_HOST"],
    port=int(os.environ.get("ZHONGZHUAN_TIDB_PORT", "4000")),
    user=os.environ["ZHONGZHUAN_TIDB_USER"],
    password=os.environ["ZHONGZHUAN_TIDB_PASSWORD"],
    database=os.environ.get("ZHONGZHUAN_TIDB_DATABASE", "apidaili"),
)
DST = dict(
    host=os.environ["AIVEN_HOST"],
    port=int(os.environ.get("AIVEN_PORT", "26357")),
    user=os.environ.get("AIVEN_USER", "avnadmin"),
    password=os.environ["AIVEN_PASSWORD"],
    database=os.environ.get("AIVEN_DATABASE", "zhongzhuan"),
)
SKIP_TABLES = {"schema_version"}
BATCH = 200


def src_ssl():
    return {"ssl": {"check_hostname": False}}


def dst_ssl():
    ca = os.environ.get("AIVEN_CA", "").strip()
    if ca:
        import ssl as _ssl

        ctx = _ssl.create_default_context()
        ctx.load_verify_locations(cadata=ca)
        return ctx
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    return ctx


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def run_migrations_on_dst() -> None:
    """用 zhongzhuan 自带的 migration engine 在目标库建 schema（幂等）。"""
    sys.path.insert(0, "/root/zhongzhuan/src")
    os.environ.setdefault("ZHONGZHUAN_TIDB_HOST", DST["host"])
    os.environ["ZHONGZHUAN_TIDB_HOST"] = DST["host"]
    os.environ["ZHONGZHUAN_TIDB_PORT"] = str(DST["port"])
    os.environ["ZHONGZHUAN_TIDB_USER"] = DST["user"]
    os.environ["ZHONGZHUAN_TIDB_PASSWORD"] = DST["password"]
    os.environ["ZHONGZHUAN_TIDB_DATABASE"] = DST["database"]
    # Aiven 用系统 CA 校验会失败（私有 CA）——迁移期间用不校验 TLS。
    os.environ["ZHONGZHUAN_TIDB_SSL_VERIFY"] = "false"

    from zhongzhuan.store.tidb_store import TiDBStore

    async def _run():
        store = await TiDBStore.create(
            host=DST["host"],
            port=DST["port"],
            user=DST["user"],
            password=DST["password"],
            database=DST["database"],
            ssl=True,
            ssl_verify=False,
            pool_size=2,
        )
        store._closed = True  # 建完 schema 即关，防止 reaper 起来

    asyncio.run(_run())
    print("[1/3] schema migration on Aiven: OK")


def copy_table(src_cur, dst_conn, table: str) -> tuple[int, int]:
    src_cur.execute(f"SELECT * FROM {quote_ident(table)}")
    rows = src_cur.fetchall()
    if not rows:
        return 0, 0
    cols = [d[0] for d in src_cur.description]
    col_list = ", ".join(quote_ident(c) for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"REPLACE INTO {quote_ident(table)} ({col_list}) VALUES ({placeholders})"
    cur = dst_conn.cursor()
    for i in range(0, len(rows), BATCH):
        cur.executemany(sql, rows[i : i + BATCH])
    dst_conn.commit()
    return len(rows), cur.rowcount


def main() -> int:
    run_migrations_on_dst()

    src_conn = pymysql.connect(**SRC, ssl=src_ssl(), connect_timeout=15, charset="utf8mb4")
    dst_conn = pymysql.connect(**DST, ssl=dst_ssl(), connect_timeout=15, charset="utf8mb4")
    src_cur = src_conn.cursor()
    src_cur.execute("SHOW TABLES")
    tables = [r[0] for r in src_cur.fetchall() if r[0] not in SKIP_TABLES]
    print(f"[2/3] copying {len(tables)} tables: {', '.join(tables)}")

    dst_cur = dst_conn.cursor()
    failures = []
    for t in tables:
        try:
            n_src, _ = copy_table(src_cur, dst_conn, t)
            dst_cur.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}")
            n_dst = dst_cur.fetchone()[0]
            status = "OK" if n_dst >= n_src else "MISMATCH"
            print(f"    {t}: src={n_src} dst={n_dst} {status}")
            if n_dst < n_src:
                failures.append(t)
        except Exception as exc:  # noqa: BLE001
            failures.append(t)
            print(f"    {t}: ERROR {type(exc).__name__}: {exc}")
    print("[3/3] done")
    if failures:
        print(f"FAILED tables: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
