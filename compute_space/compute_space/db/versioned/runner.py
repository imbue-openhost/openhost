"""Startup runner for versioned migrations.

Flow:

  1. Acquire an exclusive file lock around the DB so concurrent startups
     serialize — only one process applies migrations, others wait and
     observe the already-current version.
  2. Open the DB in autocommit mode so :meth:`Migration.apply` can
     manage transactions explicitly via ``BEGIN EXCLUSIVE`` / ``COMMIT``.
  3. If the DB has no tables: apply ``schema.sql`` inside a wrapping
     ``BEGIN EXCLUSIVE`` / ``COMMIT`` and stamp the highest registered
     version (no migrations replayed).
  4. If the DB is at v0 (no ``schema_version`` row, or ``version = 0``):
     run the legacy ``migrate()``, idempotently apply ``schema.sql`` to
     cover tables added after the fixture's baseline, THEN stamp v1.
     Stamping last keeps the v0 -> v1 transition effectively atomic —
     both migrate() and schema.sql are idempotent, so any crash before
     the stamp leaves the DB at v0 and retries cleanly.
  5. For each registered migration with version > current: call
     ``migration.apply(db)``. The migration's ``apply`` owns its own
     ``BEGIN EXCLUSIVE`` + ``COMMIT``; the runner just orders the calls.
"""

from __future__ import annotations

import fcntl
import sqlite3
import time
from pathlib import Path

from loguru import logger

from compute_space.db.migrations import _schema_path
from compute_space.db.migrations import migrate as legacy_migrate
from compute_space.db.versioned.base import SCHEMA_VERSION_DDL
from compute_space.db.versioned.base import Migration
from compute_space.db.versioned.registry import REGISTRY


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
    db.execute(SCHEMA_VERSION_DDL)


def _set_version(db: sqlite3.Connection, version: int) -> None:
    _ensure_version_table(db)
    db.execute("INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)", (version,))


def _has_any_tables(db: sqlite3.Connection) -> bool:
    """True if the DB contains any user table (excluding internal sqlite_* tables)."""
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
            current = _legacy_bootstrap(db)

        for migration in registry:
            if migration.version <= current:
                continue
            source = current
            t0 = time.perf_counter()
            try:
                migration.apply(db)
            except Exception:
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


def _legacy_bootstrap(db: sqlite3.Connection) -> int:
    """Bring a v0 DB to v1.

    Order is critical: migrate() -> schema.sql -> stamp v1. Both migrate()
    and schema.sql are idempotent (CREATE TABLE IF NOT EXISTS etc.), so a
    crash at any point before the final stamp leaves version == 0 and the
    whole sequence replays cleanly on the next startup. Stamping last is
    what makes the v0 -> v1 transition effectively atomic.
    """
    t0 = time.perf_counter()
    logger.info("DB is at v0 (legacy); running one-shot migrate() bootstrap to v1")
    legacy_migrate(db)
    with open(_schema_path()) as f:
        db.executescript(f.read())
    _set_version(db, 1)
    current = read_version(db)
    if current != 1:
        raise RuntimeError(f"Legacy bootstrap expected to produce v1, got v{current}")
    logger.info(f"Legacy v0 \u2192 v1 bootstrap complete in {time.perf_counter() - t0:.3f}s")
    return current


def _init_fresh(db: sqlite3.Connection, highest: int) -> None:
    """Initialize a brand-new DB from schema.sql and stamp the version, atomically.

    Wraps schema.sql in ``BEGIN EXCLUSIVE`` / ``COMMIT`` inside a single
    :meth:`sqlite3.Connection.executescript` call so a mid-init crash
    leaves no partial tables.
    """
    with open(_schema_path()) as f:
        schema_sql = f.read()
    wrapped = (
        "BEGIN EXCLUSIVE;\n"
        f"{schema_sql}\n"
        f"INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, {int(highest)});\n"
        "COMMIT;\n"
    )
    try:
        db.executescript(wrapped)
    except Exception:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
