"""Effective config snapshot: merged defaults + YAML + env, redacted (T32).

R-P1-62 requires the startup banner to print the *effective* configuration
with every secret redacted and every field annotated with its winning source
(``default`` / ``YAML`` / ``env``).  This module builds that snapshot from the
runtime :class:`~zhongzhuan.config.config.Config` dataclass plus a sources
mapping produced by :func:`collect_sources`.

The output is a flat mapping of dotted field paths (``server.proxy.port``) to
``{"value": <redacted>, "source": <label>}`` so tests can assert *exactly one
field at a time* is correct, and ops can eyeball the whole banner.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping

__all__ = [
    "FALLBACK_PRIVACY_NOTICE",
    "SECRET_FIELD_SUFFIXES",
    "is_secret_path",
    "redact_value",
    "collect_sources",
    "effective_config_snapshot",
    "format_effective_config",
    "log_effective_config",
]

SOURCE_DEFAULT = "default"
SOURCE_YAML = "YAML"
SOURCE_ENV = "env"

#: R-P2-06: 打开 OpenCode Free fallback（外部数据出站）时 UI 必须显示的隐私提示。
FALLBACK_PRIVACY_NOTICE = (
    "OpenCode Free 兜底上游会把请求数据发送到外部服务（opencode.ai），"
    "请确认你的业务允许数据出站后再启用。"
)

#: Field-name suffixes / prefixes that mark a value as a secret.
#: 注意不能用宽泛的 ``_key`` 后缀——``limits.default_rpm_per_key`` 这类
#: 「per key 限流」字段不是密钥。因此采用精确字段名集合 + 窄后缀。
SECRET_FIELD_SUFFIXES = (
    "_secret",
    "_password",
    "_token",
    "_secret_key",
)
SECRET_FIELD_NAMES = frozenset(
    {"api_key", "key_value", "jwt_secret", "jwt_previous_secrets", "secret_key",
     "password", "access_token", "admin_password"}
)


def is_secret_path(path: str) -> bool:
    """True when a dotted config path points at a secret value."""
    leaf = path.rsplit(".", 1)[-1].lower()
    if leaf in SECRET_FIELD_NAMES:
        return True
    return leaf.endswith(SECRET_FIELD_SUFFIXES)


def redact_value(value: Any) -> Any:
    """Redact a secret value to ``"***"`` (list values → one ``***`` each)."""
    if isinstance(value, list):
        return ["***" for _ in value]
    if value is None or value == "":
        return value
    return "***"


# ---------------------------------------------------------------------------
# Source tracking
# ---------------------------------------------------------------------------

#: Maps env vars to the config dotted path they override.
ENV_OVERRIDE_PATHS: dict[str, str] = {
    "ZHONGZHUAN_ENV": "env",
    "ZHONGZHUAN_PROXY_HOST": "server.proxy.host",
    "ZHONGZHUAN_PROXY_PORT": "server.proxy.port",
    "ZHONGZHUAN_ADMIN_HOST": "server.admin.host",
    "ZHONGZHUAN_ADMIN_PORT": "server.admin.port",
    "ZHONGZHUAN_TLS_ENABLED": "server.tls.enabled",
    "ZHONGZHUAN_TLS_CERT": "server.tls.cert_file",
    "ZHONGZHUAN_TLS_KEY": "server.tls.key_file",
    "ZHONGZHUAN_PROXY_REQUEST_TIMEOUT": "limits.proxy_request_timeout",
    "ZHONGZHUAN_ADMIN_AUTH": "auth.admin_enabled",
    "ZHONGZHUAN_PROXY_AUTH": "auth.proxy_enabled",
    "ZHONGZHUAN_JWT_SECRET": "auth.jwt_secret",
    "ZHONGZHUAN_JWT_SECRET_PREVIOUS": "auth.jwt_previous_secrets",
    "ZHONGZHUAN_JWT_GRACE_PERIOD_SECONDS": "auth.jwt_grace_period_seconds",
    "ZHONGZHUAN_ALLOW_INSECURE_DISABLE": "auth.allow_insecure_disable",
    "ZHONGZHUAN_CORS_ALLOW_ORIGINS": "cors.allow_origins",
    "ZHONGZHUAN_FALLBACK_ENABLED": "fallback.enabled",
    "ZHONGZHUAN_CSRF_ENABLED": "security.csrf_enabled",
}

#: Env vars with *side effects* on a config field that isn't named like the var.
_ALIAS_OVERRIDES: dict[str, str] = {
    # ZHONGZHUAN_TIDB_HOST 会把 storage.backend 强制为 "tidb"。
    "ZHONGZHUAN_TIDB_HOST": "storage.backend",
}


def _leaf_paths(obj: Any, prefix: str = "", out: list[str] | None = None) -> list[str]:
    """Flatten a nested dataclass into dotted leaf field paths."""
    if out is None:
        out = []
    if not dataclasses.is_dataclass(obj):
        return out
    for f in dataclasses.fields(obj):
        if not f.init:
            continue  # ClassVar / InitVar
        path = f"{prefix}.{f.name}" if prefix else f.name
        if path == "timeouts":
            continue  # tracked separately via TimeoutPolicy sources
        value = getattr(obj, f.name)
        if dataclasses.is_dataclass(value):
            _leaf_paths(value, path, out)
        else:
            out.append(path)
    return out


def _walk_yaml(data: Mapping[str, Any], prefix: str = "", out: list[str] | None = None) -> list[str]:
    """Return dotted paths explicitly present in the YAML mapping."""
    if out is None:
        out = []
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and key not in ("timeouts",):
            _walk_yaml(value, path, out)
        else:
            out.append(path)
    return out


def collect_sources(
    cfg: Any,
    yaml_data: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Compute ``{dotted_path: source}`` for every config leaf field.

    Precedence is ``env > YAML > default`` (matching the loader).  The YAML
    ``storage.db_path`` alias is also reflected onto ``storage.sqlite_db_path``.
    """
    env = dict(environ or {})
    sources: dict[str, str] = {p: SOURCE_DEFAULT for p in _leaf_paths(cfg)}

    yaml_paths = set(_walk_yaml(yaml_data or {}))
    # db_path 是 sqlite_db_path 的 YAML 别名：两者都标注 YAML。
    if "storage.db_path" in yaml_paths:
        yaml_paths.add("storage.sqlite_db_path")

    for path in yaml_paths:
        if path in sources:
            sources[path] = SOURCE_YAML

    for var, path in ENV_OVERRIDE_PATHS.items():
        if env.get(var, "") != "":
            sources[path] = SOURCE_ENV
    for var, path in _ALIAS_OVERRIDES.items():
        if env.get(var, "") != "":
            sources[path] = SOURCE_ENV

    return sources


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def effective_config_snapshot(
    cfg: Any,
    sources: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build ``{path: {"value": redacted, "source": label}}`` for all fields.

    The returned snapshot contains **no** plaintext secret values: every
    secret-bearing field is replaced by ``"***"`` (R-P1-62 判据②).
    """
    if sources is None:
        # 自动回退到 cfg 上已记录的来源（load_config 写入 config_sources）。
        sources = getattr(cfg, "config_sources", None) or {}
    src = dict(sources)
    snapshot: dict[str, dict[str, Any]] = {}
    for path, value in _leaf_values(cfg):
        label = src.get(path, SOURCE_DEFAULT)
        shown = redact_value(value) if is_secret_path(path) else value
        snapshot[path] = {"value": shown, "source": label}

    # Six timeout layers carry their own per-field sources (T01).
    timeout_sources = getattr(cfg, "timeout_sources", None) or {}
    for name, value in _timeout_values(cfg):
        path = f"timeouts.{name}"
        label = timeout_sources.get(name, SOURCE_DEFAULT) or SOURCE_DEFAULT
        snapshot[path] = {"value": value, "source": label}
    return snapshot


def _leaf_values(obj: Any, prefix: str = "", out: list[tuple[str, Any]] | None = None) -> list[tuple[str, Any]]:
    if out is None:
        out = []
    if not dataclasses.is_dataclass(obj):
        return out
    for f in dataclasses.fields(obj):
        if not f.init:
            continue
        path = f"{prefix}.{f.name}" if prefix else f.name
        value = getattr(obj, f.name)
        if dataclasses.is_dataclass(value):
            if path == "timeouts":
                continue
            _leaf_values(value, path, out)
        else:
            out.append((path, value))
    return out


def _timeout_values(cfg: Any) -> list[tuple[str, Any]]:
    policy = getattr(cfg, "timeouts", None)
    if policy is None or not dataclasses.is_dataclass(policy):
        return []
    return [(f.name, getattr(policy, f.name)) for f in dataclasses.fields(policy) if f.init]


def format_effective_config(
    cfg: Any,
    sources: Mapping[str, str] | None = None,
) -> list[str]:
    """Render one audit line per field: ``path = value [source]`` (redacted)."""
    snapshot = effective_config_snapshot(cfg, sources)
    lines: list[str] = []
    for path in sorted(snapshot):
        entry = snapshot[path]
        lines.append(f"{path} = {entry['value']!r} [{entry['source']}]")
    return lines


def log_effective_config(
    cfg: Any,
    sources: Mapping[str, str] | None = None,
    logger: Any = None,
) -> list[str]:
    """Log the redacted effective config at startup; returns the rendered lines."""
    if logger is None:
        from loguru import logger as _logger
        logger = _logger
    lines = format_effective_config(cfg, sources)
    for line in lines:
        logger.info(f"[effective-config] {line}")
    return lines
