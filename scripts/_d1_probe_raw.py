# -*- coding: utf-8 -*-
"""Probe /raw endpoint: does it return positional array rows?"""
import json
import os
import time
import urllib.request

TOK = os.environ.get("ZHONGZHUAN_D1_API_TOKEN", "")
ACCT = os.environ.get("ZHONGZHUAN_D1_ACCOUNT_ID", "")
DB = os.environ.get("ZHONGZHUAN_D1_DATABASE_ID", "")
if not (TOK and ACCT and DB):
    raise SystemExit("set ZHONGZHUAN_D1_* env vars")
BASE = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/d1/database/{DB}"


def call(path, data=None):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read().decode()), (time.perf_counter() - t0) * 1000
    except Exception as e:
        b = getattr(e, "read", lambda: b"")()
        return "ERR", b.decode() if b else str(e), (time.perf_counter() - t0) * 1000


s, b, ms = call("/query", {"sql": "CREATE TABLE IF NOT EXISTS _p2 (id INTEGER PRIMARY KEY, v TEXT)"})
print("create", s, ms)
s, b, ms = call("/query", {"sql": "INSERT OR IGNORE INTO _p2 (id, v) VALUES (1, 'a'), (2, 'b')"})
print("insert", s, ms)

# /raw with array_rows option?
s, b, ms = call("/raw", {"sql": "SELECT a.id, b.id, a.v FROM _p2 a JOIN _p2 b ON a.id=b.id"})
print("== raw join", s, f"{ms:.0f}ms")
print(json.dumps(b, ensure_ascii=False)[:1200])

s, b, ms = call("/raw", {"sql": "SELECT id, v FROM _p2 WHERE id=?", "params": [1]})
print("== raw params", s, f"{ms:.0f}ms")
print(json.dumps(b, ensure_ascii=False)[:1200])

call("/query", {"sql": "DROP TABLE IF EXISTS _p2"})
print("cleaned")
