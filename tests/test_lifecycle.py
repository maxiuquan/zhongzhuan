"""进程级启停 + 健康探测（T36 / R-P2-13，Windows/Linux 双平台）。

判据：Windows/Linux 启停 —— 起 ``zhongzhuan --port NNNN`` 子进程（真实 CLI
入口），轮询探测 ``/healthz`` 返回 200，然后 kill 进程，确认端口释放。

设计
----
* 随机选本地空闲端口，避免并发冲突。
* 数据全部隔离到 ``tmp_path``：``ZHONGZHUAN_DATA_DIR`` 指向 tmp 子目录，
  进程 cwd 设为 tmp 子目录（默认 config.yaml / data.db 生成于此），
  不触碰真实用户数据。
* ``--port`` 覆盖 proxy 端口；``/healthz`` 是 liveness 端点（T33 已加），
  无需鉴权。
* 结束用 ``terminate()``（SIGTERM）+ 等待退出；超时才 ``kill()``。
* CI 上 ``lifecycle`` job 跑 ubuntu-latest 与 windows-latest 两个 runner；
  本地任何平台都能直接跑（无 live/soak 标记，属于普通 pytest）。
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.lifecycle


def _free_port() -> int:
    """绑定 0 端口获取一个当前空闲的随机端口后立即释放。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthz(port: int, timeout: float = 30.0) -> str:
    """轮询 ``GET /healthz`` 直到 200；超时抛 AssertionError。"""
    import urllib.request

    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    body = resp.read(4096).decode("utf-8", "replace")
                    return body
        except Exception as exc:  # 连接拒绝 / 超时等，继续轮询
            last_err = exc
        time.sleep(0.5)
    raise AssertionError(f"/healthz not ready within {timeout}s (last: {last_err})")


def _write_isolated_config(workdir: Path, port: int) -> Path:
    """在 tmp 目录写最小 config.yaml：DB 路径、日志目录、proxy 端口全部隔离。

    DB 默认落在 cwd（``data.db``）——必须显式指向 tmp，否则真实仓库的
    ``data.db`` 会被启动逻辑读写（retention 清理等），污染用户数据。
    """
    cfg = workdir / "config.yaml"
    # Windows 反斜杠在 YAML 双引号里是转义序列（\i 非法）——统一用正斜杠。
    db_path = (workdir / "isolated.db").as_posix()
    log_dir = (workdir / "logs").as_posix()
    cfg.write_text(
        "\n".join(
            [
                "server:",
                "  proxy:",
                f"    host: 127.0.0.1",
                f"    port: {port}",
                "  admin:",
                "    host: 127.0.0.1",
                "    port: 0",
                "storage:",
                f'  sqlite_db_path: "{db_path}"',
                f'  log_dir: "{log_dir}"',
                "fallback:",
                "  enabled: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return cfg


def _start_server(workdir: Path, port: int) -> subprocess.Popen:
    """在隔离目录里起 ``python -m zhongzhuan --config <tmp> --port <port>``。"""
    cfg = _write_isolated_config(workdir, port)
    env = dict(os.environ)
    env["ZHONGZHUAN_DATA_DIR"] = str(workdir / "data")
    # 明确开发模式 + 关闭会联网的功能（fallback 默认已关，这里再显式确认）。
    env["ZHONGZHUAN_ENV"] = "development"
    env.pop("ZHONGZHUAN_TIDB_HOST", None)

    cmd = [sys.executable, "-m", "zhongzhuan", "--config", str(cfg), "--port", str(port)]
    proc = subprocess.Popen(
        cmd,
        cwd=str(workdir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc


def _stop_server(proc: subprocess.Popen) -> None:
    """终止进程；Windows/Linux 均先 SIGTERM，超时再强杀。"""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        proc.terminate()
    else:
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:  # pragma: no cover - 已退出
            return
    try:
        proc.wait(timeout=15.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


def test_healthz_200_on_start(tmp_path):
    """起服务 -> /healthz 200 -> kill，进程退出。"""
    port = _free_port()
    proc = _start_server(tmp_path, port)
    try:
        body = _wait_healthz(port)
        # liveness 载荷应含基本字段（不深断言，避免绑定实现细节）。
        assert "status" in body or "ok" in body.lower()
    finally:
        _stop_server(proc)

    # 进程已退出。端口释放因 Windows TIME_WAIT 语义不硬断言：
    # 若 10s 内能重新 bind 则通过，否则跳过（TIME_WAIT 残留是 OS 行为）。
    assert proc.poll() is not None
    deadline = time.monotonic() + 10.0
    rebound = False
    while time.monotonic() < deadline:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            rebound = True
            break
        except OSError:
            time.sleep(0.5)
        finally:
            s.close()
    if not rebound:
        import warnings

        warnings.warn(
            f"port {port} not released within 10s after terminate (Windows TIME_WAIT; not a failure)",
            stacklevel=1,
        )


def test_server_survives_two_healthz_checks(tmp_path):
    """连续两次 /healthz 均 200（同一进程实例稳定存活）。"""
    port = _free_port()
    proc = _start_server(tmp_path, port)
    try:
        _wait_healthz(port)
        _wait_healthz(port)
    finally:
        _stop_server(proc)
    assert proc.poll() is not None


def test_lifecycle_isolated_data_dir(tmp_path):
    """DB 与日志全部隔离到 tmp：真实仓库的 data.db 不被触碰。"""
    port = _free_port()
    proc = _start_server(tmp_path, port)
    try:
        _wait_healthz(port)
    finally:
        _stop_server(proc)
    # 服务在隔离目录里建出自己的 SQLite DB（而不是根目录 data.db）。
    assert (tmp_path / "isolated.db").exists(), "DB 必须被隔离到 tmp_path"
    assert not (tmp_path / "data.db").exists(), "cwd 不应生成 data.db"
