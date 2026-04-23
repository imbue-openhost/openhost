"""Startup runner for versioned migrations.

Flow:

  1. Acquire an exclusive file lock around the DB so concurrent startups
     serialize — only one process applies migrations, others wait and
     observe the already-current version.
  2. Open the DB in autocommit mode so we can control transactions
     explicitly (``BEGIN EXCLUSIVE`` / ``COMMIT``).
  3. If the DB has no tables: apply ``schema.sql`` and stamp to the
     highest registered version (no migrations replayed).
  4. If the DB is at v0 (no ``schema_version`` row, or ``version = 0``):
     run the legacy ``migrate()`` once — which stamps v1 — then
     idempotently apply ``schema.sql`` to cover any tables added after
     the fixture's baseline.
  5. For each registered migration whose version is strictly greater
     than the current version, apply it inside a single
     ``BEGIN EXCLUSIVE`` transaction that also bumps the version row.
"""

from __future__ import annotations

import fcntl
import sqlite3
import time
from pathlib import Path

from loguru import logger

from compute_space.db.migrations import _schema_path
from compute_space.db.migrations import migrate as legacy_migrate
from compute_space.db.versioned.base import Migration
from compute_space.db.versioned.base import execute_sql_script
from compute_space.db.versioned.registry import REGISTRY

_VERSION_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)"
)


def validate_registry(registry: list[Migration]) -> None:
    """Raise if the registry has a gap, duplicate, or wrong start.

    Versions MUST be strictly increasing and contiguous starting at 2.
    An empty registry is valid (no numbered migrations yet).
    """
    if not registry:
        return
    versions = [m.version for m in registry]
    expected = list(range(2, 2 + len(versions)))
    if versions != expected:
        raise RuntimeError(
            f"Migration registry is not strictly increasing and contiguous starting at 2: "
            f"got {versions}, expected {expected}"
        )


def highest_registered_version(registry: list[Migration]) -> int:
    """Highest version known to this code. v1 if no numbered migrations registered."""
    if not registry:
        return 1
    return registry[-1].version


def read_version(db: sqlite3.Connection) -> int:
    """Read the recorded schema version. Returns 0 if table/row is missing (v0)."""
    has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if not has_table:
        return 0
    row = db.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _ensure_version_table(db: sqlite3.Connection) -> None:
    db.execute(_VERSION_TABLE_SQL)


def _set_version(db: sqlite3.Connection, version: int) -> None:
    _ensure_version_table(db)
    db.execute("INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)", (version,))


def _has_any_tables(db: sqlite3.Connection) -> bool:
    """True if the DB contains any user table (ignoring internal sqlite_* tables).

    Note: the presence of a ``schema_version`` table alone does NOT mean
    the DB has any data schema — but it does mean the runner has touched
    it before. In practice the runner always stamps a version when it
    creates that table, so a DB with only ``schema_version`` and no other
    tables is an inconsistent/partially-initialized state that should not
    occur in normal operation.
    """
    row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone()
    return bool(row)


def apply_migrations(db_path: str, registry: list[Migration] | None = None) -> None:
    """Bring the DB at ``db_path`` up to the highest registered version.

    Safe to call concurrently from multiple processes — serialized by a
    lockfile alongside the DB. No-op if the DB is already up to date.
    """
    if registry is None:
        registry = REGISTRY
    validate_registry(registry)
    highest = highest_registered_version(registry)

    parent = Path(db_path).expanduser().resolve().parent
    parent.mkdir(parents=True, exist_ok=True)
    lock_path = f"{db_path}.migrate.lock"

    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        _apply_under_lock(db_path, registry, highest)


def _apply_under_lock(db_path: str, registry: list[Migration], highest: int) -> None:
    db = sqlite3.connect(db_path, isolation_level=None)
    try:
        current = read_version(db)
        if current > highest:
            raise RuntimeError(
                f"DB schema_version={current} is newer than the highest version this code "
                f"knows about ({highest}). Refusing to start — upgrade the code or downgrade the DB."
            )

        if not _has_any_tables(db):
            _init_fresh(db, highest)
            logger.info(f"Initialized fresh DB from schema.sql and stamped schema_version={highest}")
            return

        if current == 0:
            t0 = time.perf_counter()
            logger.info("DB is at v0 (legacy); running one-shot migrate() bootstrap to v1")
            legacy_migrate(db)
            # migrate()'s early-return path (empty DB) won't stamp; also
            # stamp here so we are definitely at v1 regardless of path.
            _set_version(db, 1)
            # Apply schema.sql idempotently to cover tables added to the
            # canonical schema after the legacy fixture's baseline.
            with open(_schema_path()) as f:
                execute_sql_script(db, f.read())
            current = read_version(db)
            if current != 1:
                raise RuntimeError(f"Legacy bootstrap expected to produce v1, got v{current}")
            logger.info(f"Legacy v0 \u2192 v1 bootstrap complete in {time.perf_counter() - t0:.3f}s")

        for migration in registry:
            if migration.version <= current:
                continue
            source = current
            t0 = time.perf_counter()
            try:
                db.execute("BEGIN EXCLUSIVE")
                migration.up(db)
                _set_version(db, migration.version)
                db.execute("COMMIT")
            except Exception:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                logger.error(f"Migration v{source} \u2192 v{migration.version} ({type(migration).__name__}) failed")
                raise
            duration = time.perf_counter() - t0
            logger.info(
                f"Applied migration v{source} \u2192 v{migration.version} "
                f"({type(migration).__name__}) in {duration:.3f}s"
            )
            current = migration.version
    finally:
        db.close()


def _init_fresh(db: sqlite3.Connection, highest: int) -> None:
    """Initialize a brand-new DB from schema.sql and stamp the version."""
    db.execute("BEGIN EXCLUSIVE")
    try:
        with open(_schema_path()) as f:
            execute_sql_script(db, f.read())
        _set_version(db, highest)
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
