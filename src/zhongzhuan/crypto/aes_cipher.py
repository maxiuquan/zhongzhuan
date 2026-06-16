"""AES-256-GCM encryption for cross-platform key protection."""
from __future__ import annotations

import os
import base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_key() -> bytes:
    """Generate a random 256-bit AES key."""
    return AESGCM.generate_key(bit_length=256)


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt plaintext with AES-256-GCM. Returns AES:base64(nonce + ciphertext)."""
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext, None)
    return b"AES:" + base64.b64encode(nonce + ct)


def decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """Decrypt AES-256-GCM ciphertext."""
    data = base64.b64decode(ciphertext[4:])  # strip "AES:"
    nonce, ct = data[:12], data[12:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None)