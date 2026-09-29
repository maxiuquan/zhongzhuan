#!/usr/bin/env python3
"""探针：定位 models 唯一键冲突行 + 全部孤儿引用行（只读）。"""
import os

import pymysql

SRC = dict(
    host=os.environ["ZHONGZHUAN_TIDB_HOST"],
    port=int(os.environ.get("ZHONGZHUAN_TIDB_PORT", "4000")),
    user=os.environ["ZHONGZHUAN_TIDB_USER"],
    password=os.environ["ZHONGZHUAN_TIDB_PASSWORD"],
    database=os.environ.get("ZHONGZHUAN_TIDB_DATABASE", "apidaili"),
)

conn = pymysql.connect(**SRC, ssl={"ssl": {"check_hostname": False}}, connect_timeout=15, charset="utf8mb4")
cur = conn.cursor()

print("== 1. models 中 id=180065 是否存在 ==")
cur.execute("SELECT id, name, enabled, is_fallback FROM models WHERE id=180065")
print("   ", cur.fetchall())

print("== 2. models 重名行（唯一键冲突候选）==")
cur.execute("SELECT name, COUNT(*) c, GROUP_CONCAT(id) ids FROM models GROUP BY name HAVING c>1")
for r in cur.fetchall():
    print("   ", r)

print("== 3. models 全部唯一索引 ==")
cur.execute("SHOW INDEX FROM models")
seen = {}
for r in cur.fetchall():
    if r[2] not in seen:
        seen[r[2]] = []
    seen[r[2]].append(r[4])
for k, v in seen.items():
    print(f"    {k}: {v}")

print("== 4. api_keys 引用的 model_id 全景（找孤儿）==")
cur.execute("SELECT model_id, COUNT(*) c FROM api_keys GROUP BY model_id ORDER BY model_id")
model_ids = set()
cur2 = conn.cursor()
cur2.execute("SELECT id FROM models")
for r in cur2.fetchall():
    model_ids.add(r[0])
for mid, c in cur.fetchall():
    if mid not in model_ids:
        print(f"    ORPHAN model_id={mid} rows={c}")

print("== 5. group_models 孤儿（含 group 与 model 双向）==")
cur.execute("SELECT group_id, COUNT(*) c FROM group_models GROUP BY group_id ORDER BY group_id")
group_ids = set()
cur2.execute("SELECT id FROM model_groups")
for r in cur2.fetchall():
    group_ids.add(r[0])
for gid, c in cur.fetchall():
    if gid not in group_ids:
        print(f"    ORPHAN group_id={gid} rows={c}")
cur.execute("SELECT model_id, COUNT(*) c FROM group_models GROUP BY model_id ORDER BY model_id")
for mid, c in cur.fetchall():
    if mid not in model_ids:
        print(f"    ORPHAN model_id={mid} rows={c}")

print("== 6. route_bindings 是否也有孤儿引用 ==")
cur.execute("SELECT model_id, COUNT(*) c FROM route_bindings GROUP BY model_id ORDER BY model_id")
for mid, c in cur.fetchall():
    if mid not in model_ids:
        print(f"    ORPHAN model_id={mid} rows={c}")
print("done")
conn.close()
