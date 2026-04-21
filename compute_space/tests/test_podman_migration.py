"""Tests for the Docker -> Podman schema migration.

These tests cover two behaviours that aren't checked by the general
test_migrations.py matrix:

1. ``docker_container_id`` is renamed to ``container_id`` with data preserved.
2. ``uid_map_base`` is added and backfilled deterministically for every row,
   including the very first row id.
"""

import sqlite3

from compute_space.core.containers import UID_MAP_BASE_START
from compute_space.core.containers import UID_MAP_RANGE_SIZE
from compute_space.core.containers import UID_MAP_WIDTH
from compute_space.core.containers import compute_uid_map_base
from compute_space.db.migrations import migrate


def _fresh_apps_with_docker_column(db_path: str) -> None:
    """Build a DB that uses ``docker_container_id`` but is otherwise current.

    We insert this schema (rather than the oldest schema) to isolate the
    rename/backfill step from every other migration step, so a regression
    in this specific logic is easier to spot.
    """
    db = sqlite3.connect(db_path)
    try:
        db.executescript("""
            CREATE TABLE apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                manifest_name TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL,
                description TEXT,
                runtime_type TEXT NOT NULL DEFAULT 'serverfull',
                repo_path TEXT NOT NULL,
                repo_url TEXT,
                health_check TEXT,
                local_port INTEGER NOT NULL UNIQUE,
                container_port INTEGER,
                docker_container_id TEXT,
                status TEXT NOT NULL DEFAULT 'stopped'
                    CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error')),
                error_message TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 128,
                cpu_millicores INTEGER NOT NULL DEFAULT 100,
                gpu INTEGER NOT NULL DEFAULT 0,
                public_paths TEXT NOT NULL DEFAULT '[]',
                manifest_raw TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE owner (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT,
                password_needs_set INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        db.commit()
    finally:
        db.close()


def test_docker_container_id_is_renamed_preserving_data(tmp_path) -> None:
    db_path = str(tmp_path / "rename.db")
    _fresh_apps_with_docker_column(db_path)

    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO apps (name, version, repo_path, local_port, docker_container_id) "
        "VALUES ('notes', '1.0', '/repo/notes', 9100, 'container-abc')"
    )
    db.commit()

    migrate(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(apps)").fetchall()}
    assert "container_id" in cols
    assert "docker_container_id" not in cols

    row = db.execute("SELECT container_id FROM apps WHERE name = 'notes'").fetchone()
    db.close()
    assert row is not None
    assert row[0] == "container-abc"


def test_uid_map_base_is_added_and_backfilled(tmp_path) -> None:
    db_path = str(tmp_path / "uidmap.db")
    _fresh_apps_with_docker_column(db_path)

    db = sqlite3.connect(db_path)
    db.executemany(
        "INSERT INTO apps (name, version, repo_path, local_port) VALUES (?, ?, ?, ?)",
        [
            ("app1", "1.0", "/repo/1", 9100),
            ("app2", "1.0", "/repo/2", 9101),
            ("app3", "1.0", "/repo/3", 9102),
        ],
    )
    db.commit()
    rows_before = db.execute("SELECT id, name FROM apps ORDER BY id").fetchall()

    migrate(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(apps)").fetchall()}
    assert "uid_map_base" in cols

    rows_after = db.execute("SELECT id, uid_map_base FROM apps ORDER BY id").fetchall()
    db.close()

    assert [r[0] for r in rows_after] == [r[0] for r in rows_before]
    bases = [uid_base for _, uid_base in rows_after]
    assert all(uid_base >= UID_MAP_BASE_START for uid_base in bases)
    # Every row lands on the canonical per-app window.
    for row_id, uid_base in rows_after:
        assert uid_base == compute_uid_map_base(row_id)
    # Disjointness: no two windows overlap.
    for i in range(len(bases)):
        for j in range(i + 1, len(bases)):
            lo_i, hi_i = bases[i], bases[i] + UID_MAP_WIDTH
            lo_j, hi_j = bases[j], bases[j] + UID_MAP_WIDTH
            assert hi_i <= lo_j or hi_j <= lo_i, f"Windows overlap: rows {i} [{lo_i},{hi_i}) and {j} [{lo_j},{hi_j})"


def test_docker_container_id_rename_runs_before_table_recreation(tmp_path) -> None:
    """Regression test: the column rename must run first.

    Older schemas carry both ``docker_container_id`` AND columns that
    trigger table recreation (``base_path``, ``subdomain``, ``spin_pid``).
    _recreate_table rebuilds the table from schema.sql (which only
    defines ``container_id``) and then copies common columns across.  If
    the rename runs *after* table recreation, the old ``docker_container_id``
    data is silently dropped by the common-column filter.  If it runs
    before, the rename turns the old column into ``container_id``, the
    table recreation's common-column filter picks it up, and the data
    survives.
    """
    db_path = str(tmp_path / "legacy.db")
    db = sqlite3.connect(db_path)
    # A schema that has BOTH docker_container_id AND base_path (which
    # forces _recreate_table to run).  This is the shape of the "oldest"
    # known schema in test_migrations.py.
    db.executescript("""
        CREATE TABLE apps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            base_path TEXT NOT NULL UNIQUE,
            subdomain TEXT NOT NULL UNIQUE,
            version TEXT NOT NULL,
            description TEXT,
            runtime_type TEXT NOT NULL CHECK(runtime_type IN ('serverless', 'serverfull')),
            repo_path TEXT NOT NULL,
            health_check TEXT,
            local_port INTEGER NOT NULL UNIQUE,
            container_port INTEGER,
            docker_container_id TEXT,
            spin_pid INTEGER,
            status TEXT NOT NULL DEFAULT 'stopped',
            error_message TEXT,
            memory_mb INTEGER NOT NULL DEFAULT 128,
            cpu_millicores INTEGER NOT NULL DEFAULT 1000,
            gpu INTEGER NOT NULL DEFAULT 0,
            manifest_raw TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE owner (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    db.execute(
        "INSERT INTO apps (name, base_path, subdomain, version, runtime_type, "
        "repo_path, local_port, docker_container_id) "
        "VALUES ('notes', '/notes', 'notes', '1.0', 'serverfull', '/repo', 9100, "
        "'container-abc')"
    )
    db.commit()

    migrate(db)

    cols = {row[1] for row in db.execute("PRAGMA table_info(apps)").fetchall()}
    assert "container_id" in cols
    assert "docker_container_id" not in cols
    # The original container id must have survived the table recreation
    # and the rename — this is the whole point of the ordering.
    row = db.execute("SELECT container_id FROM apps WHERE name = 'notes'").fetchone()
    db.close()
    assert row is not None
    assert row[0] == "container-abc", (
        "docker_container_id data was lost during migration.  The rename "
        "must run before _recreate_table or the old column's data is "
        "dropped by _recreate_table's common-column filter."
    )


def test_migrate_leaves_pool_overflow_rows_at_zero_sentinel(tmp_path) -> None:
    """Rows whose id would overflow the subuid pool keep uid_map_base=0
    during migration.  The server must still start (so the rest of the
    apps keep running), and the overflowing app surfaces a clear error
    only when it's actually started, not during the migration itself.

    Regression for a bug in an earlier draft where pool exhaustion
    during backfill crashed migrate() and therefore init_db()."""
    db_path = str(tmp_path / "overflow.db")
    _fresh_apps_with_docker_column(db_path)
    overflow_id = UID_MAP_RANGE_SIZE // UID_MAP_WIDTH

    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO apps (id, name, version, repo_path, local_port) VALUES (?, ?, ?, ?, ?)",
        (overflow_id, "too-many", "1.0", "/repo", 9100),
    )
    db.commit()

    # Must not raise.
    migrate(db)

    row = db.execute("SELECT uid_map_base FROM apps WHERE name = 'too-many'").fetchone()
    db.close()
    # Pool-overflow rows stay at the 0 sentinel; start_app_process will
    # surface a ValueError on first start instead.
    assert row is not None
    assert row[0] == 0


def test_migrate_is_idempotent_across_docker_to_podman_rename(tmp_path) -> None:
    """Running migrate twice must not re-rename or re-backfill incorrectly."""
    db_path = str(tmp_path / "idem.db")
    _fresh_apps_with_docker_column(db_path)

    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO apps (name, version, repo_path, local_port, docker_container_id) "
        "VALUES ('notes', '1.0', '/repo/notes', 9100, 'cid-x')"
    )
    db.commit()

    migrate(db)
    # Second call must be a no-op for already-renamed + already-backfilled
    # databases — it's the code path every subsequent router start-up hits.
    migrate(db)

    row = db.execute("SELECT container_id, uid_map_base FROM apps WHERE name = 'notes'").fetchone()
    db.close()
    assert row is not None
    assert row[0] == "cid-x"
    # App was inserted with id=1, so uid_map_base should be the first
    # per-app window above the base.
    assert row[1] == compute_uid_map_base(1)
    assert row[1] == UID_MAP_BASE_START + UID_MAP_WIDTH
