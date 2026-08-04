"""Versioned schema migration engine (T03 / R-P0-04 / R-P0-05).

Design goals
------------
* Every migration runs inside its **own transaction** and is recorded in the
  ``schema_migrations`` version table (version / name / sql_digest /
  applied_at / duration_ms / status).
* Only *error-code confirmed* idempotency errors are swallowed:

  =========  =====================================================
  dialect    ignorable
  =========  =====================================================
  sqlite     ``duplicate column name`` / ``index ... already exists``
  mysql      errno 1060 (ER_DUP_FIELDNAME), 1061 (ER_DUP_KEYNAME)
  =========  =====================================================

  Everything else aborts the migration, rolls back, logs the failing
  version + the verbatim SQL + the original exception, and terminates the
  process with a non-zero exit code.
* SQLite databases are backed up (``<db>.bak.<version>.<ts>``) *before* any
  pending migration runs.
* Re-running the engine is a no-op: applied versions are skipped, so the
  version table grows monotonically and each migration executes exactly once.
* The recorded ``sql_digest`` of every already-applied version is compared
  against the current one on each run.  A mismatch means somebody rewrote a
  released migration -- the engine's *same version => same schema* invariant is
  broken and the database may still carry the old schema.  This is reported as
  a ``logger.warning`` plus a ``digest_mismatch`` status, **never** as a
  startup failure; see :meth:`MigrationRunner.warn_digest_drift`.

Naming note
-----------
The architecture document (§2.4) listed both ``store/migrations.py`` (engine)
and ``store/migrations/__init__.py`` (registry).  A module and a package with
the same name cannot coexist in one directory -- the package shadows the
module.  Ruling: the engine lives here, in ``store/migration_engine.py``;
``store/migrations/`` is the migration *script* package.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

#: Process exit code used when a migration fails irrecoverably.
MIGRATION_EXIT_CODE: int = 3

#: SQLite has no distinct numeric error codes for these two conditions; the
#: driver reports ``sqlite3.OperationalError`` with a stable message.  The
#: patterns below are deliberately narrow -- a bare ``already exists`` would
#: also swallow ``table ... already exists``, which we do *not* tolerate
#: (every baseline CREATE TABLE is already guarded by ``IF NOT EXISTS``).
_SQLITE_IGNORABLE: tuple[re.Pattern[str], ...] = (
    re.compile(r"duplicate column name", re.IGNORECASE),
    re.compile(r"index\s+\S+\s+already exists", re.IGNORECASE),
)

#: MySQL / TiDB server error numbers that mean "already done".
ER_DUP_FIELDNAME: int = 1060
ER_DUP_KEYNAME: int = 1061
_MYSQL_IGNORABLE_ERRNOS: frozenset[int] = frozenset({ER_DUP_FIELDNAME, ER_DUP_KEYNAME})

SQLITE_VERSION_TABLE_DDL: str = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, "
    "name TEXT NOT NULL, "
    "sql_digest TEXT NOT NULL, "
    "applied_at INTEGER NOT NULL, "
    "duration_ms INTEGER NOT NULL DEFAULT 0, "
    "status TEXT NOT NULL DEFAULT 'applied')"
)

MYSQL_VERSION_TABLE_DDL: str = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INT PRIMARY KEY, "
    "name VARCHAR(128) NOT NULL, "
    "sql_digest VARCHAR(64) NOT NULL, "
    "applied_at BIGINT NOT NULL, "
    "duration_ms INT NOT NULL DEFAULT 0, "
    "status VARCHAR(16) NOT NULL DEFAULT 'applied'"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

STATUS_APPLIED: str = "applied"
STATUS_BASELINED: str = "baselined"

#: Recorded on a migration whose SQL was changed *after* it was applied.
#: Kept <= 16 chars to fit the MySQL ``status VARCHAR(16)`` column.
STATUS_DIGEST_MISMATCH: str = "digest_mismatch"


class MigrationError(RuntimeError):
    """Raised when a migration statement fails with a non-ignorable error."""

    def __init__(
        self,
        version: int,
        name: str,
        sql: str,
        cause: BaseException,
    ) -> None:
        self.version: int = version
        self.name: str = name
        self.sql: str = sql
        self.cause: BaseException = cause
        super().__init__(
            f"migration v{version:03d} '{name}' failed\n"
            f"  failing SQL: {sql}\n"
            f"  original error: {type(cause).__name__}: {cause}"
        )


# --------------------------------------------------------------------------- #
# Executors
# --------------------------------------------------------------------------- #
class MigrationExecutor(ABC):
    """Dialect-specific low-level driver used by :class:`MigrationRunner`."""

    #: ``"sqlite"`` or ``"mysql"``.
    dialect: str = ""

    @abstractmethod
    async def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute one statement. Raises the raw driver exception on failure."""

    @abstractmethod
    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Run a query and return every row."""

    @abstractmethod
    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        """Run a query and return the first row (or ``None``)."""

    @abstractmethod
    async def begin(self) -> None:
        """Open a transaction."""

    @abstractmethod
    async def commit(self) -> None:
        """Commit the open transaction."""

    @abstractmethod
    async def rollback(self) -> None:
        """Roll back the open transaction."""

    @abstractmethod
    async def table_exists(self, table: str) -> bool:
        """Return ``True`` when *table* is present in the current database."""

    @abstractmethod
    def is_ignorable(self, exc: BaseException) -> bool:
        """Return ``True`` for error-code confirmed idempotency errors."""


class SqliteMigrationExecutor(MigrationExecutor):
    """Executor backed by a single :class:`aiosqlite.Connection`."""

    dialect = "sqlite"

    def __init__(self, conn) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        await self._conn.execute(sql, params)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return [tuple(r) for r in rows]

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        return tuple(row) if row is not None else None

    async def begin(self) -> None:
        # sqlite3 only auto-opens a transaction for DML; DDL would otherwise
        # run in autocommit mode and could not be rolled back.  BEGIN IMMEDIATE
        # also surfaces write-lock contention right away instead of mid-way.
        if getattr(self._conn, "in_transaction", False):
            await self._conn.commit()
        await self._conn.execute("BEGIN IMMEDIATE")

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()

    async def table_exists(self, table: str) -> bool:
        row = await self.fetchone(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        return row is not None

    async def checkpoint(self) -> None:
        """Flush the WAL so a file-level copy is a complete snapshot."""
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:  # pragma: no cover - best effort only
            logger.debug(f"wal_checkpoint before backup skipped: {exc}")

    def is_ignorable(self, exc: BaseException) -> bool:
        message = str(exc)
        return any(p.search(message) for p in _SQLITE_IGNORABLE)


class MySQLMigrationExecutor(MigrationExecutor):
    """Executor backed by one dedicated ``aiomysql`` connection.

    MySQL / TiDB implicitly commit DDL, so a transaction can only protect the
    DML parts of a migration (data hooks, the ``schema_migrations`` insert).
    That limitation is inherent to the server, not to this engine.
    """

    dialect = "mysql"

    def __init__(self, conn) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: tuple = ()) -> None:
        async with self._conn.cursor() as cur:
            await cur.execute(sql.replace("?", "%s"), params)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[tuple]:
        async with self._conn.cursor() as cur:
            await cur.execute(sql.replace("?", "%s"), params)
            rows = await cur.fetchall()
        return [tuple(r) for r in rows]

    async def fetchone(self, sql: str, params: tuple = ()) -> tuple | None:
        async with self._conn.cursor() as cur:
            await cur.execute(sql.replace("?", "%s"), params)
            row = await cur.fetchone()
        return tuple(row) if row is not None else None

    async def begin(self) -> None:
        await self._conn.begin()

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        try:
            await self._conn.rollback()
        except Exception as exc:  # pragma: no cover - connection may be gone
            logger.warning(f"rollback failed: {exc}")

    async def table_exists(self, table: str) -> bool:
        row = await self.fetchone(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=?",
            (table,),
        )
        return row is not None

    def is_ignorable(self, exc: BaseException) -> bool:
        errno = _mysql_errno(exc)
        return errno in _MYSQL_IGNORABLE_ERRNOS


def _mysql_errno(exc: BaseException) -> int:
    """Extract the MySQL server error number from a driver exception."""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    code = getattr(exc, "errno", None)
    return code if isinstance(code, int) else -1


# --------------------------------------------------------------------------- #
# Migration definition
# --------------------------------------------------------------------------- #
MigrationHook = Callable[[MigrationExecutor], Awaitable[None]]


@dataclass(frozen=True)
class Migration:
    """One ordered, versioned schema change.

    Attributes:
        version: Monotonic integer version; unique across the registry.
        name: Short human readable identifier, stored in the version table.
        sqlite_sql: Statements executed on SQLite for a normal (fresh) run.
        mysql_sql: Statements executed on MySQL / TiDB for a normal run.
        sqlite_baseline_sql: Statements still executed when the migration is
            *baselined* on a pre-existing database. Must be provably
            idempotent (``ADD COLUMN`` / ``CREATE INDEX``) -- never DDL that
            re-creates a table.
        mysql_baseline_sql: MySQL counterpart of ``sqlite_baseline_sql``.
        hook: Optional async callable for data migrations that cannot be
            expressed as static SQL.
        run_hook_on_baseline: Whether ``hook`` also runs in baseline mode.
        baseline_probe: Table name whose presence (together with a missing
            ``schema_migrations`` table) marks the database as pre-existing.
    """

    version: int
    name: str
    sqlite_sql: tuple[str, ...] = ()
    mysql_sql: tuple[str, ...] = ()
    sqlite_baseline_sql: tuple[str, ...] = ()
    mysql_baseline_sql: tuple[str, ...] = ()
    hook: MigrationHook | None = None
    run_hook_on_baseline: bool = False
    baseline_probe: str = ""

    def statements(self, dialect: str) -> tuple[str, ...]:
        """Return the statements for a normal run on *dialect*."""
        return self.sqlite_sql if dialect == "sqlite" else self.mysql_sql

    def baseline_statements(self, dialect: str) -> tuple[str, ...]:
        """Return the statements for a baseline run on *dialect*."""
        if dialect == "sqlite":
            return self.sqlite_baseline_sql
        return self.mysql_baseline_sql

    def digest(self, dialect: str) -> str:
        """SHA-256 over the statement list -- detects tampered migrations."""
        payload = "\n".join(self.statements(dialect)).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
@dataclass
class MigrationReport:
    """Outcome of a :meth:`MigrationRunner.run_all` invocation."""

    from_version: int = 0
    to_version: int = 0
    applied: list[int] = field(default_factory=list)
    baselined: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    backup_path: str = ""
    executed_statements: list[tuple[int, str]] = field(default_factory=list)


class MigrationRunner:
    """Applies an ordered sequence of :class:`Migration` objects."""

    def __init__(
        self,
        executor: MigrationExecutor,
        *,
        sqlite_db_path: str | Path | None = None,
    ) -> None:
        """Initialise the runner.

        Args:
            executor: Dialect-specific executor.
            sqlite_db_path: Path of the SQLite database file; enables the
                pre-migration backup. Ignored for MySQL / TiDB.
        """
        self._ex = executor
        self._db_path: Path | None = Path(sqlite_db_path) if sqlite_db_path else None

    # -- version table ----------------------------------------------------- #
    async def version_table_exists(self) -> bool:
        """Return ``True`` when ``schema_migrations`` already exists."""
        return await self._ex.table_exists("schema_migrations")

    async def ensure_version_table(self) -> None:
        """Create ``schema_migrations`` if it is missing."""
        ddl = SQLITE_VERSION_TABLE_DDL if self._ex.dialect == "sqlite" else MYSQL_VERSION_TABLE_DDL
        await self._ex.execute(ddl)
        await self._ex.commit()

    async def applied_versions(self) -> set[int]:
        """Return the set of versions already recorded as applied."""
        if not await self.version_table_exists():
            return set()
        rows = await self._ex.fetchall("SELECT version FROM schema_migrations")
        return {int(r[0]) for r in rows}

    async def recorded_digests(self) -> dict[int, str]:
        """Return ``{version: sql_digest}`` as recorded when each ran."""
        if not await self.version_table_exists():
            return {}
        rows = await self._ex.fetchall("SELECT version, sql_digest FROM schema_migrations")
        return {int(r[0]): str(r[1] or "") for r in rows}

    async def current_version(self) -> int:
        """Return the highest applied version (0 when nothing is applied)."""
        versions = await self.applied_versions()
        return max(versions) if versions else 0

    # -- backup ------------------------------------------------------------ #
    async def backup_sqlite(self, version: int) -> str:
        """Copy the SQLite file to ``<db>.bak.<version>.<timestamp>``.

        Returns the backup path, or an empty string when no backup was needed
        (non-SQLite dialect, unknown path, or an empty/absent database file).

        Raises:
            OSError: Propagated when the copy fails -- a migration must never
                start without its safety net.
        """
        if self._ex.dialect != "sqlite" or self._db_path is None:
            return ""
        if not self._db_path.exists() or self._db_path.stat().st_size == 0:
            return ""
        checkpoint = getattr(self._ex, "checkpoint", None)
        if checkpoint is not None:
            await checkpoint()
        stamp = time.strftime("%Y%m%d%H%M%S")
        target = self._db_path.with_name(f"{self._db_path.name}.bak.{version}.{stamp}")
        shutil.copy2(self._db_path, target)
        logger.info(f"migration backup created: {target}")
        return str(target)

    # -- main entry point -------------------------------------------------- #
    async def run_all(self, migrations: Sequence[Migration]) -> MigrationReport:
        """Apply every pending migration in ascending version order.

        Args:
            migrations: Ordered registry of migrations.

        Returns:
            A :class:`MigrationReport` describing what happened.

        Raises:
            MigrationError: A statement failed with a non-ignorable error.
                The transaction has been rolled back before the raise.
        """
        ordered = sorted(migrations, key=lambda m: m.version)
        self._assert_unique_versions(ordered)

        had_version_table = await self.version_table_exists()
        applied = await self.applied_versions()
        await self.warn_digest_drift(ordered, applied)
        report = MigrationReport(from_version=max(applied) if applied else 0)

        pending = [m for m in ordered if m.version not in applied]
        report.skipped = [m.version for m in ordered if m.version in applied]
        if not pending:
            report.to_version = report.from_version
            logger.debug(f"schema up to date at version {report.from_version} ({self._ex.dialect})")
            return report

        report.backup_path = await self.backup_sqlite(report.from_version)
        await self.ensure_version_table()

        for migration in pending:
            baseline = (
                not had_version_table
                and bool(migration.baseline_probe)
                and await self._ex.table_exists(migration.baseline_probe)
            )
            await self._run_one(migration, baseline=baseline, report=report)
            if baseline:
                report.baselined.append(migration.version)
            else:
                report.applied.append(migration.version)

        report.to_version = await self.current_version()
        logger.info(
            f"schema migrated {report.from_version} -> {report.to_version} "
            f"(applied={report.applied}, baselined={report.baselined})"
        )
        return report

    # -- drift detection --------------------------------------------------- #
    async def warn_digest_drift(self, migrations: Sequence[Migration], applied: set[int]) -> int:
        """Warn when an already-applied migration's SQL has since changed.

        The engine's core invariant is **same version => same schema**.  It is
        broken the moment somebody rewrites a migration that is already
        recorded in ``schema_migrations``: the new statements will never run on
        any database that recorded the old ones, yet the version table keeps
        reporting success.  That is exactly how a database ended up stuck on
        the pre-B2 schema while claiming to be at v006 (see
        :mod:`.migrations.v007_schema_realign`).

        This check makes such a rewrite **visible**; it cannot repair it.  A
        recorded version can never be re-run, so the only fix is a new,
        corrective migration.

        Deliberately a warning, never a hard failure: every database migrated
        by the pre-rewrite v004 carries a stale digest, and a fail-closed check
        would refuse to start all of them -- *including* the ones a corrective
        migration has already repaired, because the recorded digest stays stale
        forever.  Refusing to boot a healthy deployment over a bookkeeping
        mismatch is self-harm.

        Args:
            migrations: The registry being run.
            applied: Versions already recorded (from :meth:`applied_versions`).

        Returns:
            How many drifted versions were found.
        """
        if not applied:
            return 0
        recorded = await self.recorded_digests()
        dialect = self._ex.dialect
        drifted = 0
        for migration in migrations:
            expected = recorded.get(migration.version)
            if not expected:
                continue
            current = migration.digest(dialect)
            if expected == current:
                continue
            drifted += 1
            logger.warning(
                f"migration v{migration.version:03d} '{migration.name}' was "
                f"REWRITTEN after it was applied to this database "
                f"(recorded digest {expected[:12]}, current {current[:12]}). "
                "The recorded version can never be re-run, so this database "
                "may still carry the OLD schema for that version. Fix it with "
                "a new corrective migration, never by editing the old one."
            )
            await self._mark_digest_mismatch(migration.version)
        return drifted

    async def _mark_digest_mismatch(self, version: int) -> None:
        """Flag the drifted row in ``schema_migrations`` (best effort)."""
        try:
            await self._ex.execute(
                "UPDATE schema_migrations SET status = ? WHERE version = ? AND status <> ?",
                (STATUS_DIGEST_MISMATCH, version, STATUS_DIGEST_MISMATCH),
            )
            await self._ex.commit()
        except Exception as exc:  # pragma: no cover - bookkeeping only
            logger.debug(f"could not flag v{version:03d} as digest_mismatch: {exc}")

    # -- internals --------------------------------------------------------- #
    @staticmethod
    def _assert_unique_versions(ordered: Sequence[Migration]) -> None:
        """Guard against a duplicated version number in the registry."""
        seen: set[int] = set()
        for m in ordered:
            if m.version in seen:
                raise ValueError(f"duplicate migration version: {m.version}")
            seen.add(m.version)

    async def _run_one(
        self,
        migration: Migration,
        *,
        baseline: bool,
        report: MigrationReport,
    ) -> None:
        """Execute a single migration inside its own transaction."""
        dialect = self._ex.dialect
        statements = migration.baseline_statements(dialect) if baseline else migration.statements(dialect)
        status = STATUS_BASELINED if baseline else STATUS_APPLIED
        started = time.perf_counter()

        await self._ex.begin()
        current_sql = ""
        try:
            for sql in statements:
                current_sql = sql
                try:
                    await self._ex.execute(sql)
                    report.executed_statements.append((migration.version, sql))
                except Exception as exc:
                    if self._ex.is_ignorable(exc):
                        logger.debug(f"v{migration.version:03d} ignorable ({type(exc).__name__}: {exc}): {sql}")
                        continue
                    raise MigrationError(migration.version, migration.name, sql, exc) from exc

            if migration.hook is not None and (not baseline or migration.run_hook_on_baseline):
                current_sql = f"<python hook {migration.name}>"
                try:
                    await migration.hook(self._ex)
                except MigrationError:
                    raise
                except Exception as exc:
                    raise MigrationError(migration.version, migration.name, current_sql, exc) from exc

            duration_ms = int((time.perf_counter() - started) * 1000)
            current_sql = "INSERT INTO schema_migrations(...)"
            await self._ex.execute(
                "INSERT INTO schema_migrations"
                "(version, name, sql_digest, applied_at, duration_ms, status) "
                "VALUES(?,?,?,?,?,?)",
                (
                    migration.version,
                    migration.name,
                    migration.digest(dialect),
                    int(time.time()),
                    duration_ms,
                    status,
                ),
            )
            await self._ex.commit()
        except BaseException:
            await self._safe_rollback(migration.version)
            raise
        logger.info(
            f"migration v{migration.version:03d} '{migration.name}' {status} "
            f"in {int((time.perf_counter() - started) * 1000)}ms "
            f"({len(statements)} statements, {dialect})"
        )

    async def _safe_rollback(self, version: int) -> None:
        """Roll back, never masking the original failure."""
        try:
            await self._ex.rollback()
        except Exception as exc:  # pragma: no cover - driver dependent
            logger.warning(f"rollback after v{version:03d} failure failed: {exc}")


# --------------------------------------------------------------------------- #
# Convenience wrapper used by the stores
# --------------------------------------------------------------------------- #
async def run_migrations_or_exit(
    executor: MigrationExecutor,
    migrations: Sequence[Migration],
    *,
    sqlite_db_path: str | Path | None = None,
) -> MigrationReport:
    """Run all migrations; terminate the process when any of them fails.

    A failed migration leaves the schema in an unknown state, so the only safe
    reaction is to refuse to start (R-P0-05).  The failing version, the
    verbatim SQL and the original exception are logged before exiting.

    Args:
        executor: Dialect-specific executor.
        migrations: Ordered migration registry.
        sqlite_db_path: SQLite database path (enables pre-migration backup).

    Returns:
        The successful :class:`MigrationReport`.

    Raises:
        SystemExit: With :data:`MIGRATION_EXIT_CODE` when a migration fails or
            the pre-migration backup cannot be written.
    """
    runner = MigrationRunner(executor, sqlite_db_path=sqlite_db_path)
    try:
        return await runner.run_all(migrations)
    except MigrationError as exc:
        logger.error(
            "SCHEMA MIGRATION FAILED - refusing to start\n"
            f"  version    : {exc.version}\n"
            f"  name       : {exc.name}\n"
            f"  failing SQL: {exc.sql}\n"
            f"  cause      : {type(exc.cause).__name__}: {exc.cause}"
        )
        sys.exit(MIGRATION_EXIT_CODE)
    except OSError as exc:
        logger.error(
            "SCHEMA MIGRATION FAILED - could not prepare backup, refusing to start\n"
            f"  cause: {type(exc).__name__}: {exc}"
        )
        sys.exit(MIGRATION_EXIT_CODE)
