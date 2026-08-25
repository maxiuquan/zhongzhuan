"""Seed the running proxy's DB with model + key via admin API."""

import asyncio
import os
import sys

import aiohttp


async def main():
    # 安全红线：密钥只允许从环境注入，绝不硬编码默认值（历史版本曾把真实
    # key 写死在此处并入库 git——该密钥应视为已泄露并轮换）。
    api_key = os.environ.get("AGNES_API_KEY", "").strip()
    if not api_key:
        print("[seed] AGNES_API_KEY not set; nothing to seed. "
              "Set it explicitly:  AGNES_API_KEY=sk-... python tests/seed_admin_api.py")
        return
    base = "http://127.0.0.1:8089"

    async with aiohttp.ClientSession() as sess:
        # First check existing models
        async with sess.get(f"{base}/api/models") as r:
            data = await r.json()
            print(f"[seed] existing models: {data}")
            for m in data.get("data", []):
                await sess.delete(f"{base}/api/models/{m['id']}")

        async with sess.get(f"{base}/api/keys") as r:
            data = await r.json()
            print(f"[seed] existing keys: {data}")
            for k in data.get("data", []):
                await sess.delete(f"{base}/api/keys/{k['id']}")

        # Create model
        async with sess.post(
            f"{base}/api/models",
            json={
                "name": "agens",
                "upstream_base": "https://apihub.agnes-ai.com/",
                "upstream_model": "agnes-2.0-flash",
                "rpm_limit": 60,
                "tpm_limit": 100000,
            },
        ) as r:
            print(f"[seed] create model: {r.status} {await r.text()}")

        # Get model id
        async with sess.get(f"{base}/api/models") as r:
            data = await r.json()
            model_id = data["data"][0]["id"]
            print(f"[seed] model_id = {model_id}")

        # Create key
        async with sess.post(
            f"{base}/api/keys",
            json={
                "model_id": model_id,
                "label": "test-key",
                "key_value": api_key,
                "priority": 0,
            },
        ) as r:
            print(f"[seed] create key: {r.status} {await r.text()}")


asyncio.run(main())
