"""Schema compatibility aliases.

The authoritative DDL lives in the versioned migrations under
``store/migrations/`` (see ``migrations/v001_baseline.py``).  This module keeps
backwards-compatible aliases for code that still imports the old flat
constants (e.g. manual test scripts that run ``executescript(SCHEMA)``).

Do **not** add new DDL here -- add a new migration instead.
"""

from __future__ import annotations

from .migrations.v001_baseline import (
    MYSQL_SCHEMA,
    SQLITE_SCHEMA,
)

#: Backwards-compatible alias used by legacy manual tests / scripts.
SCHEMA: str = SQLITE_SCHEMA

__all__ = [
    "SCHEMA",
    "SQLITE_SCHEMA",
    "MYSQL_SCHEMA",
]
