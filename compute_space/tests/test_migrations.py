"""
Tests that the router's hand-rolled SQLite migrations produce a schema
identical to a fresh database created by schema.sql, and that data is
preserved correctly through each migration path.
"""

import os
import sqlite3

from compute_space.db.connection import init_db
from compute_space.db.migrations import migrate
from testing_helpers.schema_helpers import assert_schemas_equal as _assert_schemas_equal
from testing_helpers.schema_helpers import get_schema_snapshot as _get_schema_snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PACKAGE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "compute_space"))
SCHEMA_SQL_PATH = os.path.join(PACKAGE_DIR, "db", "schema.sql")


class _FakeApp:
    """Minimal stand-in for a Quart app so init_db(app) can read app.config."""

    def __init__(self, db_path):
        self.config = {"DB_PATH": db_path}


def _fresh_db(path):
    """Create a DB using only schema.sql (the gold-standard fresh path)."""
    # We can't easily call init_db because it imports from quart at module
    # level.  Instead, replicate the fresh-DB path: _migrate is a no-op on
    # an empty DB, then schema.sql runs.
    db = sqlite3.connect(path)
    with open(SCHEMA_SQL_PATH) as f:
        db.executescript(f.read())
    db.close()
    return path


def _run_init_db(db_path):
    """Run the real init_db against an existing database file.

    init_db imports from quart at module level, so we import db.py directly
    using importlib to control the sys.path.  We save and restore sys.path
    and sys.modules to avoid polluting state for other tests.
    """

    init_db(_FakeApp(db_path))


# ---------------------------------------------------------------------------
# Oldest-known schema: before public_paths, manifest_name were added, and
# with base_path + subdomain columns still present, and owner table lacking
# password_needs_set.
# ---------------------------------------------------------------------------

