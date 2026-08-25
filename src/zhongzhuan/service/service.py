"""Windows service control via sc.exe (Windows) or no-op (Linux)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

if sys.platform == "win32":
    import winreg
else:
    winreg: Any = None


def _sc(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(["sc.exe", *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


# 这些 sc.exe 返回码属于「目标状态已达成」的幂等结果，不算失败：
# 1060 = 服务已安装 / 1062 = 服务未启动 / 1072 = 标记待删除
_SC_IDEMPOTENT_OK = {0, 1060, 1062, 1072}


def _sc_checked(*args: str) -> tuple[int, str, str]:
    """执行 sc.exe 并在非幂等失败时抛错（旧实现丢弃返回码导致假成功——
    非管理员运行 start 得到 Access Denied 也打印 "started"，误导排障）。"""
    code, out, err = _sc(*args)
    if code not in _SC_IDEMPOTENT_OK:
        raise RuntimeError(f"sc {' '.join(args)} failed (exit {code}): {(err or out).strip()}")
    return code, out, err


def install(svc_name: str, display_name: str, auto_start: bool = True) -> None:
    """Register as Windows service (requires admin)."""
    if sys.platform != "win32":
        raise OSError("Windows service not supported on this platform")
    exe = sys.executable
    if getattr(sys, "frozen", False):
        bin_path = f'"{exe}" --service'
    else:
        bin_path = f'"{exe}" -m zhongzhuan --service'

    start_type = "auto" if auto_start else "demand"
    code, out, err = _sc(
        "create", svc_name, f"binPath={bin_path}", f"start={start_type}", f"DisplayName={display_name}"
    )
    if code != 0:
        raise RuntimeError(f"sc create failed: {err}")
    # Set failure actions: restart on failure
    _sc("failure", svc_name, "reset=86400", "actions=restart/5000/restart/10000/restart/30000")

    # 已知限制（如实告知，勿静默）：本进程是控制台程序，未实现 SCM 调度器
    # （StartServiceCtrlDispatcher），SCM 启动后约 30s 会因等待超时报错 1053
    # 并终止进程。生产部署请改用任务计划程序或 NSSM 包装。
    print(
        "[WARN] Windows service mode is best-effort: this console app does not\n"
        "       implement the SCM dispatcher, so scm may kill it after ~30s\n"
        "       (error 1053). Prefer Task Scheduler or NSSM for production."
    )


def uninstall(svc_name: str) -> None:
    """Remove Windows service."""
    if sys.platform != "win32":
        return
    _sc_checked("stop", svc_name)
    _sc_checked("delete", svc_name)


def start(svc_name: str) -> None:
    if sys.platform != "win32":
        return
    _sc_checked("start", svc_name)


def stop(svc_name: str) -> None:
    if sys.platform != "win32":
        return
    _sc_checked("stop", svc_name)


def status(svc_name: str) -> str:
    """Return 'running', 'stopped', or 'not_installed'."""
    if sys.platform != "win32":
        return "not_installed"
    code, out, _ = _sc("query", svc_name)
    if code != 0:
        return "not_installed"
    # 用 STATE : <num> <NAME> 结构化匹配，避免非英文 locale 的子串误判
    import re as _re

    m = _re.search(r"STATE\s*:\s*\d+\s+(\w+)", out)
    state = m.group(1).upper() if m else ""
    if state == "RUNNING":
        return "running"
    if state == "STOPPED":
        return "stopped"
    return "unknown"


def set_autostart(svc_name: str, enabled: bool) -> None:
    if sys.platform != "win32":
        return
    start_type = "auto" if enabled else "demand"
    _sc("config", svc_name, f"start={start_type}")


def register_user_autostart(exe_path: str, svc_name: str) -> None:
    """Register HKCU Run key for user-level auto-start (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, svc_name, 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
    except Exception as e:
        raise RuntimeError(f"Failed to register HKCU Run: {e}")


def unregister_user_autostart(svc_name: str) -> None:
    """Remove HKCU Run key (Windows only)."""
    if sys.platform != "win32":
        return
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, svc_name)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass
    except Exception as e:
        raise RuntimeError(f"Failed to unregister HKCU Run: {e}")
