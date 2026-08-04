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


# ---------------------------------------------------------------------------
# 测试隔离守卫（ZZ-OPS-20260805-01 §3.1 / 遗留事项 6.2）
#
# 在生产目录（如 VPS 的 /root/zhongzhuan）直接跑 pytest 时，工作目录里存在
# 真实生产配置：`load_config` 在路径缺失时通过 ``load_dotenv(".env")`` 回退
# 读取工作目录的 .env / config.yaml（端口 8443、Let's Encrypt 证书、TiDB），
# 使断言默认值（proxy 8088）的用例全部虚假失败（VPS 实测 45 failed）。
#
# 这里的守卫**只告警、不 fail**：仓库自带的默认 config.yaml（proxy 8088 /
# admin 8089 / SQLite，无 TLS）不算生产信号，本地开发跑测试保持安静；只有
# 检测到生产信号（8443 / letsencrypt / tidb / 生产 .env / secret.key）才打印
# 醒目 WARNING，提醒开发者换到干净目录跑，避免把环境问题误判成代码缺陷。
# ---------------------------------------------------------------------------


def _scan_production_signals(directory: Path) -> list[str]:
    """Return human-readable production-config signals found under *directory*.

    The repo's own default ``config.yaml`` (proxy 8088 / admin 8089, SQLite,
    no TLS) is intentionally *not* a signal, so normal local runs stay quiet.
    """
    signals: list[str] = []

    cfg_path = directory / "config.yaml"
    if cfg_path.is_file():
        try:
            cfg_text = cfg_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            cfg_text = ""
        cfg_lower = cfg_text.lower()
        if "port: 8443" in cfg_lower or "letsencrypt" in cfg_lower:
            signals.append(f"{cfg_path}: HTTPS proxy (port 8443 / letsencrypt cert) is configured")
        if "backend: tidb" in cfg_lower or "tidbcloud" in cfg_lower:
            signals.append(f"{cfg_path}: TiDB backend is configured")

    env_path = directory / ".env"
    if env_path.is_file():
        try:
            env_text = env_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            env_text = ""
        env_lower = env_text.lower()
        prod_env_signals = [
            name
            for name in (
                "zhongzhuan_tidb_",           # TiDB Cloud 生产凭据
                "zhongzhuan_proxy_port=8443", # 生产 HTTPS 端口
                "zhongzhuan_tls_",            # 证书 / key 路径
                "zhongzhuan_env=production",  # 显式生产模式
                "zhongzhuan_jwt_secret",      # 生产鉴权密钥
            )
            if name in env_lower
        ]
        if prod_env_signals:
            signals.append(
                f"{env_path}: production env overrides present "
                f"({', '.join(sorted(prod_env_signals))})"
            )

    secret_path = directory / "secret.key"
    if secret_path.is_file():
        signals.append(f"{secret_path}: production HMAC secret is present")

    return signals


def pytest_configure(config) -> None:
    """Warn (never fail) when pytest runs inside a production checkout."""
    directories = [Path.cwd(), ROOT]
    seen: set[Path] = set()
    signals: list[str] = []
    for directory in directories:
        resolved = directory.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        signals.extend(_scan_production_signals(resolved))

    if signals:
        print("\n" + "!" * 78, file=sys.stderr)
        print("WARNING: production configuration detected in the test working directory.", file=sys.stderr)
        print("load_config() falls back to cwd .env / config.yaml, so tests asserting", file=sys.stderr)
        print("dev defaults (e.g. proxy port 8088) may FAIL with false positives.", file=sys.stderr)
        print("Run pytest from a clean checkout (git clone) or move these files away:", file=sys.stderr)
        for signal in signals:
            print(f"  - {signal}", file=sys.stderr)
        print("!" * 78 + "\n", file=sys.stderr)


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
