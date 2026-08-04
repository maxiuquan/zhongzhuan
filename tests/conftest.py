"""pytest configuration: add src/ to import path."""

import os

os.environ.setdefault("ZHONGZHUAN_DEV_NO_DPAPI", "1")

import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Make `tests/support` importable as a top-level package (`from support import ...`).
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    """共享异步 SQLite store fixture，测试结束自动关闭。"""
    # 强制 SQLite，清除 TiDB 环境变量（避免连到真实数据库）
    monkeypatch.delenv("ZHONGZHUAN_TIDB_HOST", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_PORT", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_USER", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_PASSWORD", raising=False)
    monkeypatch.delenv("ZHONGZHUAN_TIDB_DATABASE", raising=False)
    from zhongzhuan.store.store import create_store
    from zhongzhuan.config import default_config

    cfg = default_config()
    cfg.storage.backend = "sqlite"
    cfg.storage.db_path = str(tmp_path / "test.db")
    cfg.storage.sqlite_db_path = str(tmp_path / "test.db")
    s = await create_store(cfg)
    # 初始化 crypto（AES key）
    from zhongzhuan.crypto import init

    await init(tmp_path)
    try:
        yield s
    finally:
        await s.close()
