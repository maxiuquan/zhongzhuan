"""Service control API (sc.exe wrapper)."""

from __future__ import annotations

import subprocess
import sys

from aiohttp import web

from ..config import is_admin


def _sc(*args: str) -> tuple[int, str, str]:
    """Run sc.exe command, return (code, stdout, stderr)."""
    r = subprocess.run(
        ["sc.exe", *args],
        capture_output=True,
        text=True,
    )
    return r.returncode, r.stdout, r.stderr


def _check_admin() -> tuple[int, dict] | None:
    if not is_admin():
        return 403, {"error": {"message": "admin privileges required", "type": "forbidden"}}
    return None


def _service_status(svc_name: str) -> dict:
    """Return service status without invoking Windows tools on other platforms.

    On Linux/macOS the admin HTTP handler is hosted by the running relay
    process itself, so a successful request is sufficient proof that the
    service is running. Service lifecycle controls remain Windows-only.
    """
    if sys.platform != "win32":
        return {"status": "running", "control_supported": False}

    code, out, _ = _sc("query", svc_name)
    if code != 0:
        return {"status": "not_installed", "control_supported": True}
    if "RUNNING" in out:
        status = "running"
    elif "STOPPED" in out:
        status = "stopped"
    else:
        status = "unknown"
    return {"status": status, "control_supported": True}


def register_routes(app: web.Application, ctx) -> None:
    svc_name = "Zhongzhuan"
    if ctx.config and hasattr(ctx.config, "windows_service"):
        svc_name = ctx.config.windows_service.service_name

    async def status(_request):
        return web.json_response(_service_status(svc_name))

    async def start(_request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        _sc("start", svc_name)
        return web.json_response({"ok": True})

    async def stop(_request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        _sc("stop", svc_name)
        return web.json_response({"ok": True})

    async def autostart(request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        data = await request.json()
        enabled = data.get("enabled", True)
        start_type = "auto" if enabled else "demand"
        _sc("config", svc_name, f"start={start_type}")
        return web.json_response({"ok": True, "auto_start": enabled})

    async def install(_request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        exe = sys.executable
        _sc("create", svc_name, f"binPath={exe} --service", "start=auto")
        return web.json_response({"ok": True})

    async def uninstall(_request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        _sc("delete", svc_name)
        return web.json_response({"ok": True})

    async def reload(_request):
        # Placeholder: in production this would reload config from DB
        return web.json_response({"ok": True})

    app.router.add_get("/api/service/status", status)
    app.router.add_post("/api/service/start", start)
    app.router.add_post("/api/service/stop", stop)
    app.router.add_post("/api/service/autostart", autostart)
    app.router.add_post("/api/service/install", install)
    app.router.add_post("/api/service/uninstall", uninstall)
    app.router.add_post("/api/reload", reload)
