"""CLI entry: python -m zhongzhuan [args]"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import webbrowser
from pathlib import Path

import yaml

from zhongzhuan import __version__
from zhongzhuan.config import default_config, load_config, resolve_data_dir
from zhongzhuan.observability import setup_logging
from zhongzhuan.proxy import ProxyServer
from zhongzhuan.proxy.ratelimit import KeyHealth, SlidingWindow
from zhongzhuan.store import Store
from zhongzhuan.store.store import create_store
from zhongzhuan.store.keys import list_keys
from zhongzhuan.store.models import list_models
from zhongzhuan.crypto import decrypt
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
    # TLS subcommand
    p.add_argument("--tls-selfsign", action="store_true", help="Generate self-signed TLS certificate")
    p.add_argument("--cn", default="localhost", help="Common Name for self-signed cert")
    p.add_argument("--san-ip", action="append", default=[], help="IP SAN for self-signed cert")
    p.add_argument("--san-dns", action="append", default=[], help="DNS SAN for self-signed cert")
    p.add_argument("--out-cert", default="data/server.crt", help="Output cert file path")
    p.add_argument("--out-key", default="data/server.key", help="Output key file path")
    p.add_argument("--out-ca", default="data/local-ca.crt", help="Output CA file path")
    p.add_argument("--days", type=int, default=3650, help="Certificate validity in days")
    # Convenience flag to run directly without subcommand
    p.add_argument("args", nargs=argparse.REMAINDER)
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
            f,
            allow_unicode=True,
            sort_keys=False,
        )


async def _load_keys_from_store(store: Store, cfg) -> list[KeyHealth]:
    """Load keys from DB into KeyHealth objects.

    优化点4：启动时从 key_health 表恢复之前学到的 status/cooldown/限额。
    批量预取（修复 N+1）：原实现对每个 key 串行执行 get_model_by_id +
    get_key_cipher（各一次 DB 往返），key 数量大时 reload 随 key 数线性变慢
    （~100 key 时 reload 高达 ~5s），进而阻塞后台「保存模型/key」接口。
    这里改为一次 list_models 建字典 + 一次批量取密文本地解密，DB 往返从
    O(keys) 降到 O(1)。
    """
    from zhongzhuan.store.key_health import load_all_health
    from zhongzhuan.proxy.client_presets import parse_custom_headers

    saved_health = await load_all_health(store)

    key_rows = await list_keys(store)
    # 批量预取所有 model，O(1) 查表，替代每 key 一次 get_model_by_id DB 查询
    all_models = {m.id: m for m in await list_models(store)}
    # 一次性取全部密文，本地 decrypt（无 DB 往返），替代每 key 一次 get_key_cipher
    cipher_rows = await store.fetchall("SELECT id, key_cipher FROM api_keys")
    ciphers: dict[int, str | None] = {}
    for _cid, _c in cipher_rows:
        try:
            ciphers[_cid] = decrypt(_c).decode("utf-8")
        except Exception:
            ciphers[_cid] = None

    health_list: list[KeyHealth] = []
    for kr in key_rows:
        if not kr.enabled:
            continue
        plain = ciphers.get(kr.id)
        if not plain:
            continue
        model = all_models.get(kr.model_id)
        rpm_limit = model.rpm_limit if model and model.rpm_limit > 0 else cfg.limits.default_rpm_per_key
        tpm_limit = model.tpm_limit if model and model.tpm_limit > 0 else cfg.limits.default_tpm_per_key
        upstream_base = (model.upstream_base if model else "").replace("`", "").replace('"', "").strip()
        upstream_model = (model.upstream_model if model else "").replace("`", "").replace('"', "").strip()
        model_name = (model.name if model else "").replace("`", "").replace('"', "").strip()
        kh = KeyHealth(
            key_id=kr.id,
            api_key=plain,
            window=SlidingWindow(cfg.limits.per_key_window_seconds, rpm_limit),
            model_id=kr.model_id,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            tpm_window=SlidingWindow(60, tpm_limit) if tpm_limit > 0 else None,
            rpd_limit=cfg.limits.default_rpd_per_key,
            upstream_base=upstream_base,
            upstream_model=upstream_model,
            model_name=model_name,
            upstream_protocol=model.protocol if model else "openai",
            anthropic_version=model.anthropic_version if model else "2023-06-01",
            max_tokens_default=model.max_tokens_default if model else 4096,
            upstream_path_override=model.upstream_path_override if model else "",
            # 兜底标记 + 降权系数从 Model + Config 注入
            is_fallback=bool(model.is_fallback) if model else False,
            fallback_penalty=cfg.fallback.fallback_penalty,
            aliases=model.aliases if model else "",
            # 客户端指纹模拟（v009）：从 Model 映射, custom_headers JSON 解析容错
            client_preset=getattr(model, "client_preset", "") if model else "",
            custom_headers=parse_custom_headers(getattr(model, "custom_headers", "") or "") if model else [],
        )
        # 恢复持久化的健康状态（优化点4）
        if kr.id in saved_health:
            sh = saved_health[kr.id]
            kh.status = sh.status
            kh.cooldown_until = sh.cooldown_until
            kh.success_count = sh.success_count
            kh.total_failures = sh.failure_count
            kh.recent_429_count = sh.recent_429_count
            # 恢复学到的更严格限额
            if sh.rpm_limit > 0 and (kh.rpm_limit == 0 or sh.rpm_limit < kh.rpm_limit):
                kh.rpm_limit = sh.rpm_limit
            if sh.tpm_limit > 0:
                if kh.tpm_limit == 0 or sh.tpm_limit < kh.tpm_limit:
                    kh.tpm_limit = sh.tpm_limit
                if kh.tpm_window is None:
                    kh.tpm_window = SlidingWindow(60, sh.tpm_limit)
                else:
                    kh.tpm_window.limit = sh.tpm_limit
        health_list.append(kh)
    return health_list


# ---- OpenCode Free 兜底上游 ----

# 拉取失败时的默认免费模型列表（兜底中的兜底）
_DEFAULT_FREE_MODELS = [
    "glm-5.2-free",
    "glm-5.1-free",
    "kimi-k2.7-code-free",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
]


async def _fetch_opencode_models(cfg) -> list[str]:
    """从 OpenCode Free 拉取免费模型列表，失败时返回默认列表。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                cfg.fallback.models_url,
                headers={"Authorization": f"Bearer {cfg.fallback.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data", []) if isinstance(data, dict) else data
            # 过滤 -free 后缀的模型（OpenCode Free 的免费模型约定）
            free_ids = [m.get("id", "") for m in models if isinstance(m, dict) and m.get("id", "").endswith("-free")]
            if free_ids:
                return free_ids
            return _DEFAULT_FREE_MODELS
    except Exception as e:
        from loguru import logger

        logger.warning(f"fetch opencode models failed: {e}, using defaults")
        return _DEFAULT_FREE_MODELS


