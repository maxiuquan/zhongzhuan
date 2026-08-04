"""Pydantic strict configuration schema (T32 / R-P1-62).

The runtime configuration is still carried by the dataclasses in
``config.py`` so every existing caller keeps working, but *loading* it now
goes through this Pydantic layer which enforces:

- strict field whitelisting: unknown keys are rejected by default
  (``extra="forbid"``).  A legacy "compat mode" may be enabled explicitly
  (``ZHONGZHUAN_CONFIG_COMPAT=1``) in which unknown keys are silently dropped
  instead of aborting startup (R-P1-62: "未知配置字段默认报错并提供显式兼容模式").
- range validation for ports, concurrency, TTLs, paths and URLs.
- the ``env`` mode flag (``development`` / ``production``) that drives the
  production fail-closed security defaults (R-P2-01~06).

The ``timeouts`` section is intentionally kept as a raw mapping here: it is
validated separately by :func:`~zhongzhuan.config.timeouts.resolve_timeouts`
which already enforces the 300s floors (T01 / R-P0-01).
"""

from __future__ import annotations

import os
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

__all__ = [
    "ConfigValidationError",
    "EnvMode",
    "ListenSchema",
    "TLSSchema",
    "ServerSchema",
    "LimitsSchema",
    "StorageSchema",
    "WinSvcSchema",
    "FallbackSchema",
    "HostedToolsSchema",
    "CorsSchema",
    "AuthSchema",
    "SecuritySchema",
    "StrictConfig",
    "parse_config",
    "filter_unknown_keys",
    "DEFAULT_SECRET_FIELD_NAMES",
]


def _reject_bool(value: Any) -> Any:
    """bool 不是合法数字（Python 中 bool 是 int 子类，lax 模式会被强转）。"""
    if isinstance(value, bool):
        raise ValueError("must be a number, not a bool")
    return value


#: 数字字段：拒绝 bool，但允许 YAML/env 的数值字符串（lax 强转）。
Int = Annotated[int, BeforeValidator(_reject_bool)]
Float = Annotated[float, BeforeValidator(_reject_bool)]

EnvMode = Literal["development", "production"]

#: Env var that selects the runtime environment.
ENV_VAR = "ZHONGZHUAN_ENV"

#: Env var that enables the explicit legacy compatibility mode.
COMPAT_VAR = "ZHONGZHUAN_CONFIG_COMPAT"

#: Field names whose value must never appear in a redacted snapshot.
DEFAULT_SECRET_FIELD_NAMES = (
    "api_key",
    "jwt_secret",
    "secret_key",
    "password",
    "key_value",
    "token",
)


class ConfigValidationError(ValueError):
    """Raised when the configuration fails strict validation or is unsafe.

    Fatal by design: ``__main__`` and the test suite treat it as a startup
    failure, mirroring :class:`~zhongzhuan.config.timeouts.TimeoutConfigError`.
    """


# ---------------------------------------------------------------------------
# Nested models — every model forbids unknown keys by default.
# ---------------------------------------------------------------------------


class ListenSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: Int = Field(0, ge=0, le=65535)

    @field_validator("host")
    @classmethod
    def _host_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("host must not be blank")
        return v


class TLSSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cert_file: str = ""
    key_file: str = ""


class ServerSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy: ListenSchema = Field(default_factory=lambda: ListenSchema(port=8088))
    admin: ListenSchema = Field(default_factory=lambda: ListenSchema(port=8089))
    tls: TLSSchema = Field(default_factory=TLSSchema)


class LimitsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Max in-flight requests proxy-wide (R-P1-62 concurrency range).
    global_concurrent: Int = Field(64, ge=1, le=100000)
    per_key_window_seconds: Int = Field(60, ge=1, le=86400 * 7)
    default_rpm_per_key: Int = Field(60, ge=0, le=1000000)
    default_tpm_per_key: Int = Field(100000, ge=0, le=1000000000)
    #: 0 = unlimited.
    default_rpd_per_key: Int = Field(0, ge=0, le=1000000000)
    #: Sticky-session TTL in seconds (R-P1-62 TTL range).
    sticky_session_ttl: Int = Field(1800, ge=1, le=86400 * 30)
    #: DEPRECATED (T01) — kept for config compatibility, no longer drives
    #: upstream timeouts.
    proxy_request_timeout: Int = Field(30, ge=1, le=86400 * 7)


class StorageSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["auto", "sqlite", "tidb"] = "auto"
    sqlite_db_path: str = Field("data.db", min_length=1)
    #: YAML compat alias for ``sqlite_db_path``.
    db_path: str = ""
    log_dir: str = Field("logs", min_length=1)

    @model_validator(mode="after")
    def _sync_db_alias(self) -> "StorageSchema":
        if self.db_path:
            self.sqlite_db_path = self.db_path
        elif self.sqlite_db_path:
            self.db_path = self.sqlite_db_path
        return self


class WinSvcSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = "Zhongzhuan API Relay"
    auto_start: bool = True
    service_name: str = "Zhongzhuan"


