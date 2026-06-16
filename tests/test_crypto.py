"""Crypto tests."""
import os
os.environ["ZHONGZHUAN_DEV_NO_DPAPI"] = "1"

from zhongzhuan.crypto import encrypt, decrypt, mask


def test_encrypt_decrypt_roundtrip():
    plain = b"sk-test-key-123"
    cipher = encrypt(plain)
    assert cipher != plain
    assert decrypt(cipher) == plain


def test_mask_short_key():
    assert mask("sk-abc") == "***"


def test_mask_long_key():
    masked = mask("sk-verylongkey123456")
    assert masked == "sk-v***3456"
    assert "verylongkey" not in masked


def test_mask():
    masked = mask("sk-1234567890abcdef")
    assert masked.startswith("sk-1")
    assert masked.endswith("cdef")
    assert "***" in masked