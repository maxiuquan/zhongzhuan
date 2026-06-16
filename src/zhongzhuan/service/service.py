"""Windows service control via sc.exe."""
from __future__ import annotations

import subprocess
import sys
import winreg


def _sc(*args: str) -> tuple[int, str, str]:
    r = subprocess.run(["sc.exe", *args], capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def install(svc_name: str, display_name: str, auto_start: bool = True) -> None:
    """Register as Windows service (requires admin)."""
    exe = sys.executable
    if getattr(sys, "frozen", False):
        bin_path = f'"{exe}" --service'
    else:
        bin_path = f'"{exe}" -m zhongzhuan --service'

    start_type = "auto" if auto_start else "demand"
    code, out, err = _sc("create", svc_name, f"binPath={bin_path}", f"start={start_type}", f"DisplayName={display_name}")
    if code != 0:
        raise RuntimeError(f"sc create failed: {err}")
    # Set failure actions: restart on failure
    _sc("failure", svc_name, "reset=86400", "actions=restart/5000/restart/10000/restart/30000")


def uninstall(svc_name: str) -> None:
    """Remove Windows service."""
    _sc("stop", svc_name)
    _sc("delete", svc_name)


def start(svc_name: str) -> None:
    _sc("start", svc_name)


def stop(svc_name: str) -> None:
    _sc("stop", svc_name)


def status(svc_name: str) -> str:
    """Return 'running', 'stopped', or 'not_installed'."""
    code, out, _ = _sc("query", svc_name)
    if code != 0:
        return "not_installed"
    if "RUNNING" in out:
        return "running"
    if "STOPPED" in out:
        return "stopped"
    return "unknown"


def set_autostart(svc_name: str, enabled: bool) -> None:
    start_type = "auto" if enabled else "demand"
    _sc("config", svc_name, f"start={start_type}")


def register_user_autostart(exe_path: str, svc_name: str) -> None:
    """Register HKCU Run key for user-level auto-start (green version)."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, svc_name, 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
    except Exception as e:
        raise RuntimeError(f"Failed to register HKCU Run: {e}")


def unregister_user_autostart(svc_name: str) -> None:
    """Remove HKCU Run key."""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key, svc_name)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass
    except Exception as e:
        raise RuntimeError(f"Failed to unregister HKCU Run: {e}")