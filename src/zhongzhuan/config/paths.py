"""Cross-platform path resolution."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def exe_dir() -> Path:
    """PyInstaller --onefile extract dir / script dir."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def resolve_data_dir(service_mode: bool = False) -> Path:
    """Pick data dir by platform and mode.

    Priority:
    1. ZHONGZHUAN_DATA_DIR env var
    2. Linux service mode: /var/lib/zhongzhuan
    3. Linux user mode: ~/.local/share/zhongzhuan
    4. Windows service mode: %ProgramData%/Zhongzhuan
    5. Windows user mode: exe dir
    6. Fallback: %APPDATA%/Zhongzhuan
    """
    env_dir = os.environ.get("ZHONGZHUAN_DATA_DIR")
    if env_dir:
        d = Path(env_dir)
    elif sys.platform != "win32":
        if service_mode:
            d = Path("/var/lib/zhongzhuan")
        else:
            xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
            d = Path(xdg) / "zhongzhuan"
    else:
        if service_mode:
            base = os.environ.get("ProgramData", "C:\\ProgramData")
            d = Path(base) / "Zhongzhuan"
        else:
            d = exe_dir()

    d.mkdir(parents=True, exist_ok=True)
    test = d / ".write_test"
    try:
        test.write_text("ok")
        test.unlink()
    except OSError:
        if sys.platform == "win32":
            d = Path(os.environ.get("APPDATA", str(exe_dir()))) / "Zhongzhuan"
        else:
            d = Path(os.path.expanduser("~/.zhongzhuan"))
        d.mkdir(parents=True, exist_ok=True)
    return d


def is_admin() -> bool:
    """Check if running as admin on Windows."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False