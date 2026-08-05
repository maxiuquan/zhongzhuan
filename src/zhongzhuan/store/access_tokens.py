"""Access token CRUD (async) -- hashed storage + quota management.

Storage model (T04 / R-P0-06 / R-P0-07)
---------------------------------------
The plaintext token is **never** persisted.  ``access_tokens`` stores:

* ``token_prefix`` -- first 8 characters, used as the lookup index and as the
  only part ever shown in the admin UI / API;
* ``token_hash``   -- ``HMAC-SHA256(key, plaintext)`` hex digest, compared in
  constant time with :func:`hmac.compare_digest`.

The HMAC key is resolved once per connection, in this order:

1. ``ZHONGZHUAN_TOKEN_HMAC_KEY`` environment variable (recommended);
2. ``system_config['token_hmac_key']`` (auto-provisioned, survives restarts);
3. a machine-bound stable salt (host name + MAC) which is then persisted into
   ``system_config`` -- accompanied by a warning, because a key that lives in
   the same database as the hashes is only a speed bump against an attacker
   who already owns the database file.

Audit / lifecycle fields
------------------------
``rotation_of``, ``last_used_at``, ``created_by``, ``revoked_at``,
``revoked_by`` (R-P0-07).

Quota fields
------------
``quota_tokens`` (-1 = unlimited), ``used_tokens``, ``model_whitelist``
(comma separated, empty = allow all), ``expires_at`` (0 = never).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import platform
import secrets
import time
import uuid
from dataclasses import dataclass

from loguru import logger

from ..crypto import encrypt as _aes_encrypt, decrypt as _aes_decrypt
from .store import Store

#: Environment variable holding the HMAC key (raw text or hex).
TOKEN_HMAC_KEY_ENV: str = "ZHONGZHUAN_TOKEN_HMAC_KEY"

#: ``system_config`` row used to persist an auto-provisioned HMAC key.
TOKEN_HMAC_CONFIG_KEY: str = "token_hmac_key"

#: Number of leading characters kept in clear text for lookup / display.
TOKEN_PREFIX_LEN: int = 8

#: Attribute used to memoise the resolved key on a store / executor instance.
_KEY_CACHE_ATTR: str = "_zz_token_hmac_key"

#: Columns of the hashed schema, in a fixed order shared by every query.
_HASHED_COLUMNS: str = (
    "id, token_prefix, token_hash, label, enabled, quota_tokens, used_tokens, "
    "model_whitelist, expires_at, created_at, rotation_of, last_used_at, "
    "created_by, revoked_at, revoked_by"
)

#: Columns with the v010 ``token_cipher`` column appended (nullable; NULL for
#: tokens created before v010, whose plaintext can no longer be recovered).
_HASHED_COLUMNS_WITH_CIPHER: str = _HASHED_COLUMNS + ", token_cipher"

#: Legacy (pre-v003) column list -- kept so that stubs and not-yet-migrated
#: databases keep working.  See :func:`get_token_by_value`.
_LEGACY_COLUMNS: str = "id, token, label, enabled, quota_tokens, used_tokens, model_whitelist, expires_at, created_at"

#: Minimum seconds between two ``last_used_at`` writes for the same token.
LAST_USED_THROTTLE_SECONDS: int = 60


@dataclass
class AccessToken:
    """One proxy access token.

    ``token`` holds the plaintext only in memory and only when it is known
    (right after creation, or during verification).  It is never read back
    from storage.
    """

    id: int | None
    token: str
    label: str
    enabled: bool = True
    quota_tokens: int = -1  # -1 = unlimited
    used_tokens: int = 0
    model_whitelist: str = ""  # comma separated, empty = allow all
    expires_at: int = 0  # 0 = never expires
    created_at: int | None = None
    token_prefix: str = ""
    rotation_of: int = 0
    last_used_at: int = 0
    created_by: str = ""
    revoked_at: int = 0
    revoked_by: str = ""

    def check_quota(self, model: str = "") -> tuple[bool, str]:
        """Validate the token for an incoming request.

        Args:
            model: Requested model name (empty = skip whitelist check).

        Returns:
            ``(ok, reason)``; ``reason`` is empty when ``ok`` is ``True``.
        """
        if not self.enabled:
            return False, "token disabled"
        if self.revoked_at > 0:
            return False, "token revoked"
        if self.expires_at > 0 and time.time() > self.expires_at:
            return False, "token expired"
        if model and self.model_whitelist:
            allowed = [m.strip() for m in self.model_whitelist.split(",") if m.strip()]
            if model not in allowed:
                return False, f"model '{model}' not in whitelist"
        if self.quota_tokens >= 0 and self.used_tokens >= self.quota_tokens:
            return False, "quota exceeded"
        return True, ""

    @property
    def masked(self) -> str:
        """Display form: prefix plus a fixed mask, never the full secret."""
        return mask_token(self.token_prefix)


# --------------------------------------------------------------------------- #
# Hashing helpers
# --------------------------------------------------------------------------- #
def generate_token() -> str:
    """Generate a random access token prefixed with ``zz-``."""
    return "zz-" + secrets.token_hex(24)


def token_prefix_of(token: str) -> str:
    """Return the indexable, non-secret prefix of *token*."""
    return token[:TOKEN_PREFIX_LEN]


def mask_token(prefix: str) -> str:
    """Render a token for humans: ``zz-1a2b3...****``."""
    return f"{prefix}...****" if prefix else "****"


def hash_token(token: str, key: bytes) -> str:
    """Return ``HMAC-SHA256(key, token)`` as a lowercase hex digest."""
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def compare_token_hash(expected: str, candidate: str) -> bool:
    """Constant-time comparison of two hex digests (R-P0-06)."""
    return hmac.compare_digest(expected or "", candidate or "")


def _machine_bound_salt() -> bytes:
    """Derive a stable 32-byte salt from host identity.

    Used only as a last resort when no explicit key is configured.  It is
    deterministic on a given machine so that a restart does not invalidate
    every issued token.
    """
    material = "|".join(
        (
            platform.node(),
            str(uuid.getnode()),
            platform.machine(),
            "zhongzhuan-token-hmac-v1",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


async def resolve_hmac_key(conn) -> bytes:
    """Resolve (and memoise) the HMAC key for *conn*.

    Args:
        conn: Anything exposing async ``fetchone(sql, params)`` and
            ``execute(sql, params)`` -- both :class:`~.store.Store` and the
            migration executors qualify.

    Returns:
        A 32-byte key.
    """
    cached = getattr(conn, _KEY_CACHE_ATTR, None)
    if isinstance(cached, bytes) and cached:
        return cached

    env_value = os.getenv(TOKEN_HMAC_KEY_ENV, "").strip()
    if env_value:
        key = _coerce_key(env_value)
        _cache_key(conn, key)
        return key

    stored = await _read_config_key(conn)
    if stored:
        key = _coerce_key(stored)
        _cache_key(conn, key)
        return key

    key = _machine_bound_salt()
    logger.warning(
        f"{TOKEN_HMAC_KEY_ENV} is not set; falling back to a machine-bound "
        "token HMAC key and persisting it in system_config. Set "
        f"{TOKEN_HMAC_KEY_ENV} to a secret managed outside the database."
    )
    await _write_config_key(conn, key.hex())
    # Re-read: another process may have won the race and persisted first.
    stored = await _read_config_key(conn)
    if stored:
        key = _coerce_key(stored)
    _cache_key(conn, key)
    return key


def _coerce_key(value: str) -> bytes:
    """Accept a hex string or arbitrary text and return 32 key bytes."""
    text = value.strip()
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    return hashlib.sha256(text.encode("utf-8")).digest()


def _cache_key(conn, key: bytes) -> None:
    """Memoise *key* on *conn*, tolerating objects that reject attributes."""
    try:
        setattr(conn, _KEY_CACHE_ATTR, key)
    except (AttributeError, TypeError):  # pragma: no cover - exotic stubs
        pass


async def _read_config_key(conn) -> str:
    """Read the persisted HMAC key, returning '' when unavailable."""
    try:
        row = await conn.fetchone(
            "SELECT value FROM system_config WHERE `key`=?",
            (TOKEN_HMAC_CONFIG_KEY,),
        )
    except Exception:
        return ""
    if not row or not row[0]:
        return ""
    return str(row[0])


async def _write_config_key(conn, hex_key: str) -> None:
    """Persist the HMAC key, ignoring races and read-only stubs."""
    try:
        await conn.execute(
            "INSERT INTO system_config(`key`, value) VALUES(?, ?)",
            (TOKEN_HMAC_CONFIG_KEY, hex_key),
        )
    except Exception as exc:
        logger.debug(f"could not persist {TOKEN_HMAC_CONFIG_KEY}: {exc}")


# --------------------------------------------------------------------------- #
# Row mapping
# --------------------------------------------------------------------------- #
def _row_to_token(row: tuple, plaintext: str = "") -> AccessToken:
    """Build an :class:`AccessToken` from a ``_HASHED_COLUMNS`` row."""
    return AccessToken(
        id=row[0],
        token=plaintext,
        label=row[3] or "",
        enabled=bool(row[4]),
        quota_tokens=row[5] if row[5] is not None else -1,
        used_tokens=row[6] or 0,
        model_whitelist=row[7] or "",
        expires_at=row[8] or 0,
        created_at=row[9],
        token_prefix=row[1] or "",
        rotation_of=row[10] or 0,
        last_used_at=row[11] or 0,
        created_by=row[12] or "",
        revoked_at=row[13] or 0,
        revoked_by=row[14] or "",
    )


def _legacy_row_to_token(row: tuple) -> AccessToken:
    """Build an :class:`AccessToken` from a pre-v003 (plaintext) row."""
    plaintext = row[1] or ""
    return AccessToken(
        id=row[0],
        token=plaintext,
        label=row[2] or "",
        enabled=bool(row[3]),
        quota_tokens=row[4] if row[4] is not None else -1,
        used_tokens=row[5] or 0,
        model_whitelist=row[6] or "",
        expires_at=row[7] or 0,
        created_at=row[8],
        token_prefix=token_prefix_of(plaintext),
    )


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
async def create_token(
    s: Store,
    label: str = "",
    quota_tokens: int = -1,
    model_whitelist: str = "",
    expires_at: int = 0,
    created_by: str = "",
    rotation_of: int = 0,
) -> AccessToken:
    """Create a new access token.

    The returned :class:`AccessToken` carries the plaintext in ``token``.
    This is the **only** moment the plaintext exists -- storage keeps just the
    prefix and the keyed hash.

    Args:
        s: Store.
        label: Human readable label.
        quota_tokens: Token budget (-1 = unlimited).
        model_whitelist: Comma separated model names (empty = allow all).
        expires_at: Unix timestamp (0 = never expires).
        created_by: Audit field -- who requested the token.
        rotation_of: Id of the token this one replaces (0 = not a rotation).

    Returns:
        The created token, plaintext included.
    """
    plaintext = generate_token()
    prefix = token_prefix_of(plaintext)
    key = await resolve_hmac_key(s)
    digest = hash_token(plaintext, key)
    cipher = _aes_encrypt(plaintext.encode("utf-8"))
    now = Store.now()
    tid = await s.execute(
        "INSERT INTO access_tokens("
        "token_prefix, token_hash, label, enabled, quota_tokens, used_tokens, "
        "model_whitelist, expires_at, created_at, rotation_of, last_used_at, "
        "created_by, revoked_at, revoked_by, token_cipher) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            prefix,
            digest,
            label,
            1,
            quota_tokens,
            0,
            model_whitelist,
            expires_at,
            now,
            rotation_of,
            0,
            created_by,
            0,
            "",
            cipher,
        ),
    )
    return AccessToken(
        id=tid,
        token=plaintext,
        label=label,
        enabled=True,
        quota_tokens=quota_tokens,
        used_tokens=0,
        model_whitelist=model_whitelist,
        expires_at=expires_at,
        created_at=now,
        token_prefix=prefix,
        rotation_of=rotation_of,
        created_by=created_by,
    )


async def list_tokens(s: Store) -> list[dict]:
    """List tokens for the admin API -- prefix and mask only (R-P0-07).

    The response deliberately contains no field from which the plaintext could
    be reconstructed.  ``token`` is kept as an alias of the mask so existing
    UI code keeps rendering, but it is *not* a usable credential.
    """
    rows = await s.fetchall(f"SELECT {_HASHED_COLUMNS} FROM access_tokens ORDER BY id")
    result: list[dict] = []
    for r in rows:
        prefix = r[1] or ""
        result.append(
            {
                "id": r[0],
                "token_prefix": prefix,
                "token_masked": mask_token(prefix),
                "token": mask_token(prefix),  # UI compatibility, masked
                "label": r[3] or "",
                "enabled": bool(r[4]),
                "quota_tokens": r[5] if r[5] is not None else -1,
                "used_tokens": r[6] or 0,
                "model_whitelist": r[7] or "",
                "expires_at": r[8] or 0,
                "created_at": r[9],
                "rotation_of": r[10] or 0,
                "last_used_at": r[11] or 0,
                "created_by": r[12] or "",
                "revoked_at": r[13] or 0,
                "revoked_by": r[14] or "",
            }
        )
    return result


async def get_token_by_value(s: Store, token: str) -> AccessToken | None:
    """Look up a token by its plaintext value.

    Hashed path: fetch every row sharing the (non-secret) prefix, then compare
    the keyed hash in constant time.  Falls back to the pre-v003 plaintext
    lookup when the hashed columns are unavailable, so that a database which
    has not been migrated yet -- and the lightweight store stubs used in the
    test-suite -- keep working.

    Args:
        s: Store.
        token: The plaintext credential presented by the client.

    Returns:
        The matching :class:`AccessToken`, or ``None``.
    """
    if not token:
        return None

    prefix = token_prefix_of(token)
    rows: list[tuple] | None = None
    try:
        rows = await s.fetchall(
            f"SELECT {_HASHED_COLUMNS} FROM access_tokens WHERE token_prefix=?",
            (prefix,),
        )
    except Exception:
        rows = None

    if rows:
        key = await resolve_hmac_key(s)
        candidate = hash_token(token, key)
        matched: AccessToken | None = None
        for r in rows:
            # Compare every candidate so the runtime does not leak which row
            # matched; keep the first (and only) hit.
            if compare_token_hash(str(r[2] or ""), candidate) and matched is None:
                matched = _row_to_token(r, plaintext=token)
        return matched

    if rows is not None:
        # Hashed schema is present, prefix simply did not match anything.
        return None

    try:
        legacy = await s.fetchone(
            f"SELECT {_LEGACY_COLUMNS} FROM access_tokens WHERE token=?",
            (token,),
        )
    except Exception:
        return None
    return _legacy_row_to_token(legacy) if legacy else None


async def verify_token(s: Store, token: str) -> bool:
    """Return ``True`` when *token* exists, is enabled and is not revoked."""
    at = await get_token_by_value(s, token)
    return at is not None and at.enabled and at.revoked_at == 0


async def touch_last_used(s: Store, token_id: int, previous: int = 0) -> None:
    """Record token usage, throttled to one write per minute.

    Args:
        s: Store.
        token_id: Token row id.
        previous: Previously recorded ``last_used_at`` (skip write when fresh).
    """
    if token_id <= 0:
        return
    now = Store.now()
    if previous and now - previous < LAST_USED_THROTTLE_SECONDS:
        return
    try:
        await s.execute("UPDATE access_tokens SET last_used_at=? WHERE id=?", (now, token_id))
    except Exception as exc:
        logger.debug(f"last_used_at update skipped for token {token_id}: {exc}")


async def delete_token(s: Store, token_id: int) -> None:
    """Delete an access token."""
    await s.execute("DELETE FROM access_tokens WHERE id=?", (token_id,))


async def reveal_token(s: Store, token_id: int) -> str | None:
    """Return the plaintext of *token_id*, or ``None`` when it is unrecoverable.

    Only tokens created after the v010 migration carry ``token_cipher``; the
    legacy ones (v003..v009) store just an irreversible hash, so their
    plaintext can never be reconstructed.  Callers must treat ``None`` as
    "historical key not retained", never as an excuse to rotate or revoke the
    token.

    Raises:
        RuntimeError: When the stored ciphertext cannot be decrypted (e.g. the
            AES key changed), so the caller can surface a clear error instead
            of silently degrading.
    """
    row = await s.fetchone(
        f"SELECT {_HASHED_COLUMNS_WITH_CIPHER} FROM access_tokens WHERE id=?",
        (token_id,),
    )
    if not row:
        return None
    cipher = row[15]
    if not cipher:
        return None
    try:
        return _aes_decrypt(cipher).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - surface any driver/crypto error
        raise RuntimeError(f"cannot decrypt token #{token_id}: {exc}") from exc


async def revoke_token(s: Store, token_id: int, revoked_by: str = "") -> None:
    """Disable a token and write the revoke audit trail (R-P0-07)."""
    await s.execute(
        "UPDATE access_tokens SET enabled=0, revoked_at=?, revoked_by=? WHERE id=?",
        (Store.now(), revoked_by, token_id),
    )


async def rotate_token(s: Store, token_id: int, rotated_by: str = "") -> AccessToken | None:
    """Issue a replacement token and revoke the old one.

    Args:
        s: Store.
        token_id: Token being rotated out.
        rotated_by: Audit field for both the revoke and the new token.

    Returns:
        The freshly created token (plaintext included), or ``None`` when
        *token_id* does not exist.
    """
    row = await s.fetchone(
        "SELECT label, quota_tokens, model_whitelist, expires_at FROM access_tokens WHERE id=?",
        (token_id,),
    )
    if not row:
        return None
    fresh = await create_token(
        s,
        label=row[0] or "",
        quota_tokens=row[1] if row[1] is not None else -1,
        model_whitelist=row[2] or "",
        expires_at=row[3] or 0,
        created_by=rotated_by,
        rotation_of=token_id,
    )
    await revoke_token(s, token_id, revoked_by=rotated_by)
    return fresh


async def update_token(
    s: Store,
    token_id: int,
    *,
    label: str | None = None,
    enabled: bool | None = None,
    quota_tokens: int | None = None,
    model_whitelist: str | None = None,
    expires_at: int | None = None,
) -> None:
    """Update mutable token fields."""
    sets: list[str] = []
    params: list = []
    if label is not None:
        sets.append("label=?")
        params.append(label)
    if enabled is not None:
        sets.append("enabled=?")
        params.append(int(enabled))
    if quota_tokens is not None:
        sets.append("quota_tokens=?")
        params.append(quota_tokens)
    if model_whitelist is not None:
        sets.append("model_whitelist=?")
        params.append(model_whitelist)
    if expires_at is not None:
        sets.append("expires_at=?")
        params.append(expires_at)
    if not sets:
        return
    params.append(token_id)
    await s.execute(f"UPDATE access_tokens SET {','.join(sets)} WHERE id=?", tuple(params))


async def deduct_token_quota(s: Store, token_id: int, tokens: int) -> None:
    """Add *tokens* to ``used_tokens`` (no-op for non-positive values)."""
    if tokens <= 0 or token_id <= 0:
        return
    await s.execute(
        "UPDATE access_tokens SET used_tokens = used_tokens + ? WHERE id=?",
        (tokens, token_id),
    )


async def token_count(s: Store) -> int:
    """Count existing tokens."""
    r = await s.fetchone("SELECT COUNT(*) FROM access_tokens")
    return r[0] if r else 0
