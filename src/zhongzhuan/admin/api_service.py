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
        capture_output=True, text=True,
    )
    return r.returncode, r.stdout, r.stderr


def _check_admin() -> tuple[int, dict] | None:
    if not is_admin():
        return 403, {"error": {"message": "admin privileges required", "type": "forbidden"}}
    return None


def register_routes(app: web.Application, ctx) -> None:
    svc_name = "Zhongzhuan"
    if ctx.config and hasattr(ctx.config, "windows_service"):
        svc_name = ctx.config.windows_service.service_name

    async def status(_request):
        code, out, _ = _sc("query", svc_name)
        if code != 0:
            return web.json_response({"status": "not_installed"})
        if "RUNNING" in out:
            return web.json_response({"status": "running"})
        if "STOPPED" in out:
            return web.json_response({"status": "stopped"})
        return web.json_response({"status": "unknown"})

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