"""
Snapshot / golden-test harness for yoyo migrations (v2 — pure replay).

Shared between the test suite (``test_yoyo_migrations.py``) and the
regenerator CLI (``fixtures/migrations/regenerate.py``).  Provides:

- enumeration of the yoyo migrations directory,
- ``apply_pending``: hand the DB to yoyo; yoyo's own tracking rows decide
  which migrations to run,
- a canonical textual dump that round-trips through ``executescript``.

Every yoyo-internal table that exists in the source DB gets its DDL
emitted — yoyo refuses to lazily recreate missing tables once
``_yoyo_version`` is already up to date, and it writes to ``_yoyo_log``
and ``yoyo_lock`` on every ``apply_migrations`` call.  Only
``_yoyo_migration`` and ``_yoyo_version`` rows are persisted (the ones
``to_apply()`` actually consults); ``_yoyo_log`` and ``yoyo_lock`` rows
are stripped because they contain non-deterministic audit data (UUIDs,
hostnames, wall-clock timestamps).  Timestamp columns on the persisted
rows are rewritten to ``CANONICAL_TS`` so snapshots are byte-stable.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from yoyo import get_backend
from yoyo import read_migrations

# Computed locally rather than imported from ``._migration_helpers`` so that
# this module can also be loaded as a plain top-level script import from
# ``fixtures/migrations/regenerate.py`` (which puts this directory on
# ``sys.path`` rather than treating it as a package).
MIGRATIONS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "compute_space", "db", "migrations"))

CANONICAL_TS = "2025-01-01 00:00:00"

# Subset of yoyo's internal tables whose rows snapshots persist.  These
# are the ones to_apply() reads: _yoyo_migration (applied migrations)
# and _yoyo_version (yoyo's own schema revision — absent means yoyo will
# try to "upgrade" and re-run everything).  Rows in the other yoyo
# tables (_yoyo_log, yoyo_lock) are stripped because they're non-
# deterministic; their DDL is still kept so yoyo can write to them.
YOYO_TABLES_WITH_ROWS = ("_yoyo_migration", "_yoyo_version")

# Column names whose values are rewritten to CANONICAL_TS during dump.
# Covers every timestamp column across the yoyo internal schema.
_CANONICAL_TS_COLUMNS = frozenset({"applied_at_utc", "installed_at_utc", "created_at_utc", "ctime"})


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


def resolve_migration_id(token: str) -> str:
    """Resolve a CLI token (full id or numeric prefix) to a full migration id."""
    ids = list_migration_ids()
    if token in ids:
        return token
    matches = [mid for mid in ids if migration_prefix(mid) == token]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"No migration matches {token!r}. Known: {ids}")
    raise ValueError(f"Ambiguous migration token {token!r}: {matches}")


def apply_pending(db_path: str, up_to_inclusive: str | None = None) -> None:
    """Apply whatever yoyo considers pending in ``db_path``.

    The DB must already contain appropriate ``_yoyo_migration`` rows for
    any migrations considered "already applied" — yoyo's ``to_apply()``
    consults those rows to decide.  If ``up_to_inclusive`` is given, only
    migrations at or before that id are considered (others stay pending).
    """
    backend = get_backend(f"sqlite:///{db_path}")
    migrations = read_migrations(MIGRATIONS_DIR)
    if up_to_inclusive is not None:
        ids = [m.id for m in migrations]
        end_idx = ids.index(up_to_inclusive) + 1
        allowed = set(ids[:end_idx])
        migrations = migrations.filter(lambda m: m.id in allowed)
    pending = backend.to_apply(migrations)
    with backend.lock():
        backend.apply_migrations(pending)


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


def _canonicalize_row(col_names: list[str], row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(CANONICAL_TS if col in _CANONICAL_TS_COLUMNS else v for col, v in zip(col_names, row, strict=True))


def _objects_for_tables(conn: sqlite3.Connection, table_names: list[str]) -> list[tuple[str, str, str]]:
    """Schema objects whose tbl_name is in ``table_names`` (tables + their indexes)."""
    if not table_names:
        return []
    placeholders = ",".join("?" for _ in table_names)
    rows = conn.execute(
        f"SELECT type, name, sql FROM sqlite_master "
        f"WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND tbl_name IN ({placeholders}) "
        f"ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name",
        table_names,
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


def _yoyo_tables_present(conn: sqlite3.Connection) -> list[str]:
    """All yoyo-internal tables that exist in the DB, in deterministic order."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' "
        "AND (name LIKE '_yoyo_%' OR name LIKE 'yoyo_%') "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _emit_table_data(conn: sqlite3.Connection, table: str, canonicalize: bool) -> list[str]:
    cursor = conn.execute(f"SELECT * FROM {_quote_ident(table)}")
    col_names = [d[0] for d in cursor.description]
    raw = cursor.fetchall()
    if canonicalize:
        processed = [_canonicalize_row(col_names, r) for r in raw]
    else:
        processed = [tuple(r) for r in raw]
    processed.sort(key=_sort_key)
    cols_csv = ", ".join(col_names)
    out = []
    for row in processed:
        vals_csv = ", ".join(_sql_literal(v) for v in row)
        out.append(f"INSERT INTO {_quote_ident(table)} ({cols_csv}) VALUES ({vals_csv});")
    return out