class FallbackSchema(BaseModel):
    """OpenCode Free 兜底上游 (R-P2-06: 显式 opt-in，默认关闭)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    upstream_base: str = "https://opencode.ai"
    api_key: str = "public"
    models_url: str = "https://opencode.ai/zen/v1/models"
    chat_path: str = "/zen/v1/chat/completions"
    model_prefix: str = "oc-"
    fallback_penalty: Float = Field(0.1, ge=0.01, le=1.0)

    @field_validator("upstream_base", "models_url")
    @classmethod
    def _valid_http_url(cls, v: str) -> str:
        if not v:
            return v
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"must be an absolute http(s) URL, got {v!r}")
        return v

    @field_validator("chat_path")
    @classmethod
    def _valid_abs_path(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"chat_path must start with '/', got {v!r}")
        return v


class HostedToolsSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mcp_enabled: bool = False


class CorsSchema(BaseModel):
    """CORS allowlist (R-P2-01): 默认 `*` 改为可配置 allowlist。"""

    model_config = ConfigDict(extra="forbid")

    allow_origins: list[str] = Field(default_factory=list)

    @field_validator("allow_origins")
    @classmethod
    def _origins_not_blank(cls, v: list[str]) -> list[str]:
        for origin in v:
            if not origin.strip():
                raise ValueError("allow_origins must not contain blank entries")
        return v


class AuthSchema(BaseModel):
    """管理端 / 代理鉴权开关与 JWT 配置 (R-P2-02 / R-P2-04)."""

    model_config = ConfigDict(extra="forbid")

    admin_enabled: bool = False
    proxy_enabled: bool = False
    jwt_secret: str = ""
    jwt_previous_secrets: list[str] = Field(default_factory=list)
    jwt_grace_period_seconds: Int = Field(3600, ge=0, le=86400 * 30)
    #: 二次确认开关：production 下显式关闭鉴权需要额外打开它 (R-P2-02)。
    allow_insecure_disable: bool = False


class SecuritySchema(BaseModel):
    """R-P2-03 / R-P2-04 相关开关。"""

    model_config = ConfigDict(extra="forbid")

    csrf_enabled: bool = False
    login_rate_limit_max: Int = Field(10, ge=1, le=1000)
    login_rate_limit_window: Int = Field(300, ge=1, le=86400)


# ---------------------------------------------------------------------------
# Root model
# ---------------------------------------------------------------------------


class StrictConfig(BaseModel):
    """Root strict configuration — unknown top-level keys fail by default."""

    model_config = ConfigDict(extra="forbid")

    env: EnvMode = "development"
    server: ServerSchema = Field(default_factory=ServerSchema)
    limits: LimitsSchema = Field(default_factory=LimitsSchema)
    storage: StorageSchema = Field(default_factory=StorageSchema)
    windows_service: WinSvcSchema = Field(default_factory=WinSvcSchema)
    fallback: FallbackSchema = Field(default_factory=FallbackSchema)
    hosted_tools: HostedToolsSchema = Field(default_factory=HostedToolsSchema)
    cors: CorsSchema = Field(default_factory=CorsSchema)
    auth: AuthSchema = Field(default_factory=AuthSchema)
    security: SecuritySchema = Field(default_factory=SecuritySchema)
    #: Validated separately by ``timeouts.resolve_timeouts`` (T01 hard floors).
    timeouts: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _unknown_keys(data: dict[str, Any], model: type[BaseModel]) -> list[str]:
    return [str(k) for k in data if str(k) not in model.model_fields]


def filter_unknown_keys(data: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    """Recursively drop unknown keys so lenient/compat parsing can ignore them.

    ``extra="forbid"`` on every model rejects unknown keys; the explicit
    compat mode (R-P1-62) instead pre-filters them so legacy config files
    still load.  Nested model fields are recursed; everything else is kept.
    """
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key not in model.model_fields:
            continue
        field = model.model_fields[key]
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            filtered = filter_unknown_keys(value, annotation)
            # If a nested mapping only ever contained unknown keys (i.e. the
            # filtered result is empty), drop the whole field so the model's
            # default_factory still applies (e.g. proxy port stays 8088).
            if isinstance(value, dict) and filtered:
                out[key] = filtered
            elif isinstance(value, dict):
                continue
            else:
                out[key] = value
        else:
            out[key] = value
    return out


def parse_config(
    raw: dict[str, Any] | None,
    *,
    compat: bool = False,
) -> StrictConfig:
    """Validate a raw (YAML + env merged) dict against the strict schema.

    Args:
        raw: Configuration mapping.  ``None`` / ``{}`` yields defaults.
        compat: When True, unknown keys are dropped instead of raising
            (explicit legacy compatibility mode, R-P1-62).

    Raises:
        ConfigValidationError: On type errors, out-of-range values or (unless
            ``compat=True``) unknown fields.
    """
    data = dict(raw or {})
    if compat:
        data = filter_unknown_keys(data, StrictConfig)
    try:
        return StrictConfig.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError etc. -> one public type
        raise ConfigValidationError(_format_validation_error(exc)) from exc


def _format_validation_error(exc: Exception) -> str:
    """Turn a pydantic ValidationError into a compact one-line message."""
    errors = getattr(exc, "errors", lambda: None)()
    if errors:
        parts = []
        for err in errors[:12]:
            where = ".".join(str(x) for x in err.get("loc", ())) or "<root>"
            msg = err.get("msg", str(err.get("type", "error")))
            parts.append(f"{where}: {msg}")
        return f"invalid config: {'; '.join(parts)}"
    return f"invalid config: {exc}"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def env_mode(env: str | None = None) -> EnvMode:
    """Resolve the runtime environment (parameter > ``ZHONGZHUAN_ENV`` > dev)."""
    value = env or os.getenv(ENV_VAR, "") or "development"
    value = value.strip().lower()
    if value in ("production", "prod", "prod=", "生产"):
        return "production"
    if value in ("development", "dev", "development=", "开发"):
        return "development"
    raise ConfigValidationError(f"{ENV_VAR} must be 'development' or 'production', got {value!r}")


def compat_mode(enabled: bool | None = None) -> bool:
    """Whether the explicit legacy compat mode is active."""
    if enabled is not None:
        return enabled
    raw = os.getenv(COMPAT_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")
