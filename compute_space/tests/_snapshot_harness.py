"""
Snapshot / golden-test harness for yoyo migrations.

Shared between the test suite (``test_yoyo_migrations.py``) and the
regenerator CLI (``fixtures/migrations/regenerate.py``).  Provides:

- enumeration of the yoyo migrations directory,
- one-migration-at-a-time application with a per-checkpoint seed hook, so
  scenario builders can insert data at the point each table exists,
- a canonical textual dump that round-trips through ``executescript``.

The dump format is deliberately simple: DDL text pulled verbatim from
``sqlite_master`` followed by ``INSERT`` statements with rows sorted by
every column.  Yoyo tracking tables (``_yoyo_*``, ``yoyo_lock``) are
excluded so the snapshot captures only the application's state.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from yoyo import get_backend
from yoyo import read_migrations

# Computed locally rather than imported from ``._migration_helpers`` so that
# this module can also be loaded as a plain top-level script import from
# ``fixtures/migrations/regenerate.py`` (which puts this directory on
# ``sys.path`` rather than treating it as a package).
MIGRATIONS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "compute_space", "db", "migrations"))

SeedFn = Callable[[sqlite3.Connection, str], None]


def list_migration_ids() -> list[str]:
    """Return yoyo migration ids in canonical apply order."""
    migrations = read_migrations(MIGRATIONS_DIR)
    return [m.id for m in migrations]


def migration_prefix(migration_id: str) -> str:
    """Return the numeric prefix of a migration id (``0001_initial`` -> ``0001``)."""
    return migration_id.split("_", 1)[0]


def snapshot_filename(migration_id: str) -> str:
    """Filename for the canonical snapshot after this migration is applied."""
    return f"at_{migration_prefix(migration_id)}.sql"


def _apply_single_migration(db_path: str, migration_id: str) -> None:
    """Apply exactly the migration with the given id via yoyo.

    Uses ``backend.to_apply()`` (honours tracking) then filters to just the
    requested id, matching the task's stated API contract.
    """
    backend = get_backend(f"sqlite:///{db_path}")
    migrations = read_migrations(MIGRATIONS_DIR)
    pending = backend.to_apply(migrations).filter(lambda m: m.id == migration_id)
    with backend.lock():
        backend.apply_migrations(pending)


def walk_migrations(
    db_path: str,
    from_exclusive: str | None,
    to_inclusive: str,
    seed_fn: SeedFn | None = None,
) -> None:
    """Apply each migration in (from_exclusive, to_inclusive], seeding between.

    If ``from_exclusive`` is ``None``, start from the very first migration.
    ``seed_fn``, if given, is invoked after each migration with the freshly
    migrated connection and the migration id just applied.  This mirrors
    what ``regenerate.py`` does so that the test walk reaches the same
    state the fixture was generated from.
    """
    ids = list_migration_ids()
    start_idx = 0 if from_exclusive is None else ids.index(from_exclusive) + 1
    end_idx = ids.index(to_inclusive) + 1
    for mid in ids[start_idx:end_idx]:
        _apply_single_migration(db_path, mid)
        if seed_fn is not None:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                seed_fn(conn, mid)
                conn.commit()
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Canonical dump
# ---------------------------------------------------------------------------


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _sort_key(row: tuple[Any, ...]) -> tuple[tuple[int, Any], ...]:
    """Sort rows so NULLs come before non-NULLs; other types compare naturally."""
    return tuple((0, "") if v is None else (1, v) for v in row)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _application_objects(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """All application schema objects (type, name, sql) in a deterministic order."""
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '_yoyo_%' "
        "AND name NOT LIKE 'yoyo_%' "
        "AND (tbl_name IS NULL OR tbl_name NOT LIKE '_yoyo_%') "
        "AND (tbl_name IS NULL OR tbl_name NOT LIKE 'yoyo_%') "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _application_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '_yoyo_%' "
        "AND name NOT LIKE 'yoyo_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def dump_application_db(conn: sqlite3.Connection, header_lines: list[str] | None = None) -> str:
    """Return a canonical textual dump of application schema + data.

    The output is loadable via :meth:`sqlite3.Connection.executescript`, so
    it doubles as a materialization step for the test walk.  ``header_lines``
    are emitted as SQL comments at the top; use this to stamp "generated
    by" or scenario info.
    """
    lines: list[str] = []
    if header_lines:
        for hl in header_lines:
            lines.append(f"-- {hl}")
        lines.append("")

    lines.append("BEGIN TRANSACTION;")

    for _type, _name, sql in _application_objects(conn):
        stripped = sql.rstrip().rstrip(";")
        lines.append(stripped + ";")

    for table in _application_tables(conn):
        cursor = conn.execute(f"SELECT * FROM {_quote_ident(table)}")
        col_names = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
        rows.sort(key=_sort_key)
        cols_csv = ", ".join(col_names)
        for row in rows:
            vals_csv = ", ".join(_sql_literal(v) for v in row)
            lines.append(f"INSERT INTO {_quote_ident(table)} ({cols_csv}) VALUES ({vals_csv});")

    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Scenario discovery
# ---------------------------------------------------------------------------


def scenarios_root() -> Path:
    """Directory containing per-scenario subdirectories."""
    return Path(__file__).parent / "fixtures" / "migrations"


def discover_scenario_dirs(root: Path | None = None) -> list[Path]:
    """Return all scenario_* subdirectories of the fixtures root, sorted."""
    base = root if root is not None else scenarios_root()
    return sorted(p for p in base.iterdir() if p.is_dir() and p.name.startswith("scenario_"))


def load_scenario_builder(scenario_dir: Path) -> ModuleType:
    """Dynamically import ``builder.py`` from a scenario directory."""
    builder_path = scenario_dir / "builder.py"
    spec = importlib.util.spec_from_file_location(f"_scenario_{scenario_dir.name}", builder_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load builder for {scenario_dir}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scenario_seed_fn(builder: ModuleType) -> SeedFn:
    """Return the builder's ``seed_at`` function, or a no-op if absent."""
    fn = getattr(builder, "seed_at", None)
    if fn is None:
        return lambda _conn, _mid: None
    return fn  # type: ignore[no-any-return]


def load_snapshot(scenario_dir: Path, migration_id: str) -> str | None:
    """Read the at_<prefix>.sql fixture for a given migration, or None."""
    path = scenario_dir / snapshot_filename(migration_id)
    if not path.exists():
        return None
    return path.read_text()


def fixture_path(scenario_dir: Path, migration_id: str) -> Path:
    return scenario_dir / snapshot_filename(migration_id)


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------


def materialize_state(db_path: str, at_sql: str | None) -> None:
    """Populate a fresh sqlite file with either an empty DB or an ``at_*.sql`` dump."""
    # Start from nothing — remove any pre-existing file to avoid leftover state.
    try:
        os.remove(db_path)
    except FileNotFoundError:
        pass
    conn = sqlite3.connect(db_path)
    try:
        if at_sql is not None:
            conn.executescript(at_sql)
            conn.commit()
    finally:
        conn.close()
