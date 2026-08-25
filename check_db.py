"""本地 SQLite 快速检查脚本（开发调试用）。

安全约定：
* 路径参数化（默认当前目录 data.db），不再硬编码机器专属绝对路径；
* key_cipher 等敏感列只输出长度/前缀掩码，绝不原样打印密文或明文。
"""

import sys
from pathlib import Path

import sqlite3

db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data.db")
if not db_path.exists():
    print(f"db not found: {db_path}")
    raise SystemExit(1)

c = sqlite3.connect(str(db_path))

SENSITIVE_COLS = {"key_cipher", "token_cipher", "token", "secret", "password_hash"}


def _safe_row(cols, row):
    out = []
    for name, val in zip(cols, row):
        if name.lower() in SENSITIVE_COLS and isinstance(val, (bytes, str)):
            s = val.decode("utf-8", "replace") if isinstance(val, bytes) else val
            out.append(f"<{name}:{len(s)} chars:{s[:4]}...>")
        else:
            out.append(val)
    return tuple(out)


def dump(table: str, limit: int = 3) -> None:
    print(f"== {table} schema ==")
    cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
    for r in c.execute(f"PRAGMA table_info({table})"):
        print(r)
    print()
    print(f"== {table} sample ==")
    for r in c.execute(f"SELECT * FROM {table} LIMIT {limit}"):
        print(_safe_row(cols, r))
    print()


dump("api_keys")
dump("models")
