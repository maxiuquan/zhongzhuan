"""CLI entry: python -m zhongzhuan [args]"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

import yaml

from zhongzhuan import __version__
from zhongzhuan.admin import AdminServer
from zhongzhuan.config import default_config, load_config, resolve_data_dir, is_admin
from zhongzhuan.observability import setup_logging
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.store import Store
from zhongzhuan.store.store import create_store
from zhongzhuan.store.keys import list_keys, get_key_cipher
from zhongzhuan.store.models import list_models, get_model_by_id
from zhongzhuan.upstream import UpstreamClient


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="zhongzhuan", description="OpenAI API relay")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--upstream", default=None)
    p.add_argument("--key", default=None)
    p.add_argument("--service", action="store_true", help="Windows Service entry")
    p.add_argument("--version", action="version", version=f"zhongzhuan {__version__}")
    # Service commands
    p.add_argument("--install", action="store_true", help="Install Windows service")
    p.add_argument("--uninstall", action="store_true", help="Uninstall Windows service")
    p.add_argument("--start", action="store_true", help="Start Windows service")
    p.add_argument("--stop", action="store_true", help="Stop Windows service")
    p.add_argument("--autostart", nargs="?", const="status", help="Auto-start on/off/status")
    p.add_argument("--open-admin", action="store_true", help="Open admin UI in browser")
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
                "limits": {
                    "global_concurrent": cfg.limits.global_concurrent,
                    "default_rpm_per_key": cfg.limits.default_rpm_per_key,
                },
                "storage": {"db_path": "data.db", "log_dir": "logs"},
            },
            f, allow_unicode=True, sort_keys=False,
        )


async def _load_keys_from_store(store: Store, cfg) -> list[KeyHealth]:
    """Load keys from DB into KeyHealth objects."""
    key_rows = await list_keys(store)
    health_list: list[KeyHealth] = []
    for kr in key_rows:
        if not kr.enabled:
            continue
        plain = await get_key_cipher(store, kr.id)
        if not plain:
            continue
        model = await get_model_by_id(store, kr.model_id)
        rpm_limit = model.rpm_limit if model and model.rpm_limit > 0 else cfg.limits.default_rpm_per_key
        health_list.append(KeyHealth(
            key_id=kr.id, api_key=plain,
            window=SlidingWindow(cfg.limits.per_key_window_seconds, rpm_limit),
            rpm_limit=rpm_limit,
            upstream_base=model.upstream_base if model else "",
            upstream_model=model.upstream_model if model else "",
            model_name=model.name if model else "",
        ))
    return health_list


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
    logger.info(f"zhongzhuan {__version__} starting", cfg=str(cfg_path), data_dir=str(data_dir))

    # Create async store (TiDB or SQLite based on config)
    store = await create_store(cfg)

    # Initialize crypto with store (for AES key in TiDB system_config)
    from zhongzhuan.crypto import init as crypto_init
    async def _get_config(key_name: str) -> str | None:
        row = await store.fetchone(
            "SELECT value FROM system_config WHERE `key`=?", (key_name,)
        )
        return row[0] if row else None
    await crypto_init(data_dir, store_get_key=_get_config)

    # Create default admin user if auth is enabled and no admin exists
    from zhongzhuan.admin.auth import auth_enabled
    if auth_enabled():
        from zhongzhuan.store.admin_users import admin_exists, create_admin
        if not await admin_exists(store):
            admin_user = os.getenv("ZHONGZHUAN_ADMIN_USER", "admin")
            admin_pass = os.getenv("ZHONGZHUAN_ADMIN_PASSWORD", "")
            if not admin_pass:
                logger.warning("ZHONGZHUAN_ADMIN_PASSWORD not set in .env, admin will not be created")
            else:
                await create_admin(store, admin_user, admin_pass)
                logger.info(f"默认管理员已创建: {admin_user}")

    # Create default access token if proxy auth is enabled and no tokens exist
    from zhongzhuan.proxy.auth import proxy_auth_enabled
    if proxy_auth_enabled():
        from zhongzhuan.store.access_tokens import token_count, create_token as create_access_token
        if await token_count(store) == 0:
            token = await create_access_token(store, "default")
            logger.info(f"自动生成访问令牌: {token.token}")

    # Build keys from DB (with per-model upstream info)
    keys = await _load_keys_from_store(store, cfg)

    # Fallback: env/CLI key
    if not keys:
        api_key = key or os.environ.get("ZHONGZHUAN_KEY", "")
        if api_key:
            upstream_base = upstream_url or os.environ.get("ZHONGZHUAN_UPSTREAM", "https://api.openai.com")
            keys.append(KeyHealth(
                key_id=0, api_key=api_key,
                window=SlidingWindow(cfg.limits.per_key_window_seconds, cfg.limits.default_rpm_per_key),
                rpm_limit=cfg.limits.default_rpm_per_key,
                upstream_base=upstream_base,
                upstream_model="",
            ))

    # If no keys at all, use a dummy to avoid crash
    if not keys:
        api_key = key or os.environ.get("ZHONGZHUAN_KEY", "dummy-key-no-auth")
        upstream_base = upstream_url or os.environ.get("ZHONGZHUAN_UPSTREAM", "https://api.openai.com")
        keys.append(KeyHealth(
            key_id=0, api_key=api_key,
            window=SlidingWindow(cfg.limits.per_key_window_seconds, cfg.limits.default_rpm_per_key),
            rpm_limit=cfg.limits.default_rpm_per_key,
            upstream_base=upstream_base,
            upstream_model="",
        ))

    # Build upstream clients dict: one client per unique upstream_base
    upstream_urls: set[str] = set()
    for k in keys:
        if k.upstream_base:
            upstream_urls.add(k.upstream_base)
    if not upstream_urls:
        upstream_urls.add(upstream_url or os.environ.get("ZHONGZHUAN_UPSTREAM", "https://api.openai.com"))

    from loguru import logger
    logger.info(f"loaded {len(keys)} keys, {len(upstream_urls)} upstreams")

    upstream_clients: dict[str, UpstreamClient] = {}
    for base_url in upstream_urls:
        client = UpstreamClient(base_url=base_url, timeout=cfg.limits.proxy_request_timeout)
        await client.start()
        upstream_clients[base_url] = client

    # Load models and groups for /v1/models
    models_data = [{"name": m.name} for m in await list_models(store)]
    from zhongzhuan.store.groups import list_groups as list_groups_db
    groups_data = [{"name": g["name"]} for g in await list_groups_db(store)]

    proxy = ProxyServer(
        upstream_clients=upstream_clients, keys=keys,
        proxy_timeout=cfg.limits.proxy_request_timeout,
        models=models_data, groups=groups_data, store=store,
        load_keys_fn=lambda: _load_keys_from_store(store, cfg),
    )
    proxy_runner = web.AppRunner(proxy.app())
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, cfg.server.proxy.host, cfg.server.proxy.port)
    await proxy_site.start()
    logger.info(f"proxy listening on {cfg.server.proxy.host}:{cfg.server.proxy.port}")

    admin = AdminServer(store=store, version=__version__, config=cfg)
    admin_runner = web.AppRunner(admin.app())
    await admin_runner.setup()
    admin_site = web.TCPSite(admin_runner, cfg.server.admin.host, cfg.server.admin.port)
    await admin_site.start()
    logger.info(f"admin listening on {cfg.server.admin.host}:{cfg.server.admin.port}")

    # Open browser in foreground mode
    if not as_service:
        try:
            webbrowser.open(f"http://127.0.0.1:{cfg.server.admin.port}")
        except Exception:
            pass

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    if not as_service:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except NotImplementedError:
                pass

    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down...")
        await proxy_runner.cleanup()
        await admin_runner.cleanup()
        for client in upstream_clients.values():
            await client.close()
        await store.close()
        logger.info("shutdown complete")
    return 0


def handle_service_commands(args: argparse.Namespace) -> int | None:
    """Handle install/uninstall/start/stop/autostart. Returns None if no service command."""
    if sys.platform != "win32":
        print("Service commands are only supported on Windows.", file=sys.stderr)
        return 0

    from zhongzhuan.config import load_config
    cfg = load_config(args.config)
    svc_name = cfg.windows_service.service_name
    display_name = cfg.windows_service.display_name

    from zhongzhuan.service import (
        install, uninstall, start, stop, status, set_autostart,
    )

    if args.install:
        print(f"Installing service '{svc_name}'...")
        install(svc_name, display_name, cfg.windows_service.auto_start)
        print(f"Service '{svc_name}' installed.")
        return 0

    if args.uninstall:
        print(f"Uninstalling service '{svc_name}'...")
        uninstall(svc_name)
        print(f"Service '{svc_name}' uninstalled.")
        return 0

    if args.start:
        print(f"Starting service '{svc_name}'...")
        start(svc_name)
        print(f"Service '{svc_name}' started.")
        return 0

    if args.stop:
        print(f"Stopping service '{svc_name}'...")
        stop(svc_name)
        print(f"Service '{svc_name}' stopped.")
        return 0

    if args.autostart is not None:
        if args.autostart == "status":
            print(f"Service '{svc_name}' status: {status(svc_name)}")
            return 0
        enabled = args.autostart.lower() == "on"
        set_autostart(svc_name, enabled)
        print(f"Auto-start for '{svc_name}': {'ON' if enabled else 'OFF'}")
        return 0

    if args.open_admin:
        admin_port = cfg.server.admin.port
        webbrowser.open(f"http://127.0.0.1:{admin_port}")
        return 0

    return None


def main() -> int:
    args = parse_args()

    # Handle service commands
    result = handle_service_commands(args)
    if result is not None:
        return result

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