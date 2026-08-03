# Zhongzhuan 实现计划 (Python)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个 Windows 本地常驻的 OpenAI 兼容 API 中转代理，让 Cursor / Cline / Continue / Aider 等 AI 编程工具在「Solo / Agent 模式」下不因上游 429/500 中断任务。

**Architecture:** 单 Python 进程（PyInstaller --onefile 打包成单 exe），双端口（aiohttp 代理 + aiohttp 管理）；模块拆为 proxy/upstream/admin/store/crypto/config/service；调度器用「健康度评分 + 滑动窗口」选 Key，用「轮询 / 加权 / 故障转移」选模型；Key 用 Windows DPAPI（ctypes 调 Crypt32）落盘；SQLite（stdlib）持久化；Web 后台 vanilla JS。

**Tech Stack:** Python 3.10+ / aiohttp 3.9+ / httpx 0.27+ / sqlite3 (stdlib) / pyyaml / loguru / PyInstaller (build) / pytest (test)

**参考文档:** `docs/superpowers/specs/2026-06-14-zhongzhuan-design.md`

**已确认环境（2026-06-14）:**
- Python 3.13.13 (py.exe 也指向 3.9.0，**用 `python` 不用 `py`**)
- pip 已有: aiohttp 3.13.2, httpx 0.28.1, Flask 3.0.0, pyinstaller 6.19.0, pywin32-ctypes 0.2.3
- PowerShell 5.1 终端（**有 SetCursorPosition bug**，长命令会被中断；用 `python` 直接调脚本）

---

## 文件总览

```
zhongzhuan/
├── src/zhongzhuan/                # 主包
│   ├── __init__.py                # 版本号
│   ├── __main__.py                # python -m zhongzhuan 入口
│   ├── app.py                     # AppContext + 启动编排
│   ├── config/
│   │   ├── __init__.py
│   │   ├── config.py              # YAML 加载 + dataclass
│   │   └── paths.py               # %ProgramData% / 绿色版路径
│   ├── observability/
│   │   ├── __init__.py
│   │   └── log.py                 # loguru 配置
│   ├── store/
│   │   ├── __init__.py
│   │   ├── store.py               # SQLite 初始化、迁移
│   │   ├── schema.py              # CREATE TABLE 字符串
│   │   ├── models.py              # 模型 CRUD
│   │   ├── keys.py                # Key CRUD
│   │   ├── groups.py              # 分组 CRUD
│   │   └── logs.py                # 日志 + 统计
│   ├── crypto/
│   │   ├── __init__.py
│   │   ├── dpapi_windows.py       # DPAPI (Crypt32 via ctypes)
│   │   └── dpapi_other.py         # 非 Windows 平台 stub
│   ├── upstream/
│   │   ├── __init__.py
│   │   └── client.py              # httpx.AsyncClient 包装
│   ├── proxy/
│   │   ├── __init__.py
│   │   ├── server.py              # aiohttp App: 代理端口
│   │   ├── handler.py             # /v1/* 路由分发
│   │   ├── scheduler.py           # 选 model / 选 key
│   │   ├── ratelimit.py           # 信号量 + 滑动窗口
│   │   ├── retry.py               # 重试 & fallback
│   │   └── stream.py              # SSE 透传
│   ├── admin/
│   │   ├── __init__.py
│   │   ├── server.py              # aiohttp App: 管理端口
│   │   ├── api_models.py
│   │   ├── api_keys.py
│   │   ├── api_groups.py
│   │   ├── api_service.py         # 服务控制 (调 sc.exe)
│   │   ├── api_stats.py
│   │   ├── api_logs.py
│   │   ├── api_export_import.py
│   │   └── ui.py                  # 静态资源 (importlib.resources)
│   ├── service/
│   │   ├── __init__.py
│   │   └── service.py             # sc.exe / 注册表封装
│   └── web/                       # 前端 (importlib.resources 引用)
│       ├── index.html
│       ├── app.js
│       └── style.css
├── scripts/
│   ├── build.ps1                  # PyInstaller + Inno Setup
│   ├── dev.ps1                    # 开发模式
│   ├── installer.iss              # Inno Setup 脚本
│   ├── green-install.bat          # 绿色版注册 Run 键
│   └── green-uninstall.bat
├── tests/                         # pytest
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_upstream.py
│   ├── test_proxy.py
│   ├── test_ratelimit.py
│   ├── test_scheduler.py
│   ├── test_store.py
│   ├── test_crypto.py
│   └── test_admin.py
├── pyproject.toml                 # 项目元数据 + 依赖
├── requirements.txt
├── LICENSE                        # MIT
└── README.md
```

---

## 测试约定

- 单元测试用 `python -m pytest tests/ -v`
- Mock 上游：`tests/mock_upstream.py`（一个 `aiohttp` 小服务，能按配置返回 200/429/500/慢响应/SSE）
- 命令行测试：所有命令直接用 `python` 启动，避开 PowerShell 渲染 bug

---

# 里程碑 1：最小可跑

> 目标：双击 exe（或 `python -m zhongzhuan`），能代理 `/v1/chat/completions` 到硬编码的上游 + 硬编码的 1 个 key。

### Task 1.1：项目脚手架

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `src/zhongzhuan/__init__.py`
- Create: `src/zhongzhuan/__main__.py`
- Create: `.gitignore`
- Create: `LICENSE` (MIT)
- Create: `README.md`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1：创建项目根目录的占位文件**

```bash
cd F:\xiangmu\zhongzhuan
mkdir src\zhongzhuan
mkdir tests
mkdir scripts
```

- [ ] **Step 2：写 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "zhongzhuan"
version = "0.1.0"
description = "Local OpenAI-compatible API relay for AI coding tools"
readme = "README.md"
license = {file = "LICENSE"}
requires-python = ">=3.10"
dependencies = [
    "aiohttp>=3.9",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "loguru>=0.7",
]