async def _sync_fallback_models(store, cfg, model_ids: list[str]) -> list[int]:
    """把 OpenCode Free 模型 upsert 到 models + api_keys 表，返回 model_id 列表。

    兜底模型作为"一等公民"写入 DB：
    - 每个免费模型创建/更新一条 models 记录（is_fallback=1），name=prefix+mid
    - 为每个兜底模型创建一条 api_keys 记录（key="public"），若不存在
    - 上游消失的兜底模型：禁用（enabled=0）而非删除，避免破坏用户分组配置
    """
    from zhongzhuan.store.models import Model, get_model, create_model, update_model, list_models
    from zhongzhuan.store.keys import ApiKey, list_keys, create_key

    prefix = cfg.fallback.model_prefix
    upserted_ids: list[int] = []
    for mid in model_ids:
        name = f"{prefix}{mid}"
        existing = await get_model(store, name)
        if existing:
            # 已存在：更新上游信息，保留用户的 rpm/tpm/weight/enabled 设置
            existing.upstream_base = cfg.fallback.upstream_base
            existing.upstream_model = mid
            existing.upstream_path_override = cfg.fallback.chat_path
            existing.protocol = "openai"
            existing.is_fallback = True
            # 若之前被禁用（因上游消失），重新拉取到则恢复启用
            existing.enabled = True
            existing_id = existing.id
            if existing_id is not None:
                await update_model(store, existing_id, existing)
                upserted_ids.append(existing_id)
        else:
            m = Model(
                name=name,
                upstream_base=cfg.fallback.upstream_base,
                upstream_model=mid,
                upstream_path_override=cfg.fallback.chat_path,
                protocol="openai",
                is_fallback=True,
                enabled=True,
            )
            m = await create_model(store, m)
            if m.id is not None:
                upserted_ids.append(m.id)
        # 为兜底模型创建 api_key（若该模型下还没有 key）
        existing_keys = await list_keys(store, upserted_ids[-1])
        if not existing_keys:
            await create_key(
                store,
                ApiKey(
                    id=None,
                    model_id=upserted_ids[-1],
                    label="OpenCode Free",
                    key_value=cfg.fallback.api_key,
                    enabled=True,
                    priority=0,
                ),
            )
    # 禁用上游已消失的兜底模型（不删除，保留分组配置）
    all_models = await list_models(store)
    valid_names = {f"{prefix}{mid}" for mid in model_ids}
    for m in all_models:
        if m.is_fallback and m.name not in valid_names:
            if m.enabled and m.id is not None:
                m.enabled = False
                await update_model(store, m.id, m)
    return upserted_ids


