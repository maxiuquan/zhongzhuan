"""Admin API tests."""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

import socket

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.admin import AdminServer
from zhongzhuan.store import Store


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


@pytest.mark.asyncio
async def test_list_models_empty(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    admin = AdminServer(store=s)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/api/models") as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body == {"data": []}
    finally:
        await runner.cleanup()
        s.close()


@pytest.mark.asyncio
async def test_create_and_list_models(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    admin = AdminServer(store=s)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/api/models",
                json={"name": "m1", "upstream_base": "http://x", "upstream_model": "m1"},
            ) as resp:
                body = await resp.json()
                assert resp.status == 201
                assert body["name"] == "m1"
            async with sess.get(f"http://127.0.0.1:{port}/api/models") as resp:
                body = await resp.json()
                assert len(body["data"]) == 1
    finally:
        await runner.cleanup()
        s.close()


@pytest.mark.asyncio
async def test_create_key_api(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    admin = AdminServer(store=s)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            # Create model first
            async with sess.post(
                f"http://127.0.0.1:{port}/api/models",
                json={"name": "m1", "upstream_base": "http://x", "upstream_model": "m1"},
            ) as resp:
                m = await resp.json()
            # Create key
            async with sess.post(
                f"http://127.0.0.1:{port}/api/keys",
                json={"model_id": m["id"], "label": "test", "key_value": "sk-test123"},
            ) as resp:
                body = await resp.json()
                assert resp.status == 201
                assert "***" in body["key_masked"]
    finally:
        await runner.cleanup()
        s.close()


@pytest.mark.asyncio
async def test_stats_empty(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    admin = AdminServer(store=s)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/api/stats?range=1h") as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body["success_rate"] == 1.0
    finally:
        await runner.cleanup()
        s.close()


@pytest.mark.asyncio
async def test_ui_serves(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    admin = AdminServer(store=s)
    port = _free_port()
    runner = web.AppRunner(admin.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "Zhongzhuan" in text
    finally:
        await runner.cleanup()
        s.close()