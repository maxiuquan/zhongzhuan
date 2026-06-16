"""Configuration model: YAML loading + .env override + defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv


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
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    windows_service: WinSvcConfig = field(default_factory=WinSvcConfig)


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
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
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

    return cfg


def save_config(cfg: Config, path: str) -> None:
    """Write config back to YAML."""
    from dataclasses import asdict

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(asdict(cfg), f, allow_unicode=True, sort_keys=False)