async def run_foreground(
    cfg_path: str,
    port_override: int | None,
    upstream_url: str | None,
    key: str | None,
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

    # Request-log retention scheduler (T06 / R-P0-03): deletes rows older than
    # the configured TTL on a fixed cadence.  Defaults to 14d / 3h.
    from zhongzhuan.store.retention import RetentionScheduler

    retention_scheduler = RetentionScheduler(
        store,
        retention_days=int(os.getenv("ZHONGZHUAN_RETENTION_REQUEST_LOG_DAYS", "14")),
        interval_seconds=float(os.getenv("ZHONGZHUAN_RETENTION_INTERVAL_SECONDS", "10800")),
    )
    await retention_scheduler.start()

    # 从 DB 读取持久化的兜底配置（后台修改的 fallback_enabled / fallback_penalty 优先于 config.yaml）
    try:
        rows = await store.fetchall(
            "SELECT `key`, value FROM system_config WHERE `key` IN ('fallback_enabled','fallback_penalty')"
        )
        for k, v in rows:
            if k == "fallback_enabled":
                cfg.fallback.enabled = v == "1"
            elif k == "fallback_penalty":
                cfg.fallback.fallback_penalty = max(0.01, min(1.0, float(v)))
        logger.info(f"fallback config: enabled={cfg.fallback.enabled}, penalty={cfg.fallback.fallback_penalty}")
    except Exception:
        pass  # system_config 表可能不存在或为空，忽略

    # Initialize crypto with store (for AES key in TiDB system_config)
    from zhongzhuan.crypto import init as crypto_init

    async def _get_config(key_name: str) -> str | None:
        row = await store.fetchone("SELECT value FROM system_config WHERE `key`=?", (key_name,))
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

    # OpenCode Free 兜底上游：启用时把免费模型 upsert 到 models + api_keys 表
    # 兜底模型作为"一等公民"写入 DB，可启用/禁用/删除，可加入分组参与路由
    if cfg.fallback.enabled:
        try:
            fallback_model_ids = await _fetch_opencode_models(cfg)
            upserted = await _sync_fallback_models(store, cfg, fallback_model_ids)
            logger.info(f"OpenCode Free 兜底模型已同步: {len(upserted)} 个")
        except Exception:
            logger.exception("同步 OpenCode Free 兜底模型失败")

    # Build keys from DB (with per-model upstream info) — 兜底模型走标准加载路径
    keys = await _load_keys_from_store(store, cfg)

    # Fallback: env/CLI key (仅当 DB 无 key 且无兜底时)
    if not keys:
        api_key = key or os.environ.get("ZHONGZHUAN_KEY", "")
        if api_key:
            upstream_base = upstream_url or os.environ.get("ZHONGZHUAN_UPSTREAM", "https://api.openai.com")
            keys.append(
                KeyHealth(
                    key_id=0,
                    api_key=api_key,
                    window=SlidingWindow(cfg.limits.per_key_window_seconds, cfg.limits.default_rpm_per_key),
                    rpm_limit=cfg.limits.default_rpm_per_key,
                    upstream_base=upstream_base,
                    upstream_model="",
                )
            )

    # If no keys at all (fallback disabled), use a dummy to avoid crash
    if not keys:
        api_key = key or os.environ.get("ZHONGZHUAN_KEY", "dummy-key-no-auth")
        upstream_base = upstream_url or os.environ.get("ZHONGZHUAN_UPSTREAM", "https://api.openai.com")
        keys.append(
            KeyHealth(
                key_id=0,
                api_key=api_key,
                window=SlidingWindow(cfg.limits.per_key_window_seconds, cfg.limits.default_rpm_per_key),
                rpm_limit=cfg.limits.default_rpm_per_key,
                upstream_base=upstream_base,
                upstream_model="",
            )
        )

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

    # Load models and groups for /v1/models.
    # 仅暴露「启用且非兜底」的模型：oc-* 等 is_fallback 模型是上游
    # 池耗尽时的内部兜底实现细节，不应出现在给下游的模型发现列表里。
    # ``exposed`` (M011) 只影响*发现列表*，不影响路由：被隐藏的模型/分组
    # 仍可被客户端显式按名字调用，只是不出现在 /v1/models 里。
    models_data = [
        {"name": m.name, "exposed": int(getattr(m, "exposed", 1) or 0)}
        for m in await list_models(store)
        if m.enabled and not m.is_fallback
    ]
    from zhongzhuan.store.groups import list_groups as list_groups_db

    groups_data = [
        {
            "id": g["id"],
            "name": g["name"],
            "strategy": g["strategy"],
            # 保留 model_id/weight/ord 字典，使 _set_groups 能按 ord 排定
            # failover 成员顺序（严格按成员顺序故障转移）。
            "members": [
                {"model_id": m["model_id"], "weight": m.get("weight", 1), "ord": m.get("ord", 0)}
                for m in (g.get("members") or [])
            ],
            # 展示开关；handler 的 _set_groups 只读 name/members，额外键无害。
            "exposed": int(g.get("exposed", 1) or 0),
        }
        for g in await list_groups_db(store)
    ]

    proxy = ProxyServer(
        upstream_clients=upstream_clients,
        keys=keys,
        proxy_timeout=cfg.limits.proxy_request_timeout,
        models=models_data,
        groups=groups_data,
        store=store,
        load_keys_fn=lambda: _load_keys_from_store(store, cfg),
        sticky_ttl=float(cfg.limits.sticky_session_ttl),
        responses_bridge=cfg.responses_bridge,
    )
    proxy_runner = web.AppRunner(proxy.app())
    await proxy_runner.setup()

    # Build SSL context for proxy port (TLS for VPS / Claude Code)
    from zhongzhuan.proxy.tls import build_ssl_context

    ssl_ctx = build_ssl_context(cfg.server.tls)
    proxy_site = web.TCPSite(
        proxy_runner,
        cfg.server.proxy.host,
        cfg.server.proxy.port,
        ssl_context=ssl_ctx,
    )
    await proxy_site.start()
    scheme = "https" if ssl_ctx else "http"
    logger.info(f"proxy listening on {scheme}://{cfg.server.proxy.host}:{cfg.server.proxy.port}")

    from zhongzhuan.admin import AdminServer  # 惰性导入：核心安装（无 [admin] extra）下 --help 等不触发

    admin = AdminServer(store=store, version=__version__, config=cfg)
    admin_runner = web.AppRunner(admin.app())
    await admin_runner.setup()
    admin_site = web.TCPSite(admin_runner, cfg.server.admin.host, cfg.server.admin.port)
    await admin_site.start()
    logger.info(f"admin listening on {cfg.server.admin.host}:{cfg.server.admin.port}")

    # Open browser in foreground mode (skip in CI / headless via env var)
    if not as_service and not os.getenv("ZHONGZHUAN_NO_BROWSER"):
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
        # 优雅关闭：先停止接收新请求，等待现有请求完成（最多 30s），再释放资源
        logger.info("shutting down (graceful, timeout=30s)...")
        try:
            await asyncio.wait_for(proxy_runner.shutdown(), timeout=35.0)
        except asyncio.TimeoutError:
            logger.warning("proxy shutdown timed out, forcing close")
        try:
            await asyncio.wait_for(admin_runner.shutdown(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("admin shutdown timed out, forcing close")
        await proxy_runner.cleanup()
        await admin_runner.cleanup()
        await retention_scheduler.stop()
        for client in upstream_clients.values():
            await client.close()
        await store.close()
        logger.info("shutdown complete")
    return 0


def handle_service_commands(args: argparse.Namespace) -> int | None:
    """Handle install/uninstall/start/stop/autostart. Returns None if no service command."""
    if sys.platform != "win32":
        return None

    from zhongzhuan.config import load_config

    cfg = load_config(args.config)
    svc_name = cfg.windows_service.service_name
    display_name = cfg.windows_service.display_name

    from zhongzhuan.service import (
        install,
        uninstall,
        start,
        stop,
        status,
        set_autostart,
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

    # Handle TLS selfsign
    if args.tls_selfsign:
        from zhongzhuan.proxy.tls import selfsign
        from pathlib import Path as _P

        for p in (args.out_cert, args.out_key, args.out_ca):
            _P(p).parent.mkdir(parents=True, exist_ok=True)
        san_dns = args.san_dns or (["localhost"] if not args.san_ip else [])
        selfsign(
            out_cert=args.out_cert,
            out_key=args.out_key,
            out_ca=args.out_ca,
            cn=args.cn,
            san_dns=san_dns,
            san_ip=args.san_ip,
            days=args.days,
        )
        print(f"[zhongzhuan] TLS certificate generated:")
        print(f"  cert: {args.out_cert}")
        print(f"  key:  {args.out_key}")
        print(f"  CA:   {args.out_ca}")
        return 0

    # Handle service commands
    result = handle_service_commands(args)
    if result is not None:
        return result

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute() and not cfg_path.exists():
        make_default_config(cfg_path)
        print(f"[zhongzhuan] created default config: {cfg_path}", file=sys.stderr)

    return asyncio.run(
        run_foreground(
            args.config,
            args.port,
            args.upstream,
            args.key,
            as_service=args.service,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
