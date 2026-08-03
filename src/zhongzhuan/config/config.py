"""Configuration model: YAML loading + .env override + defaults."""
from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from typing import ClassVar

import yaml
from dotenv import load_dotenv

from .timeouts import (
    TimeoutConfigError,
    TimeoutPolicy,
    log_effective_timeouts,
    resolve_timeouts,
)


@dataclass
class ListenConfig:
    host: str = "127.0.0.1"
    port: int = 0


@dataclass
class TLSConfig:
    enabled: bool = False
    cert_file: str = ""
    key_file: str = ""


@dataclass
class ServerConfig:
    proxy: ListenConfig = field(default_factory=lambda: ListenConfig(port=8088))
    admin: ListenConfig = field(default_factory=lambda: ListenConfig(port=8089))
    tls: TLSConfig = field(default_factory=TLSConfig)


@dataclass
class LimitsConfig:
    global_concurrent: int = 64
    per_key_window_seconds: int = 60
    default_rpm_per_key: int = 60
    default_tpm_per_key: int = 100000
    default_rpd_per_key: int = 0  # 0 = unlimited
    sticky_session_ttl: int = 1800  # seconds (30 min)
    # DEPRECATED (T01 / R-P0-01): a single 30s budget for every upstream call
    # truncated long running reasoning requests.  The field is kept so old
    # config.yaml / .env files still load, but it no longer drives the upstream
    # timeouts - use the top level ``timeouts:`` section instead.  Setting it
    # explicitly emits a DeprecationWarning plus a startup log line.
    proxy_request_timeout: int = 30


@dataclass
class StorageConfig:
    backend: str = "auto"  # auto | sqlite | tidb
    sqlite_db_path: str = "data.db"
    db_path: str = ""  # alias for sqlite_db_path (YAML compat)
    log_dir: str = "logs"

    def __post_init__(self):
        # db_path is an alias for sqlite_db_path in config.yaml
        if self.db_path:
            self.sqlite_db_path = self.db_path
        elif self.sqlite_db_path:
            self.db_path = self.sqlite_db_path


@dataclass
class WinSvcConfig:
    display_name: str = "Zhongzhuan API Relay"
    auto_start: bool = True
    service_name: str = "Zhongzhuan"


@dataclass
class FallbackConfig:
    """OpenCode Free 兜底上游配置：无 key 时自动启用免费上游。"""
    enabled: bool = True
    upstream_base: str = "https://opencode.ai"
    api_key: str = "public"  # OpenCode Free 使用硬编码 "public" 占位 token
    models_url: str = "https://opencode.ai/zen/v1/models"
    chat_path: str = "/zen/v1/chat/completions"  # OpenAI 兼容端点
    model_prefix: str = "oc-"  # 暴露给下游的模型名前缀
    # 兜底 key 调度降权系数：0.1 表示评分 ×0.1（低优先级），1.0 表示不降权（与普通模型同等）
    fallback_penalty: float = 0.1


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    windows_service: WinSvcConfig = field(default_factory=WinSvcConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    # Six-layer upstream timeout policy (T01).  Built by ``load_config`` from
    # the top level ``timeouts:`` YAML section + ZHONGZHUAN_TIMEOUT_* env vars.
    timeouts: TimeoutPolicy = field(default_factory=TimeoutPolicy)

    # Per-field origin of ``timeouts`` (default / YAML / env).  Declared as a
    # ClassVar so it stays out of ``dataclasses.asdict`` (and therefore out of
    # the YAML written back by ``save_config``); ``load_config`` shadows it
    # with a per-instance value.
    timeout_sources: ClassVar[dict[str, str]] = {}


def default_config() -> Config:
    return Config()


def _merge(dc, data: dict) -> None:
    """Merge dict into dataclass instance (only existing fields)."""
    for k, v in data.items():
        if hasattr(dc, k):
            cur = getattr(dc, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                _merge(cur, v)
            else:
                setattr(dc, k, v)


def load_config(path: str | None) -> Config:
    """Load YAML config file; returns defaults if not found. .env overrides take priority."""
    load_dotenv(".env")

    cfg = default_config()
    timeouts_section: dict = {}
    limits_section: dict = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # ``timeouts`` is validated by TimeoutPolicy, not by the generic
        # dataclass merge (the policy is frozen and enforces hard floors).
        timeouts_section = data.pop("timeouts", None) or {}
        limits_section = data.get("limits") or {}
        _merge(cfg, data)

    # .env overrides for server hosts/ports
    proxy_host = os.getenv("ZHONGZHUAN_PROXY_HOST")
    if proxy_host:
        cfg.server.proxy.host = proxy_host
    proxy_port = os.getenv("ZHONGZHUAN_PROXY_PORT")
    if proxy_port:
        cfg.server.proxy.port = int(proxy_port)
    admin_host = os.getenv("ZHONGZHUAN_ADMIN_HOST")
    if admin_host:
        cfg.server.admin.host = admin_host
    admin_port = os.getenv("ZHONGZHUAN_ADMIN_PORT")
    if admin_port:
        cfg.server.admin.port = int(admin_port)

    # .env override for storage backend
    db_backend = os.getenv("ZHONGZHUAN_TIDB_HOST")
    if db_backend:
        cfg.storage.backend = "tidb"

    # .env override for proxy request timeout (DEPRECATED, see below)
    timeout = os.getenv("ZHONGZHUAN_PROXY_REQUEST_TIMEOUT")
    if timeout:
        cfg.limits.proxy_request_timeout = int(timeout)

    # Six-layer timeout policy (T01): default < YAML < env.
    cfg.timeouts, cfg.timeout_sources = resolve_timeouts(timeouts_section, os.environ)

    # Deprecation notice for the old single-value timeout.  It is still read
    # (kept on LimitsConfig for compatibility) but no longer wired into the
    # upstream clients.
    if "proxy_request_timeout" in limits_section or timeout:
        _warn_deprecated_proxy_timeout(cfg.limits.proxy_request_timeout)

    # .env overrides for TLS
    tls_enabled = os.getenv("ZHONGZHUAN_TLS_ENABLED", "")
    if tls_enabled:
        cfg.server.tls.enabled = tls_enabled.lower() == "true"
    tls_cert = os.getenv("ZHONGZHUAN_TLS_CERT", "")
    if tls_cert:
        cfg.server.tls.cert_file = tls_cert
    tls_key = os.getenv("ZHONGZHUAN_TLS_KEY", "")
    if tls_key:
        cfg.server.tls.key_file = tls_key

    return cfg


def _warn_deprecated_proxy_timeout(value: int) -> None:
    """Emit the DeprecationWarning + startup hint for the retired knob."""
    message = (
        f"limits.proxy_request_timeout ({value}s) is deprecated and no longer "
        f"controls upstream timeouts; migrate to the 'timeouts:' section "
        f"(connect_seconds / first_token_seconds / read_idle_seconds / "
        f"total_seconds / write_seconds / pool_seconds)"
    )
    warnings.warn(message, DeprecationWarning, stacklevel=3)
    try:
        from loguru import logger
        logger.warning(f"[config] {message}")
    except Exception:  # pragma: no cover - logging must never break startup
        pass


def log_timeout_policy(cfg: Config) -> list[str]:
    """Print the six effective timeout values + their source at startup."""
    return log_effective_timeouts(cfg.timeouts, getattr(cfg, "timeout_sources", {}))


def save_config(cfg: Config, path: str) -> None:
    """Write config back to YAML."""
    from dataclasses import asdict

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(asdict(cfg), f, allow_unicode=True, sort_keys=False)