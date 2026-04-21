"""Tests for the Docker -> Podman schema migration.

The only schema change the migration applies on existing databases is
renaming ``docker_container_id`` to ``container_id``.  Verify that data
survives the rename and that the ordering is correct when the same
migration run also has to recreate the table for unrelated reasons
(base_path/subdomain drop on old databases).
"""

import sqlite3

from compute_space.db.migrations import migrate


def _fresh_apps_with_docker_column(db_path: str) -> None:
    """Build a DB that uses ``docker_container_id`` but is otherwise current.

    We insert this schema (rather than the oldest schema) to isolate the
    rename step from every other migration step, so a regression in this
    specific logic is easier to spot.
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
    try:
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
        assert row is not None
        assert row[0] == "container-abc"
    finally:
        db.close()


def test_docker_container_id_rename_runs_before_table_recreation(tmp_path) -> None:
    """Regression: the column rename must run before _recreate_table.

    Older schemas carry both ``docker_container_id`` AND columns that
    trigger table recreation (``base_path``, ``subdomain``, ``spin_pid``).
    _recreate_table rebuilds the table from schema.sql (which only
    defines ``container_id``) and then copies common columns across.  If
    the rename runs *after* table recreation, the old ``docker_container_id``
    data is silently dropped by the common-column filter.
    """
    db_path = str(tmp_path / "legacy.db")
    db = sqlite3.connect(db_path)
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
    try:
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
        row = db.execute("SELECT container_id FROM apps WHERE name = 'notes'").fetchone()
        assert row is not None
        assert row[0] == "container-abc", (
            "docker_container_id data was lost during migration.  The rename "
            "must run before _recreate_table or the old column's data is "
            "dropped by _recreate_table's common-column filter."
        )
    finally:
        db.close()


def test_migrate_is_idempotent_across_docker_to_podman_rename(tmp_path) -> None:
    """Running migrate twice must not re-rename or mangle the column."""
    db_path = str(tmp_path / "idem.db")
    _fresh_apps_with_docker_column(db_path)

    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, docker_container_id) "
            "VALUES ('notes', '1.0', '/repo/notes', 9100, 'cid-x')"
        )
        db.commit()

        migrate(db)
        migrate(db)

        row = db.execute("SELECT container_id FROM apps WHERE name = 'notes'").fetchone()
    finally:
        db.close()
    assert row is not None
    assert row[0] == "cid-x"
