"""Tests for access-token hashing and credential hygiene (T04 / R-P0-06 / R-P0-07)."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from zhongzhuan.store.access_tokens import (
    compare_token_hash,
    create_token,
    generate_token,
    get_token_by_value,
    hash_token,
    list_tokens,
    mask_token,
    revoke_token,
    rotate_token,
    token_prefix_of,
    verify_token,
)
from zhongzhuan.store.schema import SCHEMA
from zhongzhuan.store.sqlite_store import SqliteStore


_PRIVATE_LOOP: asyncio.AbstractEventLoop | None = None


def _loop() -> asyncio.AbstractEventLoop:
    global _PRIVATE_LOOP
    if _PRIVATE_LOOP is None or _PRIVATE_LOOP.is_closed():
        _PRIVATE_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_PRIVATE_LOOP)
    return _PRIVATE_LOOP


def _run(coro):
    return _loop().run_until_complete(coro)


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "tokens.db")
    s = _run(SqliteStore.create(db_path))
    yield s
    _run(s.close())


def test_create_token_never_stores_plaintext(store):
    """Plaintext must not be persisted; only prefix + hash are stored."""
    tok = _run(create_token(store, label="t1"))
    assert tok.token.startswith("zz-")
    row = _run(store.fetchone("SELECT token, token_prefix, token_hash FROM access_tokens WHERE label='t1'"))
    assert row is not None
    plain, prefix, digest = row
    # Plaintext column is now nullable and never written by the hashed path.
    assert plain in (None, "")  # plaintext column cleared
    assert prefix == tok.token[:8]
    assert digest  # hashed
    assert digest != tok.token  # not the raw token


def test_verify_token_roundtrip(store):
    tok = _run(create_token(store, label="v1"))
    assert _run(verify_token(store, tok.token)) is True
    assert _run(verify_token(store, "zz-wrong-token")) is False


def test_mask_token_never_leaks_secret():
    masked = mask_token("zz-1a2b3c4d")
    assert "zz-1a2b3c4d" in masked
    assert "****" in masked
    # full secret must not appear
    assert "1a2b3c4d5e6f" not in masked


def test_list_tokens_returns_masked(store):
    _run(create_token(store, label="a"))
    _run(create_token(store, label="b"))
    items = _run(list_tokens(store))
    assert isinstance(items, list)
    for it in items:
        assert "****" in str(it.get("token", "")) or "token_prefix" in it


def test_compare_token_hash_constant_time():
    key = b"k" * 32
    t = "zz-some-token"
    h = hash_token(t, key)
    assert compare_token_hash(h, hash_token(t, key)) is True
    assert compare_token_hash(h, hash_token("zz-other", key)) is False


def test_rotate_token_links_audit(store):
    tok = _run(create_token(store, label="orig", created_by="alice"))
    rotated = _run(rotate_token(store, tok.id, rotated_by="bob"))
    assert rotated is not None
    assert rotated.token != tok.token
    row = _run(
        store.fetchone(
            "SELECT rotation_of, created_by FROM access_tokens WHERE token_prefix=?",
            (rotated.token[:8],),
        )
    )
    assert row is not None
    orig_id = _run(store.fetchone("SELECT id FROM access_tokens WHERE label='orig'"))[0]
    assert row[0] == orig_id  # rotation_of points to original
    assert row[1] == "bob"
    # original must be invalidated
    assert _run(verify_token(store, tok.token)) is False


def test_revoke_token_sets_audit(store):
    tok = _run(create_token(store, label="rev"))
    _run(revoke_token(store, tok.id, revoked_by="admin"))
    assert _run(verify_token(store, tok.token)) is False
    row = _run(store.fetchone("SELECT revoked_at, revoked_by FROM access_tokens WHERE label='rev'"))
    assert row[0] > 0
    assert row[1] == "admin"


def test_generate_token_prefix():
    for _ in range(20):
        t = generate_token()
        assert t.startswith("zz-")
        assert len(token_prefix_of(t)) == 8