def dump_application_db(conn: sqlite3.Connection, header_lines: list[str] | None = None) -> str:
    """Return a canonical textual dump of the DB.

    Layout: application schema -> yoyo schema (kept subset) -> application
    data -> yoyo data.  Application objects come first so a human reader
    sees the real schema before the bookkeeping.  Timestamps on yoyo rows
    are replaced with ``CANONICAL_TS`` for determinism.
    """
    lines: list[str] = []
    if header_lines:
        for hl in header_lines:
            lines.append(f"-- {hl}")
        lines.append("")

    lines.append("BEGIN TRANSACTION;")

    app_tables = _application_tables(conn)
    yoyo_tables = _yoyo_tables_present(conn)

    for _t, _n, sql in _objects_for_tables(conn, app_tables):
        lines.append(sql.rstrip().rstrip(";") + ";")
    for _t, _n, sql in _objects_for_tables(conn, yoyo_tables):
        lines.append(sql.rstrip().rstrip(";") + ";")

    for table in app_tables:
        lines.extend(_emit_table_data(conn, table, canonicalize=False))
    for table in yoyo_tables:
        if table in YOYO_TABLES_WITH_ROWS:
            lines.extend(_emit_table_data(conn, table, canonicalize=True))

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


def load_snapshot(scenario_dir: Path, migration_id: str) -> str | None:
    """Read the at_<prefix>.sql fixture for a given migration, or None."""
    path = scenario_dir / snapshot_filename(migration_id)
    if not path.exists():
        return None
    return path.read_text()


def fixture_path(scenario_dir: Path, migration_id: str) -> Path:
    return scenario_dir / snapshot_filename(migration_id)


def present_snapshot_ids(scenario_dir: Path) -> list[str]:
    """Return migration ids for which a snapshot exists in this scenario, ordered."""
    ids = list_migration_ids()
    return [mid for mid in ids if (scenario_dir / snapshot_filename(mid)).exists()]


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------


def materialize_state(db_path: str, at_sql: str) -> None:
    """Populate a fresh sqlite file from an ``at_*.sql`` dump."""
    try:
        os.remove(db_path)
    except FileNotFoundError:
        pass
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(at_sql)
        conn.commit()
    finally:
        conn.close()


def snapshot_header(scenario_name: str, to_migration_id: str) -> list[str]:
    """Canonical header comment block for fixture files."""
    return [
        "Generated by regenerate.py — do not edit by hand.",
        f"Scenario: {scenario_name}",
        f"After migration: {to_migration_id}",
    ]
