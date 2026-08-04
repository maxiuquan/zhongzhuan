"""SQLite store tests."""

import asyncio

import pytest

from zhongzhuan.store.store import create_store
from zhongzhuan.config import default_config


@pytest.mark.asyncio
async def test_open_applies_migrations(tmp_path):
    cfg = default_config()
    cfg.storage.db_path = str(tmp_path / "test.db")
    cfg.tidb = None  # 强制使用 SQLite
    s = await create_store(cfg)
    try:
        # 验证迁移已应用：关键表存在
        row = await s.fetchone(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('models','api_keys','request_logs','access_tokens','model_pricing','key_health')"
        )
        assert row[0] == 6
    finally:
        await s.close()
