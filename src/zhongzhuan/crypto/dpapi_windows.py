"""Windows DPAPI encryption via ctypes."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wt.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def dpapi_protect(plaintext: bytes) -> bytes:
    """Encrypt data using CryptProtectData (user-bound)."""
    in_blob = DATA_BLOB(
        cbData=len(plaintext),
        pbData=ctypes.cast(ctypes.c_char_p(plaintext), ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return b"WIN:" + buf


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """Decrypt data using CryptUnprotectData."""
    if ciphertext.startswith(b"WIN:"):
        ciphertext = ciphertext[4:]
    in_blob = DATA_BLOB(
        cbData=len(ciphertext),
        pbData=ctypes.cast(ctypes.c_char_p(ciphertext), ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return buf