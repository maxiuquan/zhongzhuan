"""Non-Windows DPAPI stub: raises error."""
from __future__ import annotations


def dpapi_protect(plaintext: bytes) -> bytes:
    raise NotImplementedError("DPAPI is only available on Windows")


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    raise NotImplementedError("DPAPI is only available on Windows")