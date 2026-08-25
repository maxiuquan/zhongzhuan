"""Admin JWT authentication middleware + security hardening (T32).

覆盖 R-P2-02 / R-P2-03 / R-P2-04：

- R-P2-02：生产模式下管理端鉴权**默认开启**（``auth_enabled()`` 无显式配置时
  跟随 ``ZHONGZHUAN_ENV=production`` 返回 True）；显式关闭必须同时确认
  ``ZHONGZHUAN_ALLOW_INSECURE_DISABLE``（config.py 校验层强制）。
- R-P2-03：CSRF 防护（生产模式写操作需要 ``X-CSRF-Token``）、登录限速、
  审计日志、安全响应头（HSTS / X-Content-Type-Options / X-Frame-Options / CSP）。
- R-P2-04：JWT secret 未配置时生产模式 **fail closed**（不再进程内随机生成）；
  支持 secret 轮换（``ZHONGZHUAN_JWT_SECRET_PREVIOUS`` 逗号分隔旧 secret），
  旧 token 在 ``ZHONGZHUAN_JWT_GRACE_PERIOD_SECONDS`` 宽限期内仍可验证。
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time

import jwt
from aiohttp import web
from aiohttp.typedefs import Middleware

from ..config import ConfigError

# JWT secret - auto-generated if not set (dev only, T32: prod fails closed)
_SECRET: str = ""
_PREVIOUS_SECRETS: list[str] = []
_GRACE_PERIOD: float = 3600.0
_TOKEN_EXPIRY = 86400  # 24 hours

# ---- R-P2-03: login rate limiting state (keyed by (client IP, username)) ----
_LOGIN_FAILURES: dict[tuple[str, str], list[float]] = {}
#: 失败记录最长保留时长（顺带清理，防内存无限增长）
_FAILURE_RETENTION = 24 * 3600.0
#: request 存储键：中间件里解析出的登录用户名（供成功后精准重置限速）。
_LOGIN_USERNAME_KEY = "zz_login_username"

#: 可注入时钟（T33：限速测试零真实等待）。默认 ``time.monotonic``；
#: 测试通过 :func:`set_login_clock` 换成假时钟。
_MONOTONIC_CLOCK = time.monotonic


def set_login_clock(clock) -> None:
    """Test hook: 替换限速用的单调时钟（假时钟推进窗口即可测「窗口过期后恢复」）。"""
    global _MONOTONIC_CLOCK
    _MONOTONIC_CLOCK = clock if clock is not None else time.monotonic


def _now() -> float:
    return _MONOTONIC_CLOCK()


# ---- R-P2-03: CSRF double-submit cookie ----
_CSRF_COOKIE = "zhongzhuan_csrf"
_CSRF_HEADER = "X-CSRF-Token"
_csrf_token: str = ""

_WHITELIST = {"/api/auth/login", "/api/auth/status"}
#: 无状态写操作：login 需要豁免 CSRF（它是获得会话的唯一入口）。
_CSRF_EXEMPT = {"/api/auth/login"}
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def env_is_production() -> bool:
    return os.getenv("ZHONGZHUAN_ENV", "").strip().lower() in ("production", "prod")


# ---------------------------------------------------------------------------
# JWT secret lifecycle (R-P2-04)
# ---------------------------------------------------------------------------


def init_jwt_secret(
    env: str | None = None,
    secret: str | None = None,
    previous: list[str] | str | None = None,
    grace: float | int | None = None,
) -> None:
    """Initialize the JWT signing secret(s).

    Production 下缺 secret 抛 :class:`ConfigError`（fail closed）；开发模式
    生成随机 secret 并告警。``previous`` 为轮换前的旧 secret 列表（支持
    逗号分隔的 env 字符串），``grace`` 为旧 token 宽限期（秒）。
    """
    global _SECRET, _PREVIOUS_SECRETS, _GRACE_PERIOD

    mode = env or os.getenv("ZHONGZHUAN_ENV", "") or "development"
    is_prod = mode.strip().lower() in ("production", "prod")

    if secret is not None:
        _SECRET = secret
    else:
        secret = os.getenv("ZHONGZHUAN_JWT_SECRET", "")
        if not secret:
            if is_prod:
                raise ConfigError(
                    "ZHONGZHUAN_JWT_SECRET is required in production (fail closed, no in-process random generation)"
                )
            _SECRET = secrets.token_hex(32)
            _warn_dev_random_secret()
        else:
            _SECRET = secret

    if previous is not None:
        prev_list = (
            previous if isinstance(previous, list) else [p.strip() for p in str(previous).split(",") if p.strip()]
        )
    else:
        raw = os.getenv("ZHONGZHUAN_JWT_SECRET_PREVIOUS", "")
        prev_list = [p.strip() for p in raw.split(",") if p.strip()]
    _PREVIOUS_SECRETS = [p for p in prev_list if p and p != _SECRET]

    if grace is not None:
        grace_val = grace
    else:
        raw_grace = os.getenv("ZHONGZHUAN_JWT_GRACE_PERIOD_SECONDS", "3600")
        try:
            grace_val = float(raw_grace)
        except (TypeError, ValueError):
            grace_val = 3600.0
    _GRACE_PERIOD = max(0.0, float(grace_val))


def _warn_dev_random_secret() -> None:
    message = (
        "ZHONGZHUAN_JWT_SECRET not set; generated a random development secret "
        "(insecure). Set it explicitly before going to production."
    )
    try:
        from loguru import logger

        logger.warning(f"[auth] {message}")
    except Exception:  # pragma: no cover
        pass


def create_token(username: str, password_hash: str | None = None) -> str:
    """Create a JWT token for admin user (signed with the current secret).

    ``password_hash`` 提供时把其指纹写入 claims["pv"]（password version），
    使改密后旧 JWT 自然失效（见 :func:`token_revoked`）。
    """
    now = int(time.time())
    claims: dict = {"sub": username, "iat": now, "exp": now + _TOKEN_EXPIRY}
    if password_hash:
        claims["pv"] = password_fingerprint(password_hash)
    return jwt.encode(claims, _SECRET, algorithm="HS256")


def password_fingerprint(password_hash: str) -> str:
    """密码哈希指纹（sha256 前 16 hex），用于 JWT 可吊销比对，不泄露原哈希。"""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def _decode_payload(token: str, now: int | None = None) -> dict | None:
    """验证签名（当前 secret 优先，旧 secret 走宽限期），返回 claims 或 None。"""
    try:
        return jwt.decode(token, _SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        pass

    if not _PREVIOUS_SECRETS:
        return None
    ts = now if now is not None else int(time.time())
    for old in _PREVIOUS_SECRETS:
        try:
            payload = jwt.decode(token, old, algorithms=["HS256"])
        except jwt.PyJWTError:
            continue
        iat = payload.get("iat", 0)
        if ts - iat < _GRACE_PERIOD:
            return payload
    return None


def verify_token(token: str, now: int | None = None) -> str | None:
    """Verify JWT token, return username if valid.

    轮换宽限期：当前 secret 验不过时，依次尝试旧 secret；旧 token 的
    ``iat`` 必须在宽限期（``now - iat < _GRACE_PERIOD``）内才算有效。
    """
    payload = _decode_payload(token, now)
    return payload.get("sub") if payload else None


async def token_revoked(store, token: str) -> bool:
    """JWT 是否已因改密被吊销（claims["pv"] 与当前密码哈希指纹不一致）。

    - 无 ``pv`` 的 token（历史版本/测试直发）与用户不在库中时无从比对，
      视为未吊销；登录签发的 token 均带 ``pv``。
    - 每次请求只做一次 admin_users 主键点查（username 唯一索引）。
    """
    payload = _decode_payload(token)
    if payload is None:
        return True
    pv = payload.get("pv")
    if not pv:
        return False
    row = await store.fetchone(
        "SELECT password_hash FROM admin_users WHERE username=?",
        (payload.get("sub") or "",),
    )
    if not row:
        return False
    return not secrets.compare_digest(str(pv), password_fingerprint(row[0]))


# ---------------------------------------------------------------------------
# R-P2-02: auth default
# ---------------------------------------------------------------------------


def auth_enabled() -> bool:
    """管理端鉴权：显式 ``ZHONGZHUAN_ADMIN_AUTH`` 优先；未配置时生产默认开启。"""
    raw = os.getenv("ZHONGZHUAN_ADMIN_AUTH")
    if raw is not None:
        return raw.strip().lower() == "true"
    return env_is_production()


def csrf_enabled() -> bool:
    """CSRF 防护开关：显式 ``ZHONGZHUAN_CSRF_ENABLED`` 优先；生产默认开启。"""
    raw = os.getenv("ZHONGZHUAN_CSRF_ENABLED")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return env_is_production()


# ---------------------------------------------------------------------------
# R-P2-03: login rate limiting
# ---------------------------------------------------------------------------


def _login_key(ip: str, username: str) -> tuple[str, str]:
    """限速键：IP + 用户名双维（单 IP 打多账号 / 单账号多 IP 都能拦）。"""
    return (ip or "unknown", (username or "").strip().lower()[:128])


async def _login_username(request: web.Request) -> str:
    """从登录请求体里取用户名；任何异常（假对象/坏 JSON）都回退空串。"""
    try:
        data = await request.json()
    except Exception:
        return ""
    if isinstance(data, dict):
        return str(data.get("username", ""))
    return ""


def _rate_limit_max() -> int:
    try:
        return max(1, int(os.getenv("ZHONGZHUAN_LOGIN_RATE_LIMIT_MAX", "10")))
    except ValueError:
        return 10


def _rate_limit_window() -> int:
    try:
        return max(1, int(os.getenv("ZHONGZHUAN_LOGIN_RATE_LIMIT_WINDOW", "300")))
    except ValueError:
        return 300


def _check_login_rate(request: web.Request, username: str = "") -> bool:
    key = _login_key(request.remote, username)
    now = _now()
    window = _rate_limit_window()
    recent = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < window]
    return len(recent) < _rate_limit_max()


def _record_login_failure(request: web.Request, username: str = "") -> None:
    key = _login_key(request.remote, username)
    now = _now()
    window = _rate_limit_window()
    # 顺带清理超过保留期的旧项，防止 _LOGIN_FAILURES 无限增长。
    stale = [k for k, ts in _LOGIN_FAILURES.items() if ts and now - ts[-1] >= _FAILURE_RETENTION]
    for k in stale:
        _LOGIN_FAILURES.pop(k, None)
    recent = [t for t in _LOGIN_FAILURES.get(key, []) if now - t < window]
    _LOGIN_FAILURES[key] = recent + [now]


def _reset_login_failures(request: web.Request | None = None) -> None:
    if request is None:
        _LOGIN_FAILURES.clear()
    else:
        username = request.get(_LOGIN_USERNAME_KEY, "") if hasattr(request, "get") else ""
        _LOGIN_FAILURES.pop(_login_key(request.remote, username), None)


def reset_login_failures() -> None:
    """Test hook: clear the login rate-limit state."""
    _LOGIN_FAILURES.clear()


# ---------------------------------------------------------------------------
# R-P2-03: CSRF + security headers + audit
# ---------------------------------------------------------------------------


def _get_csrf_token() -> str:
    global _csrf_token
    if not _csrf_token:
        _csrf_token = secrets.token_urlsafe(32)
    return _csrf_token


def _set_csrf_cookie(resp: web.StreamResponse) -> None:
    resp.set_cookie(
        _CSRF_COOKIE,
        _get_csrf_token(),
        path="/",
        samesite="Lax",
        httponly=False,
    )


def _valid_csrf(request: web.Request) -> bool:
    """CSRF double-submit：header 必须与 cookie 严格一致；无 cookie 一律拒绝。"""
    header = request.headers.get(_CSRF_HEADER, "")
    cookie = request.cookies.get(_CSRF_COOKIE, "")
    if not header or not cookie:
        return False
    return secrets.compare_digest(header, cookie)


def _is_write(request: web.Request) -> bool:
    return request.method in _WRITE_METHODS


def _add_security_headers(resp: web.StreamResponse) -> None:
    """R-P2-03: HSTS / X-Content-Type-Options / X-Frame-Options / CSP。"""
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
    )
    resp.headers.setdefault("Referrer-Policy", "no-referrer")


def _audit(event: str, request: web.Request, **extra) -> None:
    try:
        from loguru import logger

        ctx = " ".join(f"{k}={v}" for k, v in extra.items())
        logger.info(
            f"[audit] {event} ip={request.remote} method={request.method} path={request.path}{' ' + ctx if ctx else ''}"
        )
    except Exception:  # pragma: no cover
        pass


def _wrap(request: web.Request, resp: web.StreamResponse) -> web.StreamResponse:
    """Attach security headers + CSRF cookie, then run audit hooks."""
    _add_security_headers(resp)
    _set_csrf_cookie(resp)
    if request.path == "/api/auth/login" and request.method == "POST":
        if resp.status in (200, 201):
            _reset_login_failures(request)
            _audit("admin.login.success", request)
        elif resp.status == 401:
            username = request.get(_LOGIN_USERNAME_KEY, "") if hasattr(request, "get") else ""
            _record_login_failure(request, username)
            _audit("admin.login.failure", request)
    elif _is_write(request) and request.path.startswith("/api/"):
        _audit("admin.write", request, status=resp.status)
    return resp


def make_auth_middleware(store=None) -> Middleware:
    """Create the admin security middleware (JWT + CSRF + rate limit + headers).

    ``store`` 提供时启用 JWT 可吊销校验（pv 指纹与当前密码哈希比对）。
    """

    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        # 1. Login rate limiting (R-P2-03) —— 始终生效，IP+用户名双维。
        if request.path == "/api/auth/login" and request.method == "POST":
            username = await _login_username(request)
            request[_LOGIN_USERNAME_KEY] = username
            if not _check_login_rate(request, username):
                resp = web.json_response(
                    {"error": "too many failed login attempts", "type": "rate_limited"},
                    status=429,
                    headers={"Retry-After": str(_rate_limit_window())},
                )
                return _wrap(request, resp)

        # 2. CSRF (R-P2-03) —— 生产模式写操作必须携带 token。
        if (
            csrf_enabled()
            and _is_write(request)
            and request.path.startswith("/api/")
            and request.path not in _CSRF_EXEMPT
        ):
            if not _valid_csrf(request):
                resp = web.json_response(
                    {"error": "missing or invalid CSRF token", "type": "csrf_required"},
                    status=403,
                )
                return _wrap(request, resp)

        # 3. JWT 鉴权 (R-P2-02 / R-P2-04) + 可吊销校验（pv 指纹）。
        if not auth_enabled():
            return _wrap(request, await handler(request))

        if request.path in _WHITELIST:
            return _wrap(request, await handler(request))
        if request.path == "/" or not request.path.startswith("/api/"):
            return _wrap(request, await handler(request))

        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not token or not verify_token(token):
            return _wrap(
                request,
                web.json_response({"error": "unauthorized"}, status=401),
            )
        if store is not None and await token_revoked(store, token):
            return _wrap(
                request,
                web.json_response({"error": "token revoked (password changed)", "type": "token_revoked"}, status=401),
            )

        return _wrap(request, await handler(request))

    return middleware
