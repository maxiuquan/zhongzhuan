"""Key encryption/decryption: Windows DPAPI; dev stub for other platforms."""
from __future__ import annotations

import base64
import os
import sys


def _is_dev_stub() -> bool:
    if os.environ.get("ZHONGZHUAN_DEV_NO_DPAPI") == "1":
        return True
    return sys.platform != "win32"


def encrypt(plaintext: bytes) -> bytes:
    if _is_dev_stub():
        return b"DEV:" + base64.b64encode(plaintext)
    from .dpapi_windows import dpapi_protect
    return dpapi_protect(plaintext)


def decrypt(ciphertext: bytes) -> bytes:
    if ciphertext.startswith(b"DEV:"):
        return base64.b64decode(ciphertext[4:])
    if ciphertext.startswith(b"WIN:"):
        from .dpapi_windows import dpapi_unprotect
        return dpapi_unprotect(ciphertext[4:])
    from .dpapi_windows import dpapi_unprotect
    return dpapi_unprotect(ciphertext)


def mask(plaintext: str) -> str:
    """Mask key: keep first 4 + last 4, middle ***."""
    if len(plaintext) <= 8:
        return "***"
    return f"{plaintext[:4]}***{plaintext[-4:]}"