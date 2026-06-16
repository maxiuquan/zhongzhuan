"""Key encryption/decryption: AES-GCM (primary), DPAPI (Windows legacy), dev stub (fallback)."""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from .aes_cipher import encrypt as aes_encrypt, decrypt as aes_decrypt, generate_key as aes_generate_key

_aes_key: bytes | None = None
_data_dir: Path | None = None


async def init(data_dir: Path, store_get_key=None) -> None:
    """Initialize crypto: load or generate AES key from data_dir or TiDB."""
    global _aes_key, _data_dir
    _data_dir = data_dir

    # Try TiDB system_config first
    if store_get_key is not None:
        try:
            key_hex = await store_get_key("secret_key")
            if key_hex:
                _aes_key = bytes.fromhex(key_hex)
                return
        except Exception:
            pass

    # Try local key file
    key_file = data_dir / "secret.key"
    if key_file.exists():
        _aes_key = key_file.read_bytes()
        if len(_aes_key) == 32:
            return

    # Generate new key
    _aes_key = aes_generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(_aes_key)


def _get_key() -> bytes:
    if _aes_key is None:
        raise RuntimeError("crypto not initialized, call crypto.init() first")
    return _aes_key


def encrypt(plaintext: bytes) -> bytes:
    """Encrypt plaintext. Uses AES-GCM."""
    return aes_encrypt(plaintext, _get_key())


def decrypt(ciphertext: bytes) -> bytes:
    """Decrypt ciphertext. Handles AES:/WIN:/DEV: prefixes."""
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")

    if ciphertext.startswith(b"AES:"):
        return aes_decrypt(ciphertext, _get_key())

    if ciphertext.startswith(b"WIN:"):
        from .dpapi_windows import dpapi_unprotect
        return dpapi_unprotect(ciphertext[4:])

    if ciphertext.startswith(b"DEV:"):
        return base64.b64decode(ciphertext[4:])

    # Legacy: no prefix, try DPAPI (Windows)
    if sys.platform == "win32":
        from .dpapi_windows import dpapi_unprotect
        return dpapi_unprotect(ciphertext)

    raise RuntimeError("Unknown ciphertext format")


def mask(plaintext: str) -> str:
    """Mask key: keep first 4 + last 4, middle ***."""
    if len(plaintext) <= 8:
        return "***"
    return f"{plaintext[:4]}***{plaintext[-4:]}"