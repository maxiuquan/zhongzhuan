"""Configuration model: YAML loading + .env override + defaults (T32).

R-P1-62 之后加载路径变成：

    YAML + env overrides
        -> config/schema.py (Pydantic 严格校验：类型/范围/未知字段)
        -> runtime dataclass 树（本文件，所有既有调用方保持兼容）
        -> config/effective.py (脱敏快照 + 来源标注，启动输出)

生产模式（``env: production`` / ``ZHONGZHUAN_ENV=production``）执行
fail-closed 安全检查（R-P2-02/04/05），违反即抛 :class:`ConfigError`，
启动失败。开发模式保留宽松行为并输出告警。
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from typing import ClassVar, Mapping

import yaml
from dotenv import load_dotenv

from .effective import (
    collect_sources,
    effective_config_snapshot as _snapshot,
    log_effective_config as _log_effective,
)
from .schema import (
    ConfigValidationError,
    StrictConfig,
    compat_mode,
    env_mode,
    parse_config,
)
from .timeouts import (
    TimeoutPolicy,
    log_effective_timeouts,
    resolve_timeouts,
)

#: 对外暴露的统一配置错误类型（校验失败 + 生产安全检查失败）。alias 成
#: schema 抛出的错误类型，保证 ``raise ConfigError`` / ``except ConfigError``
#: 覆盖整条加载管线。
ConfigError = ConfigValidationError


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
    """OpenCode Free 兜底上游（R-P2-06：显式 opt-in，默认关闭）。"""

    enabled: bool = False
    upstream_base: str = "https://opencode.ai"
    api_key: str = "public"  # OpenCode Free 使用硬编码 "public" 占位 token
    models_url: str = "https://opencode.ai/zen/v1/models"
    chat_path: str = "/zen/v1/chat/completions"  # OpenAI 兼容端点
    model_prefix: str = "oc-"  # 暴露给下游的模型名前缀
    # 兜底 key 调度降权系数：0.1 表示评分 ×0.1（低优先级），1.0 表示不降权（与普通模型同等）
    fallback_penalty: float = 0.1


@dataclass
class HostedToolsConfig:
    """hosted tool 执行开关（T27 遗留的 opt-in，T28 补上显式启用路径）。

    PRD §3.3 #4 裁定 ``mcp`` 是 🟢 完整实现，但它的执行器真的会对外发网络
    请求——默认打开等于给每个租户默认开出网通道。因此默认关闭：请求携带
    ``mcp`` tool 时返回 400 ``unsupported_tool``；管理员显式打开后，同样的
    请求才会真正走到 T27 的 :class:`~zhongzhuan.responses_v3.mcp_client.McpClient`。

    ``tool_search_enabled`` 同理是 opt-in：开启后中继自行合成
    ``tool_search_output`` 并把 ``multi_agent_v1`` namespace 暴露给客户端
    （APIAADBPW-REQ-MA-001 / FR-1 / FR-2）。
    """

    #: 是否启用 Remote MCP 执行器（默认关闭，见模块头裁决记录）。
    mcp_enabled: bool = False
    #: 是否启用 hosted ``tool_search`` 执行器（中继合成 multi_agent_v1 namespace）。
    tool_search_enabled: bool = False


@dataclass
class MultiAgentConfig:
    """V1 多代理编排配置（APIAADBPW-REQ-MA-001 / FR-3 / FR-4 / NFR）。

    仅当 ``hosted_tools.tool_search_enabled`` 为真时才生效：该开关是整条
    V1 多代理能力的总闸（安全默认关闭，不开出网 / 不暴露 namespace）。
    """

    #: 是否启用 multi_agent_v1 namespace 编排（总闸，默认关闭）。
    enabled: bool = False
    #: 同一会话内并行子代理上限（NFR-6）。
    max_threads: int = 4
    #: 单子代理 rollout 硬上限秒数（NFR-1，默认 1800 对齐客户端 1800s 上限）。
    job_max_runtime_seconds: int = 1800
    #: 暴露给 Codex 客户端的 minimal_client_version（FR-6，建议 0.144.0 起实测）。
    minimal_client_version: str = "0.144.0"


@dataclass
class CorsConfig:
    """CORS allowlist（R-P2-01）：默认 ``*`` 改为可配置 allowlist。

    生产模式下空 allowlist 拒绝跨域（不返回 Access-Control-Allow-Origin）。
    """

    allow_origins: list[str] = field(default_factory=list)


@dataclass
class AuthConfig:
    """管理端 / 代理鉴权 + JWT（R-P2-02 / R-P2-04）。"""

    admin_enabled: bool = False
    proxy_enabled: bool = False
    jwt_secret: str = ""
    jwt_previous_secrets: list[str] = field(default_factory=list)
    #: 旧 secret 轮换宽限期（秒）：轮换后旧 token 在该窗口内仍可验证。
    jwt_grace_period_seconds: int = 3600
    #: R-P2-02 二次确认开关：production 下显式关闭鉴权需额外打开它。
    allow_insecure_disable: bool = False


@dataclass
class SecurityConfig:
    """R-P2-03 相关开关。"""

    csrf_enabled: bool = False
    login_rate_limit_max: int = 10
    login_rate_limit_window: int = 300


@dataclass
class ResponsesRolloutConfig:
    """Responses v3 model/group/key 灰度映射（R-P0-25）。"""

    groups: dict[str, bool] = field(default_factory=dict)
    models: dict[str, bool] = field(default_factory=dict)
    keys: dict[int, bool] = field(default_factory=dict)


@dataclass
class ResponsesTimeoutConfig:
    """Responses v3 流水线的四层超时（P0-7 / 铁律 5 / AC-7.4）。

    这里的默认值就是铁律 5 的**硬上限**本身：首 token 300s、读空闲 300s、
    总时长 900s。配置只能把它们收得更严，放宽会在
    :meth:`~zhongzhuan.responses_v3.pipeline.PipelineConfig.__post_init__`
    里被钳制回上限（AC-7.2/7.3）——「配错了就按最严的来」，
    而不是抛异常把整个进程拖垮。
    """

    first_token_seconds: float = 300.0
    read_idle_seconds: float = 300.0
    total_seconds: float = 900.0
    connect_seconds: float = 15.0


@dataclass
class ResponsesBridgeConfig:
    """Responses v3 bridge 总开关与灰度配置。"""

    version: str = "v3"
    enabled: bool = True
    #: P0-2 / 铁律 2: a stream that died without a completion signal must be
    #: reported as ``incomplete``/``failed``, never whitewashed into
    #: ``response.completed``.  GA ships strict; the flag exists only so an
    #: operator can buy back the pre-GA compatibility behaviour during an
    #: incident without a redeploy.  ``PipelineConfig`` keeps the *library*
    #: default permissive (R-P1-22), so this is where GA states its policy.
    strict_terminal: bool = True
    rollout: ResponsesRolloutConfig = field(default_factory=ResponsesRolloutConfig)
    timeout: ResponsesTimeoutConfig = field(default_factory=ResponsesTimeoutConfig)


@dataclass
class Config:
    env: str = "development"
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    windows_service: WinSvcConfig = field(default_factory=WinSvcConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    hosted_tools: HostedToolsConfig = field(default_factory=HostedToolsConfig)
    cors: CorsConfig = field(default_factory=CorsConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    responses_bridge: ResponsesBridgeConfig = field(default_factory=ResponsesBridgeConfig)
    multi_agent: MultiAgentConfig = field(default_factory=MultiAgentConfig)
    # Six-layer upstream timeout policy (T01).  Built by ``load_config`` from
    # the top level ``timeouts:`` YAML section + ZHONGZHUAN_TIMEOUT_* env vars.
    timeouts: TimeoutPolicy = field(default_factory=TimeoutPolicy)

    # Per-field origin of ``timeouts`` (default / YAML / env).  Declared as a
    # ClassVar so it stays out of ``dataclasses.asdict`` (and therefore out of
    # the YAML written back by ``save_config``); ``load_config`` shadows it
    # with a per-instance value.
    timeout_sources: ClassVar[dict[str, str]] = {}
    # Per-field origin of the *whole* config (R-P1-62 来源标注).  Same pattern.
    config_sources: ClassVar[dict[str, str]] = {}


#: 进程级「当前生效配置」。服务运行期由 ``set_current_config`` 注入
#: ``load_config`` 的真实实例；未注入时 ``default_config`` 返回全默认值。
#: 运行时组件（capability router 的 emulated 集、V1 多代理开关等）必须经它
#: 读到 config.yaml 里的实际值，而不是每次都拿到全新的默认值（2026-08-15
#: 排查确认：多代理功能在生产全死的根因就是 ``default_config`` 只返回默认）。
_CURRENT_CONFIG: Config | None = None


def set_current_config(cfg: Config | None) -> None:
    """注入 / 清除进程级当前配置（``run_foreground`` 在 ``load_config`` 后调用）。"""
    global _CURRENT_CONFIG
    _CURRENT_CONFIG = cfg


def default_config() -> Config:
    return _CURRENT_CONFIG if _CURRENT_CONFIG is not None else Config()


# ---------------------------------------------------------------------------
# Schema mapping
# ---------------------------------------------------------------------------


def _schema_to_config(s: StrictConfig) -> Config:
    """Map the validated Pydantic schema onto the runtime dataclass tree."""
    return Config(
        env=s.env,
        server=ServerConfig(
            proxy=ListenConfig(host=s.server.proxy.host, port=s.server.proxy.port),
            admin=ListenConfig(host=s.server.admin.host, port=s.server.admin.port),
            tls=TLSConfig(
                enabled=s.server.tls.enabled,
                cert_file=s.server.tls.cert_file,
                key_file=s.server.tls.key_file,
            ),
        ),
        limits=LimitsConfig(
            global_concurrent=s.limits.global_concurrent,
            per_key_window_seconds=s.limits.per_key_window_seconds,
            default_rpm_per_key=s.limits.default_rpm_per_key,
            default_tpm_per_key=s.limits.default_tpm_per_key,
            default_rpd_per_key=s.limits.default_rpd_per_key,
            sticky_session_ttl=s.limits.sticky_session_ttl,
            proxy_request_timeout=s.limits.proxy_request_timeout,
        ),
        storage=StorageConfig(
            backend=s.storage.backend,
            sqlite_db_path=s.storage.sqlite_db_path,
            db_path=s.storage.db_path,
            log_dir=s.storage.log_dir,
        ),
        windows_service=WinSvcConfig(
            display_name=s.windows_service.display_name,
            auto_start=s.windows_service.auto_start,
            service_name=s.windows_service.service_name,
        ),
        fallback=FallbackConfig(
            enabled=s.fallback.enabled,
            upstream_base=s.fallback.upstream_base,
            api_key=s.fallback.api_key,
            models_url=s.fallback.models_url,
            chat_path=s.fallback.chat_path,
            model_prefix=s.fallback.model_prefix,
            fallback_penalty=s.fallback.fallback_penalty,
        ),
        hosted_tools=HostedToolsConfig(
            mcp_enabled=s.hosted_tools.mcp_enabled,
            tool_search_enabled=s.hosted_tools.tool_search_enabled,
        ),
        cors=CorsConfig(allow_origins=list(s.cors.allow_origins)),
        auth=AuthConfig(
            admin_enabled=s.auth.admin_enabled,
            proxy_enabled=s.auth.proxy_enabled,
            jwt_secret=s.auth.jwt_secret,
            jwt_previous_secrets=list(s.auth.jwt_previous_secrets),
            jwt_grace_period_seconds=s.auth.jwt_grace_period_seconds,
            allow_insecure_disable=s.auth.allow_insecure_disable,
        ),
        security=SecurityConfig(
            csrf_enabled=s.security.csrf_enabled,
            login_rate_limit_max=s.security.login_rate_limit_max,
            login_rate_limit_window=s.security.login_rate_limit_window,
        ),
        responses_bridge=ResponsesBridgeConfig(
            version=s.responses_bridge.version,
            enabled=s.responses_bridge.enabled,
            strict_terminal=s.responses_bridge.strict_terminal,
            rollout=ResponsesRolloutConfig(
                groups=dict(s.responses_bridge.rollout.groups),
                models=dict(s.responses_bridge.rollout.models),
                keys=dict(s.responses_bridge.rollout.keys),
            ),
            timeout=ResponsesTimeoutConfig(
                first_token_seconds=float(s.responses_bridge.timeout.first_token_seconds),
                read_idle_seconds=float(s.responses_bridge.timeout.read_idle_seconds),
                total_seconds=float(s.responses_bridge.timeout.total_seconds),
                connect_seconds=float(s.responses_bridge.timeout.connect_seconds),
            ),
        ),
        multi_agent=MultiAgentConfig(
            enabled=s.multi_agent.enabled,
            max_threads=int(s.multi_agent.max_threads),
            job_max_runtime_seconds=int(s.multi_agent.job_max_runtime_seconds),
            minimal_client_version=str(s.multi_agent.minimal_client_version),
        ),
    )


# ---------------------------------------------------------------------------
# env overrides
# ---------------------------------------------------------------------------


def _set_path(raw: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = raw
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _env_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def _strict_env_bool(value: str, *, name: str) -> bool:
    """Parse an operational boolean without silently treating typos as false."""
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be one of 1/true/yes/on or 0/false/no/off, got {value!r}")


def _env_str_list(value: str | None) -> list[str] | None:
    if value is None:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _apply_env_overrides(raw: dict, environ: Mapping) -> dict:
    """Merge ZHONGZHUAN_* env vars into the raw config mapping (env > YAML)."""
    get = environ.get

    if v := get("ZHONGZHUAN_PROXY_HOST"):
        _set_path(raw, "server.proxy.host", v)
    if v := get("ZHONGZHUAN_PROXY_PORT"):
        _set_path(raw, "server.proxy.port", v)
    if v := get("ZHONGZHUAN_ADMIN_HOST"):
        _set_path(raw, "server.admin.host", v)
    if v := get("ZHONGZHUAN_ADMIN_PORT"):
        _set_path(raw, "server.admin.port", v)

    if get("ZHONGZHUAN_TIDB_HOST"):
        _set_path(raw, "storage.backend", "tidb")

    if v := get("ZHONGZHUAN_PROXY_REQUEST_TIMEOUT"):
        _set_path(raw, "limits.proxy_request_timeout", v)

    if v := get("ZHONGZHUAN_TLS_ENABLED"):
        _set_path(raw, "server.tls.enabled", v)
    if v := get("ZHONGZHUAN_TLS_CERT"):
        _set_path(raw, "server.tls.cert_file", v)
    if v := get("ZHONGZHUAN_TLS_KEY"):
        _set_path(raw, "server.tls.key_file", v)

    # R-P2-01 / R-P2-02 / R-P2-04 相关覆盖
    if v := get("ZHONGZHUAN_CORS_ALLOW_ORIGINS"):
        _set_path(raw, "cors.allow_origins", _env_str_list(v))
    if (v := get("ZHONGZHUAN_ADMIN_AUTH")) is not None:
        _set_path(raw, "auth.admin_enabled", v)
    if (v := get("ZHONGZHUAN_PROXY_AUTH")) is not None:
        _set_path(raw, "auth.proxy_enabled", v)
    if v := get("ZHONGZHUAN_JWT_SECRET"):
        _set_path(raw, "auth.jwt_secret", v)
    if v := get("ZHONGZHUAN_JWT_SECRET_PREVIOUS"):
        _set_path(raw, "auth.jwt_previous_secrets", _env_str_list(v))
    if v := get("ZHONGZHUAN_JWT_GRACE_PERIOD_SECONDS"):
        _set_path(raw, "auth.jwt_grace_period_seconds", v)
    if (v := get("ZHONGZHUAN_ALLOW_INSECURE_DISABLE")) is not None:
        _set_path(raw, "auth.allow_insecure_disable", v)
    if (v := get("ZHONGZHUAN_FALLBACK_ENABLED")) is not None:
        _set_path(raw, "fallback.enabled", v)
    if (v := get("ZHONGZHUAN_CSRF_ENABLED")) is not None:
        _set_path(raw, "security.csrf_enabled", v)

    # T22 / R-P0-25: environment is the global hard override.  Accept the
    # architecture name and the shorter historic alias, preferring the
    # namespaced value when both are present.  Unlike legacy boolean knobs,
    # invalid values abort startup instead of silently disabling the bridge.
    v3_name = "ZHONGZHUAN_RESPONSES_BRIDGE_V3"
    v3_raw = get(v3_name)
    if v3_raw is None:
        v3_name = "RESPONSES_BRIDGE_V3"
        v3_raw = get(v3_name)
    if v3_raw is not None:
        _set_path(
            raw,
            "responses_bridge.enabled",
            _strict_env_bool(str(v3_raw), name=v3_name),
        )

    return raw


# ---------------------------------------------------------------------------
# Production fail-closed checks (R-P2-02 / R-P2-04 / R-P2-05)
# ---------------------------------------------------------------------------


def validate_production_ready(
    cfg: Config,
    *,
    api_key_count: int | None = None,
) -> None:
    """Enforce production security defaults (fail closed).

    - R-P2-02 (判据④): production 下管理端/代理鉴权默认开启，显式关闭必须
      同时打开 ``auth.allow_insecure_disable``（二次确认）。
    - R-P2-04 (判据⑤): production 下 ``auth.jwt_secret`` 必填。
    - R-P2-05 (判据⑥): production 下无有效 API key（``api_key_count==0``）
      启动失败；开发模式仅告警。

    开发模式不会抛错（除非显式传 ``api_key_count=0`` 也只告警）。
    """
    if cfg.env != "production":
        if api_key_count is not None and api_key_count == 0:
            _warn_no_keys_dev()
        return

    issues: list[str] = []
    if not cfg.auth.admin_enabled and not cfg.auth.allow_insecure_disable:
        issues.append(
            "admin auth is disabled in production; set ZHONGZHUAN_ADMIN_AUTH=true "
            "or explicitly confirm with ZHONGZHUAN_ALLOW_INSECURE_DISABLE=true"
        )
    if not cfg.auth.proxy_enabled and not cfg.auth.allow_insecure_disable:
        issues.append(
            "proxy auth is disabled in production; set ZHONGZHUAN_PROXY_AUTH=true "
            "or explicitly confirm with ZHONGZHUAN_ALLOW_INSECURE_DISABLE=true"
        )
    if not cfg.auth.jwt_secret:
        issues.append("ZHONGZHUAN_JWT_SECRET is required in production (fail closed, no in-process random generation)")
    if api_key_count is not None and api_key_count == 0:
        issues.append("no valid API keys configured in production (fail closed, no dummy-key-no-auth fallback)")
    if issues:
        raise ConfigError("production safety check failed: " + "; ".join(issues))


def _warn_no_keys_dev() -> None:
    message = (
        "no API keys configured; running with a placeholder key in development "
        "mode. Set ZHONGZHUAN_KEY or add keys via the admin UI before production."
    )
    warnings.warn(message, UserWarning, stacklevel=3)
    try:
        from loguru import logger

        logger.warning(f"[config] {message}")
    except Exception:  # pragma: no cover - logging must never break startup
        pass


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _merge(dc, data: dict) -> None:
    """Merge dict into dataclass instance (only existing fields)."""
    for k, v in data.items():
        if hasattr(dc, k):
            cur = getattr(dc, k)
            if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
                _merge(cur, v)
            else:
                setattr(dc, k, v)


def load_config(
    path: str | None,
    *,
    compat: bool | None = None,
    env: str | None = None,
    api_key_count: int | None = None,
) -> Config:
    """Load YAML config file; returns defaults if not found.

    Pipeline: YAML -> env overrides -> Pydantic strict schema (type/range/
    unknown-field validation) -> runtime dataclass -> production fail-closed
    checks.  Raises :class:`ConfigError` (or the underlying
    :class:`TimeoutConfigError`) on any violation so startup aborts.
    """
    load_dotenv(".env")

    yaml_data: dict = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    # Resolve runtime environment: param > env var > YAML > development.
    yaml_env = yaml_data.get("env") if isinstance(yaml_data, dict) else None
    mode = env_mode(env or os.getenv("ZHONGZHUAN_ENV", "") or (str(yaml_env) if yaml_env else None))

    raw: dict = dict(yaml_data)
    raw["env"] = mode
    _apply_env_overrides(raw, os.environ)

    schema = parse_config(raw, compat=compat_mode(compat))
    cfg = _schema_to_config(schema)

    # Production 下鉴权「默认开启」：未显式配置时打开（R-P2-02）。
    if cfg.env == "production":
        auth_section = yaml_data.get("auth") if isinstance(yaml_data, dict) else None
        if not (auth_section and "admin_enabled" in auth_section) and "ZHONGZHUAN_ADMIN_AUTH" not in os.environ:
            cfg.auth.admin_enabled = True
        if not (auth_section and "proxy_enabled" in auth_section) and "ZHONGZHUAN_PROXY_AUTH" not in os.environ:
            cfg.auth.proxy_enabled = True

    # Six-layer timeout policy (T01): default < YAML < env.
    timeouts_section = yaml_data.get("timeouts") or {}
    cfg.timeouts, _sources = resolve_timeouts(timeouts_section, os.environ)
    object.__setattr__(cfg, "timeout_sources", _sources)

    # Deprecation notice for the old single-value timeout.
    if isinstance(yaml_data, dict) and "proxy_request_timeout" in (yaml_data.get("limits") or {}):
        _warn_deprecated_proxy_timeout(cfg.limits.proxy_request_timeout)

    # Per-field source annotation (R-P1-62 来源标注)。
    object.__setattr__(cfg, "config_sources", collect_sources(cfg, yaml_data, os.environ))

    # Fail-closed production safety checks.
    validate_production_ready(cfg, api_key_count=api_key_count)

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


def log_effective_config(cfg: Config) -> list[str]:
    """Log the redacted effective config (R-P1-62); returns rendered lines."""
    return _log_effective(cfg, getattr(cfg, "config_sources", None))


def effective_config_snapshot(cfg: Config) -> dict[str, dict]:
    """Redacted effective config snapshot (R-P1-62 判据②)."""
    return _snapshot(cfg, getattr(cfg, "config_sources", None))


def save_config(cfg: Config, path: str) -> None:
    """Write config back to YAML."""
    from dataclasses import asdict

    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(asdict(cfg), f, allow_unicode=True, sort_keys=False)
