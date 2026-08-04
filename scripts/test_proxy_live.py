"""Make a real chat completion request through the running proxy."""
import asyncio
import os

import aiohttp


async def main():
    async with aiohttp.ClientSession() as sess:
        # Non-stream
        print("\n=== Non-stream ===")
        async with sess.post(
            "http://127.0.0.1:8088/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            json={"model": "agens", "messages": [{"role": "user", "content": "Hello, please respond with a short greeting."}]},
            timeout=60.0,
        ) as r:
            text = await r.text()
            print(f"status={r.status}")
            print(f"body={text[:500]}")

        # Stream
        print("\n=== Stream ===")
        async with sess.post(
            "http://127.0.0.1:8088/v1/chat/completions",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            json={"model": "agens", "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
            timeout=60.0,
        ) as r:
            print(f"status={r.status}")
            chunks = []
            async for line in r.content:
                chunks.append(line)
                if len(chunks) > 6:
                    break
            for c in chunks:
                print(f"  chunk: {c[:200]!r}")


asyncio.run(main())
