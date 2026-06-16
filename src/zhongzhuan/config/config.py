"""Configuration model: YAML loading + defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml


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
    db_path: str = "data.db"
    log_dir: str = "logs"


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
    """Load YAML config file; returns defaults if not found."""
    cfg = default_config()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _merge(cfg, data)
    return cfg


def save_config(cfg: Config, path: str) -> None:
    """Write config back to YAML."""
    from dataclasses import asdict

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(asdict(cfg), f, allow_unicode=True, sort_keys=False)