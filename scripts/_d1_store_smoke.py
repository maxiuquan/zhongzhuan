# -*- coding: utf-8 -*-
"""D1Store 真库冒烟：跑 migration（真建表）+ 全接口探测 + 清理。

一次性脚本；migration 执行后 D1 库即正式建好 schema v17。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("ZHONGZHUAN_D1_ACCOUNT_ID", os.environ.get("ZHONGZHUAN_D1_ACCOUNT_ID", ""))
os.environ.setdefault("ZHONGZHUAN_D1_DATABASE_ID", os.environ.get("ZHONGZHUAN_D1_DATABASE_ID", ""))
os.environ.setdefault("ZHONGZHUAN_D1_API_TOKEN", os.environ.get("ZHONGZHUAN_D1_API_TOKEN", ""))
if not all(os.environ.get(k) for k in ("ZHONGZHUAN_D1_ACCOUNT_ID", "ZHONGZHUAN_D1_DATABASE_ID", "ZHONGZHUAN_D1_API_TOKEN")):
    raise SystemExit("set ZHONGZHUAN_D1_ACCOUNT_ID / DATABASE_ID / API_TOKEN (values live in VPS /root/zhongzhuan/.env)")

from zhongzhuan.store.d1_store import D1Store  # noqa: E402


async def main() -> None:
    store = await D1Store.create(
        account_id=os.environ["ZHONGZHUAN_D1_ACCOUNT_ID"],
        database_id=os.environ["ZHONGZHUAN_D1_DATABASE_ID"],
        api_token=os.environ["ZHONGZHUAN_D1_API_TOKEN"],
    )
    print("== migration done, status:", store.status())

    # 1. 建表清单
    rows = await store.fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in rows]
    print(f"== tables ({len(tables)}):", tables)

    # 2. BLOB 往返（api_keys.key_cipher 走 bytes 参数 → X'' 内联 → 数组还原）
    #    api_keys.model_id 有外键 → 先插探针 model 行；先清上次失败残留（幂等）
    await store.execute("DELETE FROM key_health WHERE key_id=?", (999999,))
    await store.execute("DELETE FROM api_keys WHERE model_id=?", (999999,))
    await store.execute("INSERT OR REPLACE INTO models(id, name, upstream_base, upstream_model, enabled, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                        (999999, "_d1_smoke_model", "https://smoke.invalid", "smoke-model", 1, 1727800000, 1727800000))
    cipher = b"AES:" + bytes(range(32))
    rid = await store.execute(
        "INSERT INTO api_keys(model_id, label, key_cipher, enabled, priority, created_at) VALUES(?,?,?,?,?,?)",
        (999999, "_d1_smoke", cipher, 1, 100, 1727800000),
    )
    row = await store.fetchone(
        "SELECT id, model_id, label, key_cipher, enabled, priority, created_at FROM api_keys WHERE model_id=?",
        (999999,),
    )
    assert row is not None, "insert+fetch failed"
    assert row[0] == rid, f"lastrowid mismatch: {row[0]} != {rid}"
    assert row[3] == cipher, f"blob roundtrip mismatch: {row[3]!r}"
    print(f"== blob roundtrip ok (lastrowid={rid}, cipher len={len(row[3])})")

    # 3. execute_rowcount
    n = await store.execute_rowcount("UPDATE api_keys SET priority=? WHERE model_id=?", (200, 999999))
    assert n == 1, f"rowcount expected 1, got {n}"
    n0 = await store.execute_rowcount("UPDATE api_keys SET priority=? WHERE model_id=?", (200, 888888))
    assert n0 == 0, f"rowcount expected 0, got {n0}"
    print("== execute_rowcount ok")

    # 4. key_health sqlite 分支 UPSERT（ON CONFLICT DO UPDATE）
    await store.execute(
        """INSERT INTO key_health(key_id, status, cooldown_until, rpm_limit, tpm_limit,
                                  success_count, failure_count, recent_429_count, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key_id) DO UPDATE SET
             status=excluded.status, cooldown_until=excluded.cooldown_until,
             rpm_limit=excluded.rpm_limit, tpm_limit=excluded.tpm_limit,
             success_count=excluded.success_count, failure_count=excluded.failure_count,
             recent_429_count=excluded.recent_429_count, updated_at=excluded.updated_at""",
        (999999, "ok", 0.0, 60, 100000, 1, 0, 0, 1727800001),
    )
    await store.execute(
        """INSERT INTO key_health(key_id, status, cooldown_until, rpm_limit, tpm_limit,
                                  success_count, failure_count, recent_429_count, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key_id) DO UPDATE SET
             status=excluded.status, updated_at=excluded.updated_at""",
        (999999, "cooldown", 0.0, 60, 100000, 2, 0, 0, 1727800002),
    )
    kh = await store.fetchone("SELECT key_id, status, success_count FROM key_health WHERE key_id=?", (999999,))
    # 第二条 UPSERT 的 SET 子句只更新 status/updated_at，success_count 保持首次插入值 1
    assert kh == (999999, "cooldown", 1), f"upsert mismatch: {kh}"
    print("== key_health upsert ok:", kh)

    # 5. 清理冒烟数据
    await store.execute("DELETE FROM key_health WHERE key_id=?", (999999,))
    await store.execute("DELETE FROM api_keys WHERE model_id=?", (999999,))
    left = await store.fetchone("SELECT count(*) FROM api_keys")
    print(f"== cleanup ok (api_keys rows left: {left[0]})")

    print("== final status:", store.status())
    await store.close()
    print("SMOKE PASS")


if __name__ == "__main__":
    asyncio.run(main())