[project.optional-dependencies]
build = ["pyinstaller>=6.0"]
dev = ["pytest>=7.4", "pytest-asyncio>=0.23", "aiohttp>=3.9"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3：写 `requirements.txt`**

```
aiohttp>=3.9
httpx>=0.27
pyyaml>=6.0
loguru>=0.7
```

- [ ] **Step 4：写 `src/zhongzhuan/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 5：写 `src/zhongzhuan/__main__.py`（占位）**

```python
"""命令行入口：python -m zhongzhuan [args]"""
import sys


def main() -> int:
    print("zhongzhuan dev build")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6：写 `.gitignore`**

```
/dist/
/build/
*.exe
__pycache__/
*.pyc
*.pyo
*.db
*.log
data/
logs/
.venv/
venv/
.pytest_cache/
.ruff_cache/
```

- [ ] **Step 7：写 `LICENSE`（MIT 标准文本，Copyright 2026）**

- [ ] **Step 8：写 `tests/conftest.py`（添加 src 到 sys.path）**

```python
"""pytest 配置：把 src/ 加入 import 路径。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
```

- [ ] **Step 9：跑通**

```bash
cd F:\xiangmu\zhongzhuan
python -m zhongzhuan
# 预期: zhongzhuan dev build
```

- [ ] **Step 10：commit**

```bash
git init
git add -A
git commit -m "chore: scaffold project (M1.1)"
```

---

### Task 1.2：配置加载

**Files:**
- Create: `src/zhongzhuan/config/config.py`
- Create: `src/zhongzhuan/config/paths.py`
- Create: `src/zhongzhuan/config/__init__.py`
- Create: `src/zhongzhuan/observability/log.py`
- Create: `src/zhongzhuan/observability/__init__.py`
- Create: `tests/test_config.py`

- [ ] **Step 1：写测试 `tests/test_config.py`**

```python
"""配置默认值 + YAML 加载测试。"""
from zhongzhuan.config.config import Config, default_config, load_config


def test_default_proxy_port():
    cfg = default_config()
    assert cfg.server.proxy.port == 8088


def test_default_admin_port():
    cfg = default_config()
    assert cfg.server.admin.port == 8089


def test_default_host():
    cfg = default_config()
    assert cfg.server.proxy.host == "127.0.0.1"
    assert cfg.server.admin.host == "127.0.0.1"


def test_default_global_concurrent():
    cfg = default_config()
    assert cfg.limits.global_concurrent == 64


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "missing.yaml"))
    assert cfg.server.proxy.port == 8088
```

- [ ] **Step 2：跑测试 → 应失败（Config 未定义）**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_config.py -v
# 预期: ModuleNotFoundError
```

- [ ] **Step 3：实现 `src/zhongzhuan/config/config.py`**

```python
"""配置模型：YAML 加载 + 默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


@dataclass
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 0


@dataclass
class TLSConfig:
    enabled: bool = False
    cert_file: str = ""
    key_file: str = ""


@dataclass
class ServerConfig:
    proxy: ListenConfig = field(default_factory=lambda: ListenConfig(port=8088))
    admin: ListenConfig = field(default_factory=lambda: ListenConfig(port=8089))
    tls: TLSConfig = field(default_factory=TLSConfig)


@dataclass
class LimitsConfig:
    global_concurrent: int = 64
    per_key_window_seconds: int = 60
    default_rpm_per_key: int = 60
    default_tpm_per_key: int = 100000
    proxy_request_timeout: int = 30


@dataclass
class StorageConfig:
    db_path: str = "data.db"
    log_dir: str = "logs"


@dataclass
class WinSvcConfig:
    display_name: str = "Zhongzhuan API Relay"
    auto_start: bool = True
    service_name: str = "Zhongzhuan"


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    windows_service: WinSvcConfig = field(default_factory=WinSvcConfig)


def default_config() -> Config:
    return Config()


def _merge(dc, data: dict) -> None:
    """把 dict 合并进 dataclass 实例（只覆盖已存在的字段）。"""
    for k, v in data.items():
        if hasattr(dc, k):
            cur = getattr(dc, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                _merge(cur, v)
            else:
                setattr(dc, k, v)


def load_config(path: str | None) -> Config:
    """加载 YAML 配置文件；不存在则返回默认配置。"""
    cfg = default_config()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _merge(cfg, data)
    return cfg


def save_config(cfg: Config, path: str) -> None:
    """把配置写回 YAML。"""
    from dataclasses import asdict

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(asdict(cfg), f, allow_unicode=True, sort_keys=False)
```

- [ ] **Step 4：写 `src/zhongzhuan/config/paths.py`**

```python
"""Windows 路径解析。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def exe_dir() -> Path:
    """PyInstaller --onefile 解压根目录 / 脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def program_data_dir() -> Path:
    """%ProgramData%\\Zhongzhuan。"""
    base = os.environ.get("ProgramData", "C:\\ProgramData")
    return Path(base) / "Zhongzhuan"


def is_admin() -> bool:
    """Windows 下判断是否以管理员运行。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resolve_data_dir(service_mode: bool) -> Path:
    """根据运行模式选数据目录；不可写时退到 %APPDATA%。"""
    if service_mode:
        d = program_data_dir()
    else:
        d = exe_dir()
    d.mkdir(parents=True, exist_ok=True)
    test = d / ".write_test"
    try:
        test.write_text("ok")
        test.unlink()
    except OSError:
        d = Path(os.environ.get("APPDATA", str(exe_dir()))) / "Zhongzhuan"
        d.mkdir(parents=True, exist_ok=True)
    return d
```

- [ ] **Step 5：写 `src/zhongzhuan/config/__init__.py`**

```python
from .config import Config, default_config, load_config, save_config
from .paths import exe_dir, program_data_dir, resolve_data_dir, is_admin

__all__ = [
    "Config", "default_config", "load_config", "save_config",
    "exe_dir", "program_data_dir", "resolve_data_dir", "is_admin",
]
```

- [ ] **Step 6：写 `src/zhongzhuan/observability/log.py`**

```python
"""loguru 配置。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )
    logger.add(
        log_dir / "app-{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
    )
```

- [ ] **Step 7：写 `src/zhongzhuan/observability/__init__.py`**

```python
from .log import logger, setup_logging

__all__ = ["logger", "setup_logging"]
```

- [ ] **Step 8：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_config.py -v
# 预期: 5 passed
```

- [ ] **Step 9：commit**

```bash
git add -A
git commit -m "feat(config): defaults + YAML loader + loguru (M1.2)"
```

---

### Task 1.3：硬编码上游 + 透传 HTTP 客户端

**Files:**
- Create: `src/zhongzhuan/upstream/client.py`
- Create: `src/zhongzhuan/upstream/__init__.py`
- Create: `tests/test_upstream.py`
- Create: `tests/mock_upstream.py`

- [ ] **Step 1：写 mock 上游 `tests/mock_upstream.py`**

```python
"""测试用 mock 上游。启动：python -m tests.mock_upstream [port]"""
from __future__ import annotations

import sys
from aiohttp import web


async def chat_completions(request: web.Request) -> web.Response:
    """回显 Authorization 头。"""
    auth = request.headers.get("Authorization", "")
    return web.json_response(
        {
            "id": "mock",
            "model": "x",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": f"echo; auth={auth}",
                    }
                }
            ],
        }
    )


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    return app


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 19999
    web.run_app(make_app(), port=port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2：写 `src/zhongzhuan/upstream/client.py`**

```python
"""上游 HTTP 客户端：httpx.AsyncClient 包装。"""
from __future__ import annotations

from typing import AsyncIterator

import httpx


class UpstreamClient:
    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        if self._client is None:
            await self.start()
        assert self._client is not None
        return await self._client.request(
            method, path, headers=headers, content=content, params=params,
        )

    async def stream(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        params: dict | None = None,
    ) -> AsyncIterator[httpx.Response]:
        if self._client is None:
            await self.start()
        assert self._client is not None
        async with self._client.stream(
            method, path, headers=headers, content=content, params=params
        ) as resp:
            yield resp
```

- [ ] **Step 3：写 `src/zhongzhuan/upstream/__init__.py`**

```python
from .client import UpstreamClient

__all__ = ["UpstreamClient"]
```

- [ ] **Step 4：写测试 `tests/test_upstream.py`**

```python
"""UpstreamClient 测试。"""
import pytest
from aiohttp import web

from zhongzhuan.upstream import UpstreamClient


@pytest.fixture
async def mock_server():
    async def handler(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        return web.json_response({"auth": auth, "ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest.mark.asyncio
async def test_request_passes_authorization(mock_server: str):
    client = UpstreamClient(base_url=mock_server, timeout=5.0)
    await client.start()
    try:
        resp = await client.request(
            "POST", "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
            content=b'{"model":"x"}',
        )
        body = resp.json()
        assert resp.status_code == 200
        assert body["auth"] == "Bearer sk-test"
    finally:
        await client.close()
```

- [ ] **Step 5：跑测试**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_upstream.py -v
# 预期: 1 passed
```

- [ ] **Step 6：commit**

```bash
git add -A
git commit -m "feat(upstream): httpx async client + mock (M1.3)"
```

---

### Task 1.4：代理 HTTP 服务（透传）

**Files:**
- Create: `src/zhongzhuan/proxy/server.py`
- Create: `src/zhongzhuan/proxy/handler.py`
- Create: `src/zhongzhuan/proxy/__init__.py`
- Modify: `src/zhongzhuan/__main__.py`
- Create: `tests/test_proxy.py`

- [ ] **Step 1：写测试 `tests/test_proxy.py`**

```python
"""代理透传测试。"""
import socket

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient


@pytest.fixture
async def mock_upstream():
    async def handler(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bearer sk-1":
            return web.Response(status=401, text="bad auth")
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    app.router.add_get("/v1/models", lambda r: web.json_response({"data": []}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


@pytest.mark.asyncio
async def test_proxy_passes_through_chat_completions(mock_upstream: str):
    upstream = UpstreamClient(base_url=mock_upstream, timeout=5.0)
    await upstream.start()
    proxy = ProxyServer(upstream=upstream, api_key="sk-1", proxy_timeout=5.0)
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                data='{"model":"x","messages":[]}',
            ) as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body == {"ok": True}
    finally:
        await runner.cleanup()
        await upstream.close()
```

- [ ] **Step 2：写 `src/zhongzhuan/proxy/handler.py`**

```python
"""/v1/* 路由处理：透传到上游。"""
from __future__ import annotations

from aiohttp import web

from ..upstream import UpstreamClient


class Handler:
    def __init__(
        self,
        upstream: UpstreamClient,
        api_key: str,
        proxy_timeout: float,
    ) -> None:
        self.upstream = upstream
        self.api_key = api_key
        self.proxy_timeout = proxy_timeout

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        headers = dict(request.headers)
        for h in ("Host", "Content-Length", "Authorization"):
            headers.pop(h, None)
        headers["Authorization"] = f"Bearer {self.api_key}"
        path = request.path
        try:
            resp = await self.upstream.request(
                request.method, path, headers=headers, content=body,
            )
        except Exception as e:  # noqa: BLE001
            return web.json_response(
                {"error": {"message": str(e), "type": "upstream_error"}},
                status=502,
            )

        resp_headers = dict(resp.headers)
        for h in ("content-length", "transfer-encoding", "connection"):
            resp_headers.pop(h, None)

        return web.Response(
            status=resp.status_code, body=resp.content, headers=resp_headers,
        )


def make_handler(
    upstream: UpstreamClient, api_key: str, proxy_timeout: float,
) -> Handler:
    return Handler(upstream=upstream, api_key=api_key, proxy_timeout=proxy_timeout)
```

- [ ] **Step 3：写 `src/zhongzhuan/proxy/server.py`**

```python
"""代理 HTTP 服务。"""
from __future__ import annotations

from aiohttp import web

from .handler import make_handler
from ..upstream import UpstreamClient


class ProxyServer:
    def __init__(
        self,
        upstream: UpstreamClient,
        api_key: str = "",
        keys: list | None = None,
        proxy_timeout: float = 30.0,
        models: list[dict] | None = None,
        groups: list[dict] | None = None,
    ) -> None:
        self.upstream = upstream
        self.api_key = api_key
        self.keys = keys or []
        self.proxy_timeout = proxy_timeout
        self.models = models or []
        self.groups = groups or []

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        # M1: 单 key 直接传 api_key
        handler = make_handler(
            upstream=self.upstream,
            api_key=self.api_key or (self.keys[0].api_key if self.keys else ""),
            proxy_timeout=self.proxy_timeout,
        )
        app.router.add_route("*", "/v1/{tail:.*}", handler)
        app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
        app.router.add_get("/version", self._version)
        app.router.add_get("/v1/models", self._list_models)
        return app

    async def _version(self, _request: web.Request) -> web.Response:
        from zhongzhuan import __version__
        return web.json_response({"name": "zhongzhuan", "version": __version__})

    async def _list_models(self, _request: web.Request) -> web.Response:
        items: list[dict] = []
        for m in self.models:
            items.append({"id": m.get("name", ""), "object": "model"})
        for g in self.groups:
            items.append({"id": g.get("name", ""), "object": "model"})
        return web.json_response({"object": "list", "data": items})
```

- [ ] **Step 4：写 `src/zhongzhuan/proxy/__init__.py`**

```python
from .server import ProxyServer

__all__ = ["ProxyServer"]
```

- [ ] **Step 5：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_proxy.py -v
# 预期: 1 passed
```

- [ ] **Step 6：写完整 `__main__.py`**

```python
"""命令行入口：python -m zhongzhuan [args]"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

import yaml

from zhongzhuan import __version__
from zhongzhuan.config import default_config, load_config, resolve_data_dir
from zhongzhuan.observability import setup_logging
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.upstream import UpstreamClient


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="zhongzhuan", description="OpenAI API relay")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--upstream", default=None, help="硬编码上游 base URL")
    p.add_argument("--key", default=None, help="硬编码 API key (也读 ZHONGZHUAN_KEY)")
    p.add_argument("--service", action="store_true", help="Windows Service 入口")
    p.add_argument("--version", action="version", version=f"zhongzhuan {__version__}")
    return p.parse_args()


def make_default_config(path: Path) -> None:
    cfg = default_config()
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {
                "server": {
                    "proxy": {"host": cfg.server.proxy.host, "port": cfg.server.proxy.port},
                    "admin": {"host": cfg.server.admin.host, "port": cfg.server.admin.port},
                },
                "limits": {"global_concurrent": cfg.limits.global_concurrent},
                "storage": {"db_path": "data.db", "log_dir": "logs"},
            },
            f, allow_unicode=True, sort_keys=False,
        )


async def run_foreground(
    cfg_path: str, port_override: int | None,
    upstream_url: str | None, key: str | None,
    as_service: bool = False,
) -> int:
    from aiohttp import web
    from loguru import logger

    cfg = load_config(cfg_path)
    if port_override is not None:
        cfg.server.proxy.port = port_override

    data_dir = resolve_data_dir(service_mode=as_service)
    setup_logging(data_dir / cfg.storage.log_dir)
    logger.info("zhongzhuan starting", cfg=str(cfg_path), data_dir=str(data_dir))

    upstream_base = upstream_url or os.environ.get(
        "ZHONGZHUAN_UPSTREAM", "https://api.openai.com",
    )
    api_key = key or os.environ.get("ZHONGZHUAN_KEY", "")

    upstream = UpstreamClient(base_url=upstream_base, timeout=cfg.limits.proxy_request_timeout)
    await upstream.start()

    proxy = ProxyServer(
        upstream=upstream, api_key=api_key,
        proxy_timeout=cfg.limits.proxy_request_timeout,
    )
    proxy_runner = web.AppRunner(proxy.app())
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, cfg.server.proxy.host, cfg.server.proxy.port)
    await proxy_site.start()
    logger.info("proxy listening", addr=f"{cfg.server.proxy.host}:{cfg.server.proxy.port}")

    stop = asyncio.Event()

    def _on_signal() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    if not as_service:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except NotImplementedError:
                pass

    try:
        await stop.wait()
    finally:
        await proxy_runner.cleanup()
        await upstream.close()
        logger.info("shutdown complete")
    return 0


def main() -> int:
    args = parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute() and not cfg_path.exists():
        make_default_config(cfg_path)
        print(f"[zhongzhuan] created default config: {cfg_path}", file=sys.stderr)
    return asyncio.run(run_foreground(
        args.config, args.port, args.upstream, args.key,
        as_service=args.service,
    ))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7：本地跑通（端到端）**

打开两个终端：

```bash
# 终端 A
cd F:\xiangmu\zhongzhuan
python -m tests.mock_upstream 19999
```

```bash
# 终端 B
cd F:\xiangmu\zhongzhuan
set ZHONGZHUAN_KEY=sk-test
set ZHONGZHUAN_UPSTREAM=http://127.0.0.1:19999
python -m zhongzhuan
```

```bash
# 终端 C：测试
curl -X POST http://127.0.0.1:8088/v1/chat/completions ^
  -H "Authorization: Bearer whatever" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"x\",\"messages\":[]}"
# 预期: {"id":"mock",...}
```

- [ ] **Step 8：commit**

```bash
git add -A
git commit -m "feat(proxy): pass-through HTTP server + main wiring (M1.4)"
```

**🎯 M1 验收：**
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] 手工 curl 透传成功
- [ ] `python -m zhongzhuan --version` 输出 `zhongzhuan 0.1.0`

---

# 里程碑 2：调度器

> 目标：多 key 轮转 + 滑动窗口 + 429/5xx 自动重试。客户端看到的是"几乎从不失败"。

### Task 2.1：滑动窗口

**Files:**
- Create: `src/zhongzhuan/proxy/ratelimit.py`
- Create: `tests/test_ratelimit.py`

- [ ] **Step 1：写测试 `tests/test_ratelimit.py`**

```python
"""SlidingWindow 测试。"""
import time

from zhongzhuan.proxy.ratelimit import SlidingWindow


def test_window_allows_below_limit():
    w = SlidingWindow(window_seconds=60, limit=3)
    assert w.allow(1)
    assert w.allow(1)
    assert w.allow(1)
    assert not w.allow(1)


def test_window_expires():
    w = SlidingWindow(window_seconds=1, limit=2)
    assert w.allow(1)
    assert w.allow(1)
    assert not w.allow(1)
    time.sleep(1.1)
    assert w.allow(1)


def test_window_unlimited():
    w = SlidingWindow(window_seconds=60, limit=0)
    for _ in range(1000):
        assert w.allow(1)
```

- [ ] **Step 2：实现 `src/zhongzhuan/proxy/ratelimit.py`**

```python
"""限流器：滑动窗口 + Key 健康度。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


class SlidingWindow:
    """60 个 1s 桶的循环数组；复杂度 O(1)。"""

    def __init__(self, window_seconds: int = 60, limit: int = 0) -> None:
        self.window_seconds = window_seconds
        self.limit = limit  # 0 = 不限
        self.buckets: list[int] = [0] * window_seconds
        self.total: int = 0
        self.last_rotate: float = time.time()

    def _rotate(self) -> None:
        now = time.time()
        elapsed = int(now - self.last_rotate)
        if elapsed <= 0:
            return
        if elapsed >= self.window_seconds:
            self.buckets = [0] * self.window_seconds
            self.total = 0
            self.last_rotate = now
            return
        for _ in range(elapsed):
            self.buckets.pop(0)
            self.buckets.append(0)
        # total 不变（推入的是 0）
        self.last_rotate = now

    def allow(self, n: int = 1) -> bool:
        self._rotate()
        if self.limit > 0 and self.total + n > self.limit:
            return False
        self.buckets[-1] += n
        self.total += n
        return True

    def current_usage(self) -> int:
        self._rotate()
        return self.total


@dataclass
class KeyHealth:
    key_id: int
    api_key: str
    window: SlidingWindow
    rpm_limit: int = 0
    cooldown_until: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    recent_429_count: int = 0

    def is_available(self) -> bool:
        if time.time() < self.cooldown_until:
            return False
        if self.rpm_limit > 0 and self.window.current_usage() >= self.rpm_limit:
            return False
        return True
```

- [ ] **Step 3：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_ratelimit.py -v
```

- [ ] **Step 4：commit**

```bash
git add -A
git commit -m "feat(proxy): sliding window + KeyHealth (M2.1)"
```

---

### Task 2.2：选 Key 评分

**Files:**
- Create: `src/zhongzhuan/proxy/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1：写测试 `tests/test_scheduler.py`**

```python
"""调度器测试。"""
import time

from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.proxy.scheduler import pick_key, score


def _kh(key_id: int, success: int = 0, failure: int = 0, cooldown: float = 0) -> KeyHealth:
    return KeyHealth(
        key_id=key_id, api_key=f"sk-{key_id}",
        window=SlidingWindow(60, 1000),
        success_count=success, failure_count=failure,
        cooldown_until=cooldown,
    )


def test_pick_key_prefers_higher_success_rate():
    keys = [
        _kh(1, success=10, failure=0),
        _kh(2, success=5, failure=5),
    ]
    picked = pick_key(keys)
    assert picked is not None
    assert picked.key_id == 1


def test_pick_key_skip_cooldown():
    keys = [
        _kh(1, cooldown=time.time() + 60),
        _kh(2),
    ]
    picked = pick_key(keys)
    assert picked is not None
    assert picked.key_id == 2


def test_pick_key_returns_none_when_all_unavailable():
    keys = [
        _kh(1, cooldown=time.time() + 60),
        _kh(2, cooldown=time.time() + 60),
    ]
    assert pick_key(keys) is None


def test_score_clamps():
    k = _kh(1, success=0, failure=0)
    s = score(k)
    assert 0.0 <= s <= 1.0
```

- [ ] **Step 2：实现 `src/zhongzhuan/proxy/scheduler.py`**

```python
"""调度器：选 model / 选 key。"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .ratelimit import KeyHealth


def score(k: KeyHealth) -> float:
    """Key 健康度评分。0-1 区间，越大越优先。"""
    if not k.is_available():
        return -1.0
    total = k.success_count + k.failure_count
    success_rate = 1.0 if total == 0 else k.success_count / total
    if k.rpm_limit > 0:
        window = 1.0 - k.window.current_usage() / k.rpm_limit
    else:
        window = 1.0
    return (
        0.50 * success_rate
        + 0.30 * window
        + 0.15 * (1.0 if k.api_key else 0.0)
        + 0.05 * random.random()
    )


def pick_key(keys: list[KeyHealth]) -> KeyHealth | None:
    best: KeyHealth | None = None
    best_score = -1.0
    for k in keys:
        s = score(k)
        if s > best_score:
            best_score = s
            best = k
    return best


# ---------------- Group 调度 ----------------


@dataclass
class GroupMember:
    model_id: int
    weight: int = 1
    ord: int = 0


@dataclass
class Group:
    id: int
    name: str
    strategy: str
    fallback_enabled: bool = True
    members: list[GroupMember] | None = None

    def member_ids(self) -> list[int]:
        return [m.model_id for m in (self.members or [])]


@dataclass
class ModelHealth:
    model_id: int
    name: str
    available: bool = True
    weight_penalty: float = 1.0


_round_robin_counters: dict[int, int] = {}


def pick_group_model(
    g: Group,
    models: dict[int, ModelHealth],
    last_model_id: int | None = None,
) -> ModelHealth | None:
    members = g.members or []
    if not members:
        return None
    if g.strategy == "failover":
        for m in sorted(members, key=lambda x: x.ord):
            h = models.get(m.model_id)
            if h and h.available:
                return h
        return None
    if g.strategy == "round_robin":
        candidates: list[ModelHealth] = []
        for m in members:
            if m.model_id == last_model_id:
                continue
            h = models.get(m.model_id)
            if h and h.available:
                candidates.append(h)
        if not candidates:
            for m in members:
                h = models.get(m.model_id)
                if h and h.available:
                    candidates.append(h)
        if not candidates:
            return None
        idx = _round_robin_counters.get(g.id, 0) % len(candidates)
        _round_robin_counters[g.id] = idx + 1
        return candidates[idx]
    if g.strategy == "weighted":
        weights: list[tuple[ModelHealth, float]] = []
        for m in members:
            h = models.get(m.model_id)
            if h and h.available:
                weights.append((h, m.weight * h.weight_penalty))
        if not weights:
            return None
        total = sum(w for _, w in weights)
        if total <= 0:
            return weights[0][0]
        pick = random.random() * total
        for h, w in weights:
            pick -= w
            if pick < 0:
                return h
        return weights[-1][0]
    return None
```

- [ ] **Step 3：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_scheduler.py -v
```

- [ ] **Step 4：commit**

```bash
git add -A
git commit -m "feat(proxy): key scoring + group strategies (M2.2)"
```

---

### Task 2.3：多 key 轮转 + 失败重试

**Files:**
- Create: `src/zhongzhuan/proxy/retry.py`
- Modify: `src/zhongzhuan/proxy/handler.py`
- Modify: `src/zhongzhuan/proxy/server.py`
- Create: `tests/test_proxy_retry.py`

- [ ] **Step 1：写测试 `tests/test_proxy_retry.py`**

```python
"""多 key 轮转 + 429 重试测试。"""
import socket
import time

import pytest
from aiohttp import ClientSession, web

from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.upstream import UpstreamClient


@pytest.fixture
async def mock_upstream_429():
    state = {"calls_b": 0}

    async def handler(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer sk-a":
            return web.Response(status=429, text="rate limited")
        state["calls_b"] += 1
        return web.json_response({"ok": True, "by": "b"})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield f"http://127.0.0.1:{port}", state
    await runner.cleanup()


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p


@pytest.mark.asyncio
async def test_proxy_rotates_key_on_429(mock_upstream_429):
    upstream_url, state = mock_upstream_429
    upstream = UpstreamClient(base_url=upstream_url, timeout=5.0)
    await upstream.start()
    keys = [
        KeyHealth(key_id=1, api_key="sk-a", window=SlidingWindow(60, 1000), rpm_limit=1000),
        KeyHealth(key_id=2, api_key="sk-b", window=SlidingWindow(60, 1000), rpm_limit=1000),
    ]
    proxy = ProxyServer(upstream=upstream, keys=keys, proxy_timeout=5.0)
    port = _free_port()
    runner = web.AppRunner(proxy.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        async with ClientSession() as sess:
            async with sess.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                headers={"Content-Type": "application/json"},
                data='{"model":"x"}',
            ) as resp:
                body = await resp.json()
                assert resp.status == 200
                assert body == {"ok": True, "by": "b"}
        assert state["calls_b"] == 1
        assert time.time() < keys[0].cooldown_until
    finally:
        await runner.cleanup()
        await upstream.close()
```

- [ ] **Step 2：实现 `src/zhongzhuan/proxy/retry.py`**

```python
"""重试 & cooldown 工具。"""
from __future__ import annotations

import time

from .ratelimit import KeyHealth


def cooldown_for(failures: int) -> float:
    """根据连续失败次数返回 cooldown 秒数。"""
    if failures <= 1:
        return 5.0
    if failures == 2:
        return 10.0
    if failures == 3:
        return 30.0
    return 60.0


def mark_failure(k: KeyHealth) -> None:
    k.failure_count += 1
    k.recent_429_count += 1
    k.cooldown_until = time.time() + cooldown_for(k.failure_count)


def mark_success(k: KeyHealth) -> None:
    k.success_count += 1
    if k.recent_429_count > 0:
        k.recent_429_count = 0
    k.cooldown_until = 0.0
```

- [ ] **Step 3：改 `src/zhongzhuan/proxy/server.py` 接受 keys（多 key 模式）**

替换为：

```python
"""代理 HTTP 服务。"""
from __future__ import annotations

from aiohttp import web

from .handler import make_handler
from ..upstream import UpstreamClient


class ProxyServer:
    def __init__(
        self,
        upstream: UpstreamClient,
        api_key: str = "",
        keys: list | None = None,
        proxy_timeout: float = 30.0,
        models: list[dict] | None = None,
        groups: list[dict] | None = None,
    ) -> None:
        self.upstream = upstream
        self.api_key = api_key
        self.keys = keys or []
        self.proxy_timeout = proxy_timeout
        self.models = models or []
        self.groups = groups or []

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        handler = make_handler(
            upstream=self.upstream, keys=self.keys,
            proxy_timeout=self.proxy_timeout,
        )
        # 兜底：单 key 旧用法
        if not self.keys and self.api_key:
            from .ratelimit import KeyHealth, SlidingWindow
            self.keys = [KeyHealth(
                key_id=0, api_key=self.api_key,
                window=SlidingWindow(60, 1000),
            )]
            handler = make_handler(
                upstream=self.upstream, keys=self.keys,
                proxy_timeout=self.proxy_timeout,
            )
        app.router.add_route("*", "/v1/{tail:.*}", handler)
        app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
        app.router.add_get("/version", self._version)
        app.router.add_get("/v1/models", self._list_models)
        return app

    async def _version(self, _request: web.Request) -> web.Response:
        from zhongzhuan import __version__
        return web.json_response({"name": "zhongzhuan", "version": __version__})

    async def _list_models(self, _request: web.Request) -> web.Response:
        items: list[dict] = []
        for m in self.models:
            items.append({"id": m.get("name", ""), "object": "model"})
        for g in self.groups:
            items.append({"id": g.get("name", ""), "object": "model"})
        return web.json_response({"object": "list", "data": items})
```

- [ ] **Step 4：改 `src/zhongzhuan/proxy/handler.py`（带重试）**

```python
"""/v1/* 路由处理：透传 + 多 key 重试。"""
from __future__ import annotations

from aiohttp import web

from ..upstream import UpstreamClient
from .ratelimit import KeyHealth
from .retry import mark_failure, mark_success
from .scheduler import pick_key


class Handler:
    def __init__(
        self,
        upstream: UpstreamClient,
        keys: list[KeyHealth],
        proxy_timeout: float,
    ) -> None:
        if not keys:
            raise ValueError("keys must not be empty")
        self.upstream = upstream
        self.keys = keys
        self.proxy_timeout = proxy_timeout

    async def __call__(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        base_headers = dict(request.headers)
        for h in ("Host", "Content-Length", "Authorization"):
            base_headers.pop(h, None)
        path = request.path

        tried: set[int] = set()
        last_error: tuple[int, bytes] | None = None
        for _ in range(len(self.keys)):
            k = pick_key([x for x in self.keys if x.key_id not in tried])
            if k is None:
                break
            tried.add(k.key_id)
            if k.window is not None and not k.window.allow(1):
                continue

            headers = dict(base_headers)
            headers["Authorization"] = f"Bearer {k.api_key}"
            try:
                resp = await self.upstream.request(
                    request.method, path, headers=headers, content=body,
                )
            except Exception as e:  # noqa: BLE001
                mark_failure(k)
                last_error = (502, str(e).encode())
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                mark_failure(k)
                last_error = (resp.status_code, await resp.aread())
                continue

            mark_success(k)
            data = await resp.aread()
            resp_headers = dict(resp.headers)
            for h in ("content-length", "transfer-encoding", "connection"):
                resp_headers.pop(h, None)
            return web.Response(status=resp.status_code, body=data, headers=resp_headers)

        if last_error:
            status, body = last_error
            return web.Response(status=status, body=body)
        return web.json_response(
            {"error": {"message": "upstream failed after retries", "type": "upstream_error"}},
            status=502,
        )


def make_handler(upstream, keys, proxy_timeout) -> Handler:
    return Handler(upstream=upstream, keys=keys, proxy_timeout=proxy_timeout)
```

- [ ] **Step 5：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_proxy_retry.py -v
```

- [ ] **Step 6：commit**

```bash
git add -A
git commit -m "feat(proxy): multi-key rotation + 429/5xx retry (M2.3)"
```

**🎯 M2 验收：** 配置 2 个 key，mock 上游让 key-a 永远 429，curl 5 次全部 200（fallback 到 key-b）。

---

# 里程碑 3：持久化

> 目标：配置 / Key / 日志 落 SQLite，Web 后台能增删。

### Task 3.1：SQLite 初始化 + schema

**Files:**
- Create: `src/zhongzhuan/store/store.py`
- Create: `src/zhongzhuan/store/schema.py`
- Create: `src/zhongzhuan/store/__init__.py`
- Create: `tests/test_store.py`

- [ ] **Step 1：写测试 `tests/test_store.py`**

```python
"""SQLite store 测试。"""
from zhongzhuan.store import Store


def test_open_applies_migrations(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    try:
        conn = s.connect()
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='models'"
        )
        assert cur.fetchone()[0] == 1
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_keys'"
        )
        assert cur.fetchone()[0] == 1
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='request_logs'"
        )
        assert cur.fetchone()[0] == 1
    finally:
        s.close()
```

- [ ] **Step 2：实现 `src/zhongzhuan/store/schema.py`**

```python
"""SQLite schema 字符串。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    upstream_base TEXT NOT NULL,
    upstream_model TEXT NOT NULL,
    rpm_limit     INTEGER NOT NULL DEFAULT 0,
    tpm_limit     INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    weight        INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id    INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    label       TEXT NOT NULL DEFAULT '',
    key_cipher  BLOB NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    priority    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    strategy        TEXT NOT NULL CHECK(strategy IN ('round_robin','weighted','failover')),
    fallback_enabled INTEGER NOT NULL DEFAULT 1,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_models (
    group_id  INTEGER NOT NULL REFERENCES model_groups(id) ON DELETE CASCADE,
    model_id  INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    weight    INTEGER NOT NULL DEFAULT 1,
    ord       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, model_id)
);

CREATE TABLE IF NOT EXISTS request_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    client_ip   TEXT,
    model_name  TEXT NOT NULL,
    resolved_model_id INTEGER,
    key_id      INTEGER,
    status      INTEGER NOT NULL,
    latency_ms  INTEGER NOT NULL,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    error       TEXT DEFAULT '',
    request_id  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model_name, ts);
"""
```

- [ ] **Step 3：实现 `src/zhongzhuan/store/store.py`**

```python
"""SQLite 存储。"""
from __future__ import annotations

import sqlite3
import threading
import time

from .schema import SCHEMA


class Store:
    """单文件 SQLite store（WAL 模式）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._open()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @staticmethod
    def now() -> int:
        return int(time.time())
```

- [ ] **Step 4：写 `src/zhongzhuan/store/__init__.py`**

```python
from .store import Store

__all__ = ["Store"]
```

- [ ] **Step 5：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_store.py -v
```

- [ ] **Step 6：commit**

```bash
git add -A
git commit -m "feat(store): SQLite open + schema (M3.1)"
```

---

### Task 3.2：模型与 Key 的 CRUD + crypto stub

**Files:**
- Create: `src/zhongzhuan/crypto/__init__.py`
- Create: `src/zhongzhuan/crypto/dpapi_windows.py`
- Create: `src/zhongzhuan/store/models.py`
- Create: `src/zhongzhuan/store/keys.py`
- Create: `tests/test_models_crud.py`
- Create: `tests/test_keys_crud.py`

- [ ] **Step 1：写测试 `tests/test_models_crud.py`**

```python
"""Model CRUD 测试。"""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

from zhongzhuan.store import Store
from zhongzhuan.store.models import (
    Model, create_model, get_model, list_models, update_model, delete_model,
)


def test_model_crud(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    try:
        m = create_model(s, Model(name="gpt-4o", upstream_base="https://x", upstream_model="gpt-4o"))
        assert m.id and m.id > 0
        got = get_model(s, "gpt-4o")
        assert got is not None
        assert got.name == "gpt-4o"
        update_model(s, m.id, Model(
            name="gpt-4o", upstream_base="https://x", upstream_model="gpt-4o-renamed",
            rpm_limit=100,
        ))
        got2 = get_model(s, "gpt-4o")
        assert got2 is not None
        assert got2.upstream_model == "gpt-4o-renamed"
        assert got2.rpm_limit == 100
        models = list_models(s)
        assert any(x.id == m.id for x in models)
        delete_model(s, m.id)
        assert get_model(s, "gpt-4o") is None
    finally:
        s.close()
```

- [ ] **Step 2：写测试 `tests/test_keys_crud.py`**

```python
"""Key CRUD 测试。"""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

from zhongzhuan.store import Store
from zhongzhuan.store.models import Model, create_model
from zhongzhuan.store.keys import ApiKey, create_key, list_keys, get_key_cipher, delete_key


def test_key_crud(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    try:
        m = create_model(s, Model(name="m1", upstream_base="http://x", upstream_model="m1"))
        k = create_key(s, ApiKey(id=None, model_id=m.id, label="L", key_value="sk-abc"))
        assert k.id and k.id > 0
        rows = list_keys(s, m.id)
        assert len(rows) == 1
        assert rows[0].key_masked.startswith("sk-")
        plain = get_key_cipher(s, k.id)
        assert plain == "sk-abc"
        delete_key(s, k.id)
        assert list_keys(s, m.id) == []
    finally:
        s.close()
```

- [ ] **Step 3：实现 `src/zhongzhuan/crypto/__init__.py`**

```python
"""Key 加密/解密：Windows DPAPI；其他平台/dev 走 base64 stub。"""
from __future__ import annotations

import base64
import os
import sys


def _is_dev_stub() -> bool:
    if os.environ.get("ZHONGZHUAN_DEV_NO_DPAPI") == "1":
        return True
    return sys.platform != "win32"


def encrypt(plaintext: bytes) -> bytes:
    if _is_dev_stub():
        return b"DEV:" + base64.b64encode(plaintext)
    from .dpapi_windows import dpapi_protect
    return dpapi_protect(plaintext)


def decrypt(ciphertext: bytes) -> bytes:
    if ciphertext.startswith(b"DEV:"):
        return base64.b64decode(ciphertext[4:])
    from .dpapi_windows import dpapi_unprotect
    return dpapi_unprotect(ciphertext)


def mask(plaintext: str) -> str:
    """遮显 key：保留前 4 + 后 4，中间 ***。"""
    if len(plaintext) <= 8:
        return "***"
    return f"{plaintext[:4]}***{plaintext[-4:]}"
```

- [ ] **Step 4：实现 `src/zhongzhuan/crypto/dpapi_windows.py`（先 stub，M6 替换 ctypes 真实现）**

```python
"""Windows DPAPI 加密。M6 替换为 ctypes 真实现。"""


def dpapi_protect(plaintext: bytes) -> bytes:
    raise NotImplementedError("DPAPI 待 M6 实现")


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    raise NotImplementedError("DPAPI 待 M6 实现")
```

- [ ] **Step 5：实现 `src/zhongzhuan/store/models.py`**

```python
"""Model CRUD。"""
from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass
class Model:
    name: str
    upstream_base: str
    upstream_model: str
    rpm_limit: int = 0
    tpm_limit: int = 0
    enabled: bool = True
    weight: int = 1
    id: int | None = None
    created_at: int | None = None
    updated_at: int | None = None


def _row(r: tuple) -> Model:
    return Model(
        id=r[0], name=r[1], upstream_base=r[2], upstream_model=r[3],
        rpm_limit=r[4], tpm_limit=r[5], enabled=bool(r[6]), weight=r[7],
        created_at=r[8], updated_at=r[9],
    )


def create_model(s: Store, m: Model) -> Model:
    now = Store.now()
    cur = s.connect().execute(
        """INSERT INTO models(name, upstream_base, upstream_model, rpm_limit, tpm_limit, enabled, weight, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (m.name, m.upstream_base, m.upstream_model, m.rpm_limit, m.tpm_limit,
         int(m.enabled), m.weight, now, now),
    )
    m.id = cur.lastrowid
    m.created_at = now
    m.updated_at = now
    return m


def get_model(s: Store, name: str) -> Model | None:
    r = s.connect().execute(
        "SELECT id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,created_at,updated_at FROM models WHERE name=?",
        (name,),
    ).fetchone()
    return _row(r) if r else None


def get_model_by_id(s: Store, model_id: int) -> Model | None:
    r = s.connect().execute(
        "SELECT id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,created_at,updated_at FROM models WHERE id=?",
        (model_id,),
    ).fetchone()
    return _row(r) if r else None


def list_models(s: Store) -> list[Model]:
    rows = s.connect().execute(
        "SELECT id,name,upstream_base,upstream_model,rpm_limit,tpm_limit,enabled,weight,created_at,updated_at FROM models ORDER BY id"
    ).fetchall()
    return [_row(r) for r in rows]


def update_model(s: Store, model_id: int, m: Model) -> None:
    now = Store.now()
    s.connect().execute(
        """UPDATE models SET name=?, upstream_base=?, upstream_model=?, rpm_limit=?, tpm_limit=?, enabled=?, weight=?, updated_at=? WHERE id=?""",
        (m.name, m.upstream_base, m.upstream_model, m.rpm_limit, m.tpm_limit,
         int(m.enabled), m.weight, now, model_id),
    )


def delete_model(s: Store, model_id: int) -> None:
    s.connect().execute("DELETE FROM models WHERE id=?", (model_id,))
```

- [ ] **Step 6：实现 `src/zhongzhuan/store/keys.py`**

```python
"""API Key CRUD。"""
from __future__ import annotations

from dataclasses import dataclass

from ..crypto import encrypt, decrypt, mask
from .store import Store


@dataclass
class ApiKey:
    id: int | None
    model_id: int
    label: str
    key_value: str
    enabled: bool = True
    priority: int = 0
    created_at: int | None = None


@dataclass
class ApiKeyRow:
    id: int
    model_id: int
    label: str
    key_masked: str
    enabled: bool
    priority: int
    created_at: int


def create_key(s: Store, k: ApiKey) -> ApiKey:
    cipher = encrypt(k.key_value.encode("utf-8"))
    now = Store.now()
    cur = s.connect().execute(
        "INSERT INTO api_keys(model_id, label, key_cipher, enabled, priority, created_at) VALUES(?,?,?,?,?,?)",
        (k.model_id, k.label, cipher, int(k.enabled), k.priority, now),
    )
    k.id = cur.lastrowid
    k.created_at = now
    return k


def list_keys(s: Store, model_id: int | None = None) -> list[ApiKeyRow]:
    conn = s.connect()
    if model_id is None:
        cur = conn.execute("SELECT id,model_id,label,key_cipher,enabled,priority,created_at FROM api_keys ORDER BY id")
    else:
        cur = conn.execute(
            "SELECT id,model_id,label,key_cipher,enabled,priority,created_at FROM api_keys WHERE model_id=? ORDER BY id",
            (model_id,),
        )
    out = []
    for row in cur.fetchall():
        plain = decrypt(row[3]).decode("utf-8", errors="replace")
        out.append(ApiKeyRow(
            id=row[0], model_id=row[1], label=row[2], key_masked=mask(plain),
            enabled=bool(row[4]), priority=row[5], created_at=row[6],
        ))
    return out


def get_key_cipher(s: Store, key_id: int) -> str | None:
    r = s.connect().execute("SELECT key_cipher FROM api_keys WHERE id=?", (key_id,)).fetchone()
    return decrypt(r[0]).decode("utf-8") if r else None


def delete_key(s: Store, key_id: int) -> None:
    s.connect().execute("DELETE FROM api_keys WHERE id=?", (key_id,))


def update_key(s: Store, key_id: int, *, label: str | None = None, enabled: bool | None = None, priority: int | None = None) -> None:
    sets, params = [], []
    if label is not None:
        sets.append("label=?"); params.append(label)
    if enabled is not None:
        sets.append("enabled=?"); params.append(int(enabled))
    if priority is not None:
        sets.append("priority=?"); params.append(priority)
    if not sets:
        return
    params.append(key_id)
    s.connect().execute(f"UPDATE api_keys SET {','.join(sets)} WHERE id=?", params)
```

- [ ] **Step 7：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_models_crud.py tests/test_keys_crud.py -v
```

- [ ] **Step 8：commit**

```bash
git add -A
git commit -m "feat(store,crypto): model + key CRUD + dev stub (M3.2)"
```

---

### Task 3.3：管理 HTTP 服务 + 基础 API

**Files:**
- Create: `src/zhongzhuan/admin/server.py`
- Create: `src/zhongzhuan/admin/api_models.py`
- Create: `src/zhongzhuan/admin/api_keys.py`
- Create: `src/zhongzhuan/admin/api_groups.py` (stub)
- Create: `src/zhongzhuan/admin/api_stats.py` (stub)
- Create: `src/zhongzhuan/admin/api_logs.py` (stub)
- Create: `src/zhongzhuan/admin/api_service.py` (stub)
- Create: `src/zhongzhuan/admin/api_export_import.py` (stub)
- Create: `src/zhongzhuan/admin/ui.py`
- Create: `src/zhongzhuan/admin/__init__.py`
- Create: `tests/test_admin.py`

- [ ] **Step 1：写测试 `tests/test_admin.py`**

```python
"""Admin API 测试。"""
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
```

- [ ] **Step 2：实现 `src/zhongzhuan/admin/server.py`**

```python
"""Admin HTTP 服务。"""
from __future__ import annotations

from aiohttp import web

from ..store import Store
from .api_models import register_routes as register_models
from .api_keys import register_routes as register_keys
from .api_groups import register_routes as register_groups
from .api_stats import register_routes as register_stats
from .api_logs import register_routes as register_logs
from .api_service import register_routes as register_service
from .api_export_import import register_routes as register_export
from .ui import mount_ui


class AdminServer:
    def __init__(self, store: Store, version: str = "0.1.0", config=None) -> None:
        self.store = store
        self.version = version
        self.config = config

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)

        @web.middleware
        async def error_middleware(request, handler):
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as e:  # noqa: BLE001
                return web.json_response(
                    {"error": {"message": str(e), "type": "internal_error"}},
                    status=500,
                )
        app.middlewares.append(error_middleware)

        register_models(app, self)
        register_keys(app, self)
        register_groups(app, self)
        register_stats(app, self)
        register_logs(app, self)
        register_service(app, self)
        register_export(app, self)
        mount_ui(app)
        return app
```

- [ ] **Step 3：实现 `src/zhongzhuan/admin/api_models.py`**

```python
"""模型 CRUD API。"""
from __future__ import annotations

from aiohttp import web

from ..store.models import (
    Model, create_model, get_model, get_model_by_id,
    list_models, update_model, delete_model,
)


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        ms = list_models(ctx.store)
        return web.json_response({"data": [_to_dict(m) for m in ms]})

    async def create(request):
        data = await request.json()
        m = Model(
            name=data["name"], upstream_base=data["upstream_base"],
            upstream_model=data["upstream_model"],
            rpm_limit=int(data.get("rpm_limit", 0)),
            tpm_limit=int(data.get("tpm_limit", 0)),
            enabled=bool(data.get("enabled", True)),
            weight=int(data.get("weight", 1)),
        )
        m = create_model(ctx.store, m)
        return web.json_response(_to_dict(m), status=201)

    async def update(request):
        model_id = int(request.match_info["id"])
        data = await request.json()
        m = Model(
            name=data["name"], upstream_base=data["upstream_base"],
            upstream_model=data["upstream_model"],
            rpm_limit=int(data.get("rpm_limit", 0)),
            tpm_limit=int(data.get("tpm_limit", 0)),
            enabled=bool(data.get("enabled", True)),
            weight=int(data.get("weight", 1)),
        )
        update_model(ctx.store, model_id, m)
        return web.json_response({"ok": True})

    async def delete(request):
        model_id = int(request.match_info["id"])
        delete_model(ctx.store, model_id)
        return web.json_response({"ok": True})

    app.router.add_get("/api/models", list_)
    app.router.add_post("/api/models", create)
    app.router.add_put("/api/models/{id}", update)
    app.router.add_delete("/api/models/{id}", delete)


def _to_dict(m: Model) -> dict:
    return {
        "id": m.id, "name": m.name,
        "upstream_base": m.upstream_base, "upstream_model": m.upstream_model,
        "rpm_limit": m.rpm_limit, "tpm_limit": m.tpm_limit,
        "enabled": m.enabled, "weight": m.weight,
        "created_at": m.created_at, "updated_at": m.updated_at,
    }
```

- [ ] **Step 4：实现 `src/zhongzhuan/admin/api_keys.py`**

```python
"""Key CRUD API。"""
from __future__ import annotations

from aiohttp import web

from ..crypto import mask
from ..store.keys import ApiKey, create_key, list_keys, delete_key, update_key


def register_routes(app: web.Application, ctx) -> None:
    async def list_(request):
        model_id = request.query.get("model_id")
        rows = list_keys(ctx.store, int(model_id) if model_id else None)
        return web.json_response({"data": [
            {
                "id": r.id, "model_id": r.model_id, "label": r.label,
                "key_masked": r.key_masked, "enabled": r.enabled,
                "priority": r.priority, "created_at": r.created_at,
            }
            for r in rows
        ]})

    async def create(request):
        data = await request.json()
        k = ApiKey(
            id=None, model_id=int(data["model_id"]),
            label=data.get("label", ""), key_value=data["key_value"],
            enabled=bool(data.get("enabled", True)),
            priority=int(data.get("priority", 0)),
        )
        k = create_key(ctx.store, k)
        return web.json_response({
            "id": k.id, "model_id": k.model_id, "label": k.label,
            "key_masked": mask(k.key_value), "enabled": k.enabled,
            "priority": k.priority, "created_at": k.created_at,
        }, status=201)

    async def delete(request):
        key_id = int(request.match_info["id"])
        delete_key(ctx.store, key_id)
        return web.json_response({"ok": True})

    async def update(request):
        key_id = int(request.match_info["id"])
        data = await request.json()
        update_key(
            ctx.store, key_id,
            label=data.get("label"),
            enabled=data.get("enabled"),
            priority=data.get("priority"),
        )
        return web.json_response({"ok": True})

    app.router.add_get("/api/keys", list_)
    app.router.add_post("/api/keys", create)
    app.router.add_put("/api/keys/{id}", update)
    app.router.add_delete("/api/keys/{id}", delete)
```

- [ ] **Step 5：写 stub（groups/stats/logs/service/export）**

```python
# src/zhongzhuan/admin/api_groups.py
from aiohttp import web
def register_routes(app, ctx):
    app.router.add_get("/api/groups", lambda r: web.json_response({"data": []}))
```

```python
# src/zhongzhuan/admin/api_stats.py
from aiohttp import web
def register_routes(app, ctx):
    app.router.add_get("/api/stats", lambda r: web.json_response({"qps": 0, "success_rate": 1.0}))
```

```python
# src/zhongzhuan/admin/api_logs.py
from aiohttp import web
def register_routes(app, ctx):
    app.router.add_get("/api/logs", lambda r: web.json_response({"data": []}))
```

```python
# src/zhongzhuan/admin/api_service.py
from aiohttp import web
def register_routes(app, ctx):
    app.router.add_get("/api/service/status", lambda r: web.json_response({"status": "unknown"}))
```

```python
# src/zhongzhuan/admin/api_export_import.py
from aiohttp import web
def register_routes(app, ctx):
    app.router.add_get("/api/export", lambda r: web.json_response({"ok": True}))
```

- [ ] **Step 6：实现 `src/zhongzhuan/admin/ui.py`（M3 占位 UI；M6 替换完整）**

```python
"""Admin UI 静态资源。"""
from __future__ import annotations

from aiohttp import web

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Zhongzhuan</title></head>
<body><h1>Zhongzhuan Admin</h1>
<p>M3 占位 UI；M6 提供完整后台。</p>
</body></html>
"""


def mount_ui(app: web.Application) -> None:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=INDEX_HTML, content_type="text/html")
    app.router.add_get("/", index)
    app.router.add_get("/ui/", index)
```

- [ ] **Step 7：写 `src/zhongzhuan/admin/__init__.py`**

```python
from .server import AdminServer

__all__ = ["AdminServer"]
```

- [ ] **Step 8：跑测试 → 通过**

```bash
cd F:\xiangmu\zhongzhuan
python -m pytest tests/test_admin.py -v
```

- [ ] **Step 9：commit**

```bash
git add -A
git commit -m "feat(admin): model + key CRUD API + ui placeholder (M3.3)"
```

---

### Task 3.4：把 admin 接入 `__main__.py`

**Files:**
- Modify: `src/zhongzhuan/__main__.py`

- [ ] **Step 1：替换 `__main__.py` 启动 admin 端口**

```python
"""命令行入口：python -m zhongzhuan [args]"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

import yaml

from zhongzhuan import __version__
from zhongzhuan.admin import AdminServer
from zhongzhuan.config import default_config, load_config, resolve_data_dir
from zhongzhuan.observability import setup_logging
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.store import Store
from zhongzhuan.upstream import UpstreamClient


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="zhongzhuan", description="OpenAI API relay")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--upstream", default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--service", action="store_true")
    p.add_argument("--version", action="version", version=f"zhongzhuan {__version__}")
    return p.parse_args()


def make_default_config(path: Path) -> None:
    cfg = default_config()
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {
                "server": {
                    "proxy": {"host": cfg.server.proxy.host, "port": cfg.server.proxy.port},
                    "admin": {"host": cfg.server.admin.host, "port": cfg.server.admin.port},
                },
                "limits": {"global_concurrent": cfg.limits.global_concurrent},
                "storage": {"db_path": "data.db", "log_dir": "logs"},
            },
            f, allow_unicode=True, sort_keys=False,
        )


async def run_foreground(
    cfg_path: str, port_override: int | None,
    upstream_url: str | None, key: str | None,
    as_service: bool = False,
) -> int:
    from aiohttp import web
    from loguru import logger

    cfg = load_config(cfg_path)
    if port_override is not None:
        cfg.server.proxy.port = port_override

    data_dir = resolve_data_dir(service_mode=as_service)
    setup_logging(data_dir / cfg.storage.log_dir)
    logger.info("zhongzhuan starting", cfg=str(cfg_path), data_dir=str(data_dir))

    store = Store(str(data_dir / cfg.storage.db_path))

    upstream_base = upstream_url or os.environ.get("ZHONGZHUAN_UPSTREAM", "https://api.openai.com")
    api_key = key or os.environ.get("ZHONGZHUAN_KEY", "")

    upstream = UpstreamClient(base_url=upstream_base, timeout=cfg.limits.proxy_request_timeout)
    await upstream.start()

    keys = []
    if api_key:
        keys.append(KeyHealth(
            key_id=0, api_key=api_key,
            window=SlidingWindow(cfg.limits.per_key_window_seconds, cfg.limits.default_rpm_per_key),
        ))

    proxy = ProxyServer(
        upstream=upstream, keys=keys,
        proxy_timeout=cfg.limits.proxy_request_timeout,
    )
    proxy_runner = web.AppRunner(proxy.app())
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, cfg.server.proxy.host, cfg.server.proxy.port)
    await proxy_site.start()
    logger.info("proxy listening", addr=f"{cfg.server.proxy.host}:{cfg.server.proxy.port}")

    admin = AdminServer(store=store, version=__version__, config=cfg)
    admin_runner = web.AppRunner(admin.app())
    await admin_runner.setup()
    admin_site = web.TCPSite(admin_runner, cfg.server.admin.host, cfg.server.admin.port)
    await admin_site.start()
    logger.info("admin listening", addr=f"{cfg.server.admin.host}:{cfg.server.admin.port}")

    stop = asyncio.Event()

    def _on_signal() -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    if not as_service:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except NotImplementedError:
                pass

    try:
        await stop.wait()
    finally:
        await proxy_runner.cleanup()
        await admin_runner.cleanup()
        await upstream.close()
        store.close()
        logger.info("shutdown complete")
    return 0


def main() -> int:
    args = parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute() and not cfg_path.exists():
        make_default_config(cfg_path)
        print(f"[zhongzhuan] created default config: {cfg_path}", file=sys.stderr)
    return asyncio.run(run_foreground(
        args.config, args.port, args.upstream, args.key,
        as_service=args.service,
    ))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2：本地端到端**

```bash
# 终端 A
cd F:\xiangmu\zhongzhuan
python -m tests.mock_upstream 19999

# 终端 B
cd F:\xiangmu\zhongzhuan
set ZHONGZHUAN_KEY=sk-test
set ZHONGZHUAN_UPSTREAM=http://127.0.0.1:19999
python -m zhongzhuan

# 终端 C
curl -s http://127.0.0.1:8088/healthz
curl -s http://127.0.0.1:8088/v1/models
curl -s -X POST http://127.0.0.1:8088/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"x\"}"
curl -s http://127.0.0.1:8089/
curl -s -X POST http://127.0.0.1:8089/api/models -H "Content-Type: application/json" -d "{\"name\":\"m1\",\"upstream_base\":\"http://x\",\"upstream_model\":\"m1\"}"
curl -s http://127.0.0.1:8089/api/models
```

- [ ] **Step 3：commit**

```bash
git add -A
git commit -m "feat: wire admin server into main (M3.4)"
```

**🎯 M3 验收：** 启动 → Web 后台 8089 → 看到首页；`POST /api/models` + `POST /api/keys` 成功。

---



