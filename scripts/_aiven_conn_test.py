#!/usr/bin/env python3
"""Aiven 连通性测试：unverified TLS vs 系统 CA 校验。"""
import os
import ssl

import pymysql

HOST = "zhongzhuan-maxiuquan1.c.aivencloud.com"
PORT = 26357
USER = "avnadmin"
PWD = os.environ.get("AIVEN_PASSWORD", "")
DB = "zhongzhuan"


def make_ctx(unverified: bool):
    ctx = ssl.create_default_context()
    if unverified:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


for mode, unverified in (("unverified-tls", True), ("system-ca", False)):
    try:
        conn = pymysql.connect(
            host=HOST, port=PORT, user=USER, password=PWD, database=DB,
            connect_timeout=15, ssl=make_ctx(unverified), charset="utf8mb4",
        )
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        print(f"{mode}: OK version={version} tables={len(tables)} {tables[:5]}")
        conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"{mode}: FAIL {type(e).__name__}: {str(e)[:150]}")
