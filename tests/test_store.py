"""SQLite store tests."""
from zhongzhuan.store import Store


def test_open_applies_migrations(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    try:
        conn = s.connect()
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='models'"
        )
        assert cur.fetchone()[0] == 1
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='api_keys'"
        )
        assert cur.fetchone()[0] == 1
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='request_logs'"
        )
        assert cur.fetchone()[0] == 1
    finally:
        s.close()