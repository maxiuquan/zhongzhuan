# -*- coding: utf-8 -*-
"""D1 REST API behavior probe - one-off, defines D1Store design."""
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


def call(path, data=None, method=None):
    url = BASE + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        r = urllib.request.urlopen(req, timeout=30)
        ms = (time.perf_counter() - t0) * 1000
        return r.status, json.loads(r.read().decode()), ms
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        b = getattr(e, "read", lambda: b"")()
        return "ERR", b.decode() if b else str(e), ms


def show(label, res):
    s, b, ms = res
    print(f"== {label}  [{s}, {ms:.0f}ms]")
    print(json.dumps(b, ensure_ascii=False)[:900])
    print()


# 1. BLOB round-trip: insert via X'' literal, see how SELECT returns it
show("create probe table", call("/query", {"sql": "CREATE TABLE IF NOT EXISTS _probe1 (id INTEGER PRIMARY KEY, b BLOB, t TEXT)"}))
show("insert blob literal", call("/query", {"sql": "INSERT INTO _probe1 (b, t) VALUES (X'00FF10A5', 'hello')"}))
show("select blob", call("/query", {"sql": "SELECT id, b, t FROM _probe1 WHERE id=1"}))

# 2. type() function to get typeof
show("typeof blob", call("/query", {"sql": "SELECT typeof(b), hex(b), length(b) FROM _probe1 WHERE id=1"}))

# 3. param binding with special values
show("param types", call("/query", {"sql": "SELECT ?1 AS a, ?2 AS b, ?3 AS c, ?4 AS d", "params": [42, "txt", None, 1.5]}))

# 4. lastrowid + changes for writes
show("insert for meta", call("/query", {"sql": "INSERT INTO _probe1 (t) VALUES ('meta')"}))

# 5. multi-statement in one sql string?
show("multi statement", call("/query", {"sql": "INSERT INTO _probe1 (t) VALUES ('m1'); INSERT INTO _probe1 (t) VALUES ('m2');"}))

# 6. INSERT OR REPLACE (sqlite upsert flavor)
show("insert or replace", call("/query", {"sql": "INSERT OR REPLACE INTO _probe1 (id, t) VALUES (1, 'replaced')"}))
show("check replace", call("/query", {"sql": "SELECT id, t, b FROM _probe1 WHERE id=1"}))

# 7. named/positional ? params in write + RETURNING
show("returning", call("/query", {"sql": "INSERT INTO _probe1 (t) VALUES (?) RETURNING id, t", "params": ["ret"]}))

# 8. row order of dict keys in response (does it follow SELECT column order?)
show("key order", call("/query", {"sql": "SELECT t, id, b FROM _probe1 WHERE id=1"}))

# 9. duplicate column names across tables (JOIN collapse risk)
show("dup columns", call("/query", {"sql": "SELECT a.id, b.id FROM _probe1 a JOIN _probe1 b ON a.id=b.id LIMIT 1"}))

# 10. concurrency / transaction: BEGIN via REST?
show("begin via rest", call("/query", {"sql": "BEGIN"}))
show("commit via rest", call("/query", {"sql": "COMMIT"}))

print("== cleanup")
show("drop probe", call("/query", {"sql": "DROP TABLE IF EXISTS _probe1"}))
