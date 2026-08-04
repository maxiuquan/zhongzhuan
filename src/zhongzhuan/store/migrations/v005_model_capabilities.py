"""v005 -- per-model capability declaration (T25 / R-P1-44).

Adds the two columns the :class:`~zhongzhuan.responses_v3.capability.CapabilityRouter`
routes on (§4.2.9):

    capabilities   -- comma-separated ``Capability`` values, e.g.
                      ``"code_interpreter,web_search"``; ``""`` = declares nothing
    upstream_mode  -- ``"bonded"`` (未声明) | ``"native"`` | ``"emulate"``
                      | ``"translate"``; ``"bonded"`` keeps the pre-v005 behaviour

Why a delimited string and not a join table
-------------------------------------------
The set is bounded at nine members, is read as a whole on every key load and is
never queried by element.  A join table would add a migration, a second query
per key and a cache-invalidation problem to buy a filter nobody performs.  The
column mirrors the existing ``models.aliases`` convention, so the CRUD layer
stays uniform.

Why the values stay strings
---------------------------
``KeyHealth.capabilities`` / ``KeyHealth.upstream_mode`` are strings (T07 field
contract, T25 ruling D2); the typed view lives in
``KeyHealth.declared_capabilities()`` / ``KeyHealth.execution_mode()``.  Storing
enums here would put a second, divergent representation in the schema.

Idempotency
-----------
Both statements are bare ``ADD COLUMN``s, which the engine's error-code
whitelist already recognises (SQLite ``duplicate column name`` / MySQL errno
1060), so the same list doubles as the baseline SQL for pre-existing databases.
"""

from __future__ import annotations

from ..migration_engine import Migration

#: SQLite: no length limits, ``TEXT NOT NULL DEFAULT`` is accepted by
#: ``ADD COLUMN`` because the default is a non-null constant.
SQLITE_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN capabilities TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN upstream_mode TEXT NOT NULL DEFAULT 'bonded'",
)

#: MySQL / TiDB: 512 chars comfortably holds all nine capability names with
#: room for future members; ``upstream_mode`` is a short enum-like token.
MYSQL_ALTERS: tuple[str, ...] = (
    "ALTER TABLE models ADD COLUMN capabilities VARCHAR(512) NOT NULL DEFAULT ''",
    "ALTER TABLE models ADD COLUMN upstream_mode VARCHAR(32) NOT NULL DEFAULT 'bonded'",
)


MIGRATION = Migration(
    version=5,
    name="model_capabilities",
    sqlite_sql=SQLITE_ALTERS,
    mysql_sql=MYSQL_ALTERS,
    sqlite_baseline_sql=SQLITE_ALTERS,
    mysql_baseline_sql=MYSQL_ALTERS,
    baseline_probe="models",
)
