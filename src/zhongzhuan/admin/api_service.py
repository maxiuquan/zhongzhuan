"""Service control API (sc.exe wrapper)."""

from __future__ import annotations

import asyncio
import sys

from aiohttp import web

from ..config import is_admin


async def _sc(*args: str) -> tuple[int, str, str]:
    """Run sc.exe command, return (code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "sc.exe",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    code = proc.returncode if proc.returncode is not None else -1
    return code, out.decode(errors="replace"), err.decode(errors="replace")


def _check_admin() -> tuple[int, dict] | None:
    if not is_admin():
        return 403, {"error": {"message": "admin privileges required", "type": "forbidden"}}
    return None


async def _check_confirm(request: web.Request) -> web.Response | None:
    """破坏性/服务生命周期操作必须显式携带 {"confirm": true}，防误触。"""
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict) or data.get("confirm") is not True:
        return web.json_response(
            {"error": {"message": "missing {\"confirm\": true} in body", "type": "confirm_required"}},
            status=400,
        )
    return None


def _service_status(svc_name: str) -> dict:
    """非 Windows 快速路径：admin HTTP 由常驻进程承载，能收到请求即证明服务在运行。

    Service lifecycle controls remain Windows-only. Windows 的真实状态查询走
    :func:`_service_status_async`（需要 await 异步 sc.exe）。
    """
    if sys.platform != "win32":
        return {"status": "running", "control_supported": False}
    return {"status": "unknown", "control_supported": True}


async def _service_status_async(svc_name: str) -> dict:
    """Windows 平台真实状态查询（异步调用 ``sc.exe query``）。"""
    code, out, _ = await _sc("query", svc_name)
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
        if sys.platform != "win32":
            return web.json_response(_service_status(svc_name))
        return web.json_response(await _service_status_async(svc_name))

    async def start(request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        if err := await _check_confirm(request):
            return err
        await _sc("start", svc_name)
        return web.json_response({"ok": True})

    async def stop(request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        if err := await _check_confirm(request):
            return err
        await _sc("stop", svc_name)
        return web.json_response({"ok": True})

    async def autostart(request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        data = await request.json()
        enabled = data.get("enabled", True)
        start_type = "auto" if enabled else "demand"
        await _sc("config", svc_name, f"start={start_type}")
        return web.json_response({"ok": True, "auto_start": enabled})

    async def install(request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        if err := await _check_confirm(request):
            return err
        exe = sys.executable
        # binPath 含空格（如 C:\Program Files\...）时必须整体加引号，
        # sc.exe 才能正确解析可执行路径与参数。
        await _sc("create", svc_name, f'binPath="{exe} --service"', "start=auto")
        return web.json_response({"ok": True})

    async def uninstall(request):
        if err := _check_admin():
            return web.json_response(err[1], status=err[0])
        if err := await _check_confirm(request):
            return err
        await _sc("delete", svc_name)
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
