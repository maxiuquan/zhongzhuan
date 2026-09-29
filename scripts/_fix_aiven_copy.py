#!/usr/bin/env python3
"""修复迁移：按依赖序（models → api_keys → group_models）逐行 REPLACE，
逐行捕获错误定位坏行；最后全表行数核对。"""
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


def src_ssl():
    return {"ssl": {"check_hostname": False}}


def dst_ssl():
    import ssl

    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile="/root/aiven-ca.pem")
    return ctx


def qi(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def main() -> int:
    src = pymysql.connect(**SRC, ssl=src_ssl(), connect_timeout=15, charset="utf8mb4")
    dst = pymysql.connect(**DST, ssl=dst_ssl(), connect_timeout=15, charset="utf8mb4")
    src_cur = src.cursor()
    dst_cur = dst.cursor()

    for table in ("models", "api_keys", "group_models"):
        src_cur.execute(f"SELECT * FROM {qi(table)}")
        rows = src_cur.fetchall()
        cols = [d[0] for d in src_cur.description]
        col_list = ", ".join(qi(c) for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        sql = f"REPLACE INTO {qi(table)} ({col_list}) VALUES ({placeholders})"
        bad = 0
        for row in rows:
            try:
                dst_cur.execute(sql, row)
            except Exception as exc:  # noqa: BLE001
                bad += 1
                print(f"[{table}] ROW ERROR: {type(exc).__name__}: {str(exc)[:160]}")
                print(f"    row keys: {dict(zip(cols, [str(v)[:60] for v in row]))}")
        dst.commit()
        dst_cur.execute(f"SELECT COUNT(*) FROM {qi(table)}")
        n_dst = dst_cur.fetchone()[0]
        print(f"[{table}] src={len(rows)} dst={n_dst} bad={bad} {'OK' if bad == 0 and n_dst >= len(rows) else 'FAIL'}")

    # 最终全表核对
    src_cur.execute("SHOW TABLES")
    tables = [r[0] for r in src_cur.fetchall() if r[0] != "schema_version"]
    failures = []
    for t in tables:
        src_cur.execute(f"SELECT COUNT(*) FROM {qi(t)}")
        n_src = src_cur.fetchone()[0]
        dst_cur.execute(f"SELECT COUNT(*) FROM {qi(t)}")
        n_dst = dst_cur.fetchone()[0]
        flag = "OK" if n_dst >= n_src else "MISMATCH"
        if flag != "OK":
            failures.append(t)
        print(f"    {t}: src={n_src} dst={n_dst} {flag}")
    print("FAILED:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
