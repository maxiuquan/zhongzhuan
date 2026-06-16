"""Seed the running proxy's DB with model + key via admin API."""
import asyncio
import os
import sys

import aiohttp


async def main():
    api_key = os.environ.get("AGNES_API_KEY", "sk-KhU0WpxQvXVyT59klRC2GabxH8HSEe7tBYs0L9Hnaqy85ifg")
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
        async with sess.post(f"{base}/api/models", json={
            "name": "agens",
            "upstream_base": "https://apihub.agnes-ai.com/",
            "upstream_model": "agnes-2.0-flash",
            "rpm_limit": 60,
            "tpm_limit": 100000,
        }) as r:
            print(f"[seed] create model: {r.status} {await r.text()}")

        # Get model id
        async with sess.get(f"{base}/api/models") as r:
            data = await r.json()
            model_id = data["data"][0]["id"]
            print(f"[seed] model_id = {model_id}")

        # Create key
        async with sess.post(f"{base}/api/keys", json={
            "model_id": model_id,
            "label": "test-key",
            "key_value": api_key,
            "priority": 0,
        }) as r:
            print(f"[seed] create key: {r.status} {await r.text()}")


asyncio.run(main())
