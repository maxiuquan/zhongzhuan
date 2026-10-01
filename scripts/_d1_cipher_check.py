# -*- coding: utf-8 -*-
"""密文完整性核对：Aiven api_keys.key_cipher vs D1（逐行 hex 比对）。ASCII only."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import aiomysql
from zhongzhuan.store.d1_store import D1Store


async def main() -> None:
    mysql = await aiomysql.connect(
        host=os.environ["ZHONGZHUAN_TIDB_HOST"],
        port=int(os.environ.get("ZHONGZHUAN_TIDB_PORT", "3306")),
        user=os.environ["ZHONGZHUAN_TIDB_USER"],
        password=os.environ["ZHONGZHUAN_TIDB_PASSWORD"],
        db=os.environ.get("ZHONGZHUAN_TIDB_DATABASE", "zhongzhuan"),
    )
    async with mysql.cursor() as cur:
        await cur.execute("SELECT id, HEX(key_cipher) FROM api_keys ORDER BY id")
        src = {r[0]: r[1] for r in await cur.fetchall()}
    mysql.close()

    store = await D1Store.create(
        account_id=os.environ["ZHONGZHUAN_D1_ACCOUNT_ID"],
        database_id=os.environ["ZHONGZHUAN_D1_DATABASE_ID"],
        api_token=os.environ["ZHONGZHUAN_D1_API_TOKEN"],
    )
    rows = await store.fetchall("SELECT id, key_cipher FROM api_keys ORDER BY id")
    dst = {r[0]: bytes(r[1]).hex().upper() for r in rows}

    mismatch = [k for k in src if src[k] != dst.get(k)]
    print(f"src={len(src)} dst={len(dst)} mismatched={len(mismatch)}")
    if mismatch:
        print("BAD IDS:", mismatch[:20])
        raise SystemExit(1)
    print("CIPHER INTEGRITY OK")


if __name__ == "__main__":
    asyncio.run(main())
