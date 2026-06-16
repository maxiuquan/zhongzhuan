"""Windows path resolution."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def exe_dir() -> Path:
    """PyInstaller --onefile extract dir / script dir."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def program_data_dir() -> Path:
    """%ProgramData%\\Zhongzhuan."""
    base = os.environ.get("ProgramData", "C:\\ProgramData")
    return Path(base) / "Zhongzhuan"


def is_admin() -> bool:
    """Check if running as admin on Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resolve_data_dir(service_mode: bool) -> Path:
    """Pick data dir by mode; fallback to %APPDATA% if unwritable."""
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