_OLDEST_ROUTER_SCHEMA = """\
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
    status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error')),
    error_message TEXT,
    memory_mb INTEGER NOT NULL DEFAULT 128,
    cpu_millicores INTEGER NOT NULL DEFAULT 1000,
    gpu INTEGER NOT NULL DEFAULT 0,
    manifest_raw TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE app_databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    db_name TEXT NOT NULL,
    db_path TEXT NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, db_name)
);

CREATE TABLE app_object_stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    bucket_name TEXT NOT NULL,
    bucket_path TEXT NOT NULL,
    FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
    UNIQUE(app_name, bucket_name)
);

CREATE INDEX idx_apps_status ON apps(status);

CREATE TABLE owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_refresh_tokens_token ON refresh_tokens(token);
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouterMigrations:
    """Migration correctness tests for the router database."""

    def test_fresh_db_schema(self, tmp_path):
        """A fresh DB created by schema.sql should have all expected tables."""
        db_path = str(tmp_path / "fresh.db")
        _fresh_db(db_path)
        db = sqlite3.connect(db_path)
        snap = _get_schema_snapshot(db)
        db.close()

        assert "apps" in snap["tables"]
        assert "app_databases" in snap["tables"]
        assert "owner" in snap["tables"]
        assert "refresh_tokens" in snap["tables"]
        # Key columns that migrations add
        assert "public_paths" in snap["tables"]["apps"]
        assert "manifest_name" in snap["tables"]["apps"]
        assert "password_needs_set" in snap["tables"]["owner"]
        # Columns that should NOT exist
        assert "base_path" not in snap["tables"]["apps"]
        assert "subdomain" not in snap["tables"]["apps"]
        assert "spin_pid" not in snap["tables"]["apps"]

    def test_migrated_oldest_db_matches_fresh(self, tmp_path):
        """Running init_db on the oldest known schema should produce the same
        schema as a fresh database."""
        fresh_path = str(tmp_path / "fresh.db")
        migrated_path = str(tmp_path / "migrated.db")

        _fresh_db(fresh_path)

        # Create the old DB
        old_db = sqlite3.connect(migrated_path)
        old_db.executescript(_OLDEST_ROUTER_SCHEMA)
        old_db.close()

        # Run init_db (which calls _migrate then schema.sql)
        _run_init_db(migrated_path)

        fresh_db = sqlite3.connect(fresh_path)
        migrated_db = sqlite3.connect(migrated_path)
        fresh_snap = _get_schema_snapshot(fresh_db)
        migrated_snap = _get_schema_snapshot(migrated_db)
        fresh_db.close()
        migrated_db.close()

        _assert_schemas_equal(fresh_snap, migrated_snap)

    def test_migrate_adds_public_paths(self, tmp_path):
        """Migration adds public_paths column with correct default."""
        db_path = str(tmp_path / "test.db")
        db = sqlite3.connect(db_path)
        # Schema without public_paths
        db.executescript(_OLDEST_ROUTER_SCHEMA)
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        cols = {row[1]: row for row in db.execute("PRAGMA table_info(apps)").fetchall()}
        db.close()
        assert "public_paths" in cols
        # default should be '[]'
        assert cols["public_paths"][4] == "'[]'"  # dflt_value

    def test_migrate_adds_manifest_name_with_backfill(self, tmp_path):
        """Migration adds manifest_name and backfills it from name."""
        db_path = str(tmp_path / "test.db")
        db = sqlite3.connect(db_path)
        db.executescript(_OLDEST_ROUTER_SCHEMA)
        # Insert a row so we can verify the backfill
        db.execute(
            "INSERT INTO apps (name, base_path, subdomain, version, runtime_type, repo_path, local_port) "
            "VALUES ('myapp', '/myapp', 'myapp', '1.0', 'serverfull', '/repo', 9000)"
        )
        db.commit()
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        row = db.execute("SELECT manifest_name FROM apps WHERE name = 'myapp'").fetchone()
        db.close()
        assert row is not None
        assert row[0] == "myapp"

    def test_migrate_drops_base_path_and_subdomain(self, tmp_path):
        """Migration removes base_path and subdomain columns."""
        db_path = str(tmp_path / "test.db")
        db = sqlite3.connect(db_path)
        db.executescript(_OLDEST_ROUTER_SCHEMA)
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        cols = {row[1] for row in db.execute("PRAGMA table_info(apps)").fetchall()}
        db.close()
        assert "base_path" not in cols
        assert "subdomain" not in cols

    def test_data_preserved_through_table_recreation(self, tmp_path):
        """Data in the apps table survives the base_path/subdomain drop migration."""
        db_path = str(tmp_path / "test.db")
        db = sqlite3.connect(db_path)
        db.executescript(_OLDEST_ROUTER_SCHEMA)
        db.execute(
            "INSERT INTO apps (name, base_path, subdomain, version, runtime_type, "
            "repo_path, local_port, description, memory_mb, cpu_millicores, gpu) "
            "VALUES ('testapp', '/testapp', 'testapp', '2.0', 'serverfull', "
            "'/repos/test', 9001, 'A test app', 256, 2000, 1)"
        )
        db.commit()
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM apps WHERE name = 'testapp'").fetchone()
        db.close()

        assert row is not None
        assert row["name"] == "testapp"
        assert row["version"] == "2.0"
        assert row["runtime_type"] == "serverfull"
        assert row["repo_path"] == "/repos/test"
        assert row["local_port"] == 9001
        assert row["description"] == "A test app"
        assert row["memory_mb"] == 256
        assert row["cpu_millicores"] == 2000
        assert row["gpu"] == 1
        # New columns should have defaults or backfilled values
        assert row["manifest_name"] == "testapp"
        assert row["public_paths"] == "[]"

    def test_migrate_adds_password_needs_set(self, tmp_path):
        """Migration adds password_needs_set to owner table."""
        db_path = str(tmp_path / "test.db")
        db = sqlite3.connect(db_path)
        db.executescript(_OLDEST_ROUTER_SCHEMA)
        # Insert an owner so the owner table exists and has a row
        db.execute("INSERT INTO owner (id, username, password_hash) VALUES (1, 'admin', 'hash123')")
        db.commit()
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM owner WHERE id = 1").fetchone()
        cols = {r[1]: r for r in db.execute("PRAGMA table_info(owner)").fetchall()}
        db.close()

        assert "password_needs_set" in cols
        assert row["password_needs_set"] == 0
        # Verify existing owner data survived the table recreation
        assert row["username"] == "admin"
        assert row["password_hash"] == "hash123"
        assert row["created_at"] is not None

    def test_fresh_db_migrate_is_noop(self, tmp_path):
        """migrate on an empty DB should not raise (early-return path)."""
        db_path = str(tmp_path / "empty.db")
        db = sqlite3.connect(db_path)

        try:
            # Should not raise — the early return path handles empty DBs
            migrate(db)

            # DB should still be empty
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            assert len(tables) == 0
        finally:
            db.close()

    def test_idempotent_double_init(self, tmp_path):
        """Running init_db twice on the same DB should not raise or corrupt."""
        db_path = str(tmp_path / "double.db")
        _run_init_db(db_path)
        _run_init_db(db_path)  # second run should be fine

        db = sqlite3.connect(db_path)
        snap = _get_schema_snapshot(db)
        db.close()
        assert "apps" in snap["tables"]

    def test_partial_migration_only_base_path(self, tmp_path):
        """A DB that has public_paths and manifest_name but still has base_path
        should only need the column-drop migration."""
        db_path = str(tmp_path / "partial.db")
        db = sqlite3.connect(db_path)
        # Schema with public_paths and manifest_name already present, but
        # base_path still present (mid-migration state)
        db.executescript("""
            CREATE TABLE apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                manifest_name TEXT NOT NULL DEFAULT '',
                base_path TEXT NOT NULL UNIQUE,
                version TEXT NOT NULL,
                description TEXT,
                runtime_type TEXT NOT NULL CHECK(runtime_type IN ('serverless', 'serverfull')),
                repo_path TEXT NOT NULL,
                health_check TEXT,
                local_port INTEGER NOT NULL UNIQUE,
                container_port INTEGER,
                docker_container_id TEXT,
                spin_pid INTEGER,
                status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error')),
                error_message TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 128,
                cpu_millicores INTEGER NOT NULL DEFAULT 1000,
                gpu INTEGER NOT NULL DEFAULT 0,
                public_paths TEXT NOT NULL DEFAULT '[]',
                manifest_raw TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE app_databases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name TEXT NOT NULL,
                db_name TEXT NOT NULL,
                db_path TEXT NOT NULL,
                FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
                UNIQUE(app_name, db_name)
            );
            CREATE TABLE app_object_stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_name TEXT NOT NULL,
                bucket_name TEXT NOT NULL,
                bucket_path TEXT NOT NULL,
                FOREIGN KEY (app_name) REFERENCES apps(name) ON DELETE CASCADE,
                UNIQUE(app_name, bucket_name)
            );
            CREATE INDEX idx_apps_status ON apps(status);
            CREATE TABLE owner (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT,
                password_needs_set INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE refresh_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_refresh_tokens_token ON refresh_tokens(token);
        """)
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        cols = {row[1] for row in db.execute("PRAGMA table_info(apps)").fetchall()}
        snap = _get_schema_snapshot(db)
        db.close()

        assert "base_path" not in cols
        # Compare against fresh
        fresh_path = str(tmp_path / "fresh.db")
        _fresh_db(fresh_path)
        fresh_db = sqlite3.connect(fresh_path)
        fresh_snap = _get_schema_snapshot(fresh_db)
        fresh_db.close()
        _assert_schemas_equal(fresh_snap, snap)

    def test_migrate_handles_null_datetime_columns(self, tmp_path):
        """Rows with NULL created_at/updated_at survive table recreation.

        _recreate_table must COALESCE NULLs with the column default so that
        NOT NULL constraints on the new table are not violated.  This can
        happen when an older schema version didn't enforce NOT NULL on these
        columns, or data was inserted manually.
        """
        db_path = str(tmp_path / "nulldate.db")
        db = sqlite3.connect(db_path)
        # Use a variant of the oldest schema with nullable datetime columns
        # to simulate the real-world broken state
        schema = _OLDEST_ROUTER_SCHEMA.replace(
            "created_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "created_at TEXT DEFAULT (datetime('now'))",
        ).replace(
            "updated_at TEXT NOT NULL DEFAULT (datetime('now'))",
            "updated_at TEXT DEFAULT (datetime('now'))",
        )
        db.executescript(schema)
        # Insert a row with explicit NULLs for the datetime columns
        db.execute(
            "INSERT INTO apps (name, base_path, subdomain, version, runtime_type, "
            "repo_path, local_port, created_at, updated_at) "
            "VALUES ('nullapp', '/nullapp', 'nullapp', '1.0', 'serverfull', "
            "'/repo', 9999, NULL, NULL)"
        )
        db.commit()
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM apps WHERE name = 'nullapp'").fetchone()
        db.close()
        assert row is not None
        assert row["created_at"] is not None
        assert row["updated_at"] is not None


class TestCrashRecovery:
    """Verify that _recreate_table recovers from a prior crash that left the
    database in an intermediate state (original table dropped, temp table
    still present)."""

    def test_recovers_when_original_missing_and_temp_exists(self, tmp_path):
        """Simulate a crash after DROP TABLE apps but before RENAME apps_new.

        The temp table ``apps_new`` holds all the data.  On next init_db() the
        crash-recovery path should rename it back to ``apps`` before proceeding
        with the normal migration, ultimately producing a correct schema with
        data intact.
        """
        db_path = str(tmp_path / "crash.db")
        db = sqlite3.connect(db_path)
        # Simulate the state after a crash between DROP TABLE apps and
        # ALTER TABLE apps_new RENAME TO apps.  In the real crash apps_new
        # would already have the new schema (created from schema.sql), but
        # we deliberately use old-schema columns (base_path/subdomain) to
        # exercise the harder path where _migrate must fix the table again.
        db.executescript("""
            CREATE TABLE apps_new (
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
                status TEXT NOT NULL DEFAULT 'stopped' CHECK(status IN ('building', 'starting', 'running', 'stopped', 'error')),
                error_message TEXT,
                memory_mb INTEGER NOT NULL DEFAULT 128,
                public_paths TEXT NOT NULL DEFAULT '[]',
                manifest_name TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO apps_new (name, base_path, subdomain, version, runtime_type, repo_path, local_port, manifest_name)
            VALUES ('myapp', '/old', 'sub', '1.0', 'serverfull', '/repo', 3000, 'myapp');
            CREATE TABLE owner (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO owner (id, username, password_hash) VALUES (1, 'admin', 'hash123');
            CREATE TABLE refresh_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX idx_refresh_tokens_token ON refresh_tokens(token);
        """)
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        # apps should exist and contain the recovered row
        rows = db.execute("SELECT name FROM apps").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "myapp"
        # The temp table should be gone
        temp = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='apps_new'").fetchone()
        assert temp is None
        # Schema should match a fresh database
        snap = _get_schema_snapshot(db)
        db.close()

        fresh_path = str(tmp_path / "fresh.db")
        _fresh_db(fresh_path)
        fresh_db = sqlite3.connect(fresh_path)
        fresh_snap = _get_schema_snapshot(fresh_db)
        fresh_db.close()
        _assert_schemas_equal(fresh_snap, snap)
