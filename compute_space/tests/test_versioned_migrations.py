"""Tests for the versioned migration framework.

Covers:
  - schema_version metadata table (REQ-VER-1..5)
  - legacy v0 -> v1 bootstrap via the existing migrate() (REQ-LEG-*)
  - Migration base class + SqlFileMigration (REQ-MF-*)
  - Registry validation: contiguous, starts at 2 (REQ-REG-*)
  - Runner behavior: lock, apply, atomic version bump, rollback (REQ-RUN-*)
  - Fresh-DB init via schema.sql, stamps to highest (REQ-INIT-*)
  - Snapshot tests per registered migration + sanity vs schema.sql
    (REQ-TEST-1, REQ-TEST-2, REQ-TEST-3, REQ-TEST-4)
  - Legacy bootstrap fixture test (REQ-TEST-5)
  - Concurrency: two concurrent startups (REQ-TEST-6)
  - PRAGMA foreign_keys heuristic warning (REQ-TEST-7)
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import warnings as _warnings
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from compute_space.db.migrations import _schema_path
from compute_space.db.versioned import REGISTRY
from compute_space.db.versioned import Migration
from compute_space.db.versioned import SqlFileMigration
from compute_space.db.versioned import apply_migrations
from compute_space.db.versioned import execute_sql_script
from compute_space.db.versioned import highest_registered_version
from compute_space.db.versioned import read_version
from compute_space.db.versioned import runner as runner_mod
from compute_space.db.versioned import validate_registry
from testing_helpers.schema_helpers import assert_schemas_equal
from testing_helpers.schema_helpers import get_schema_snapshot

# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


_OLDEST_LEGACY_SCHEMA = """\
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


def _seed_dataset(db: sqlite3.Connection) -> None:
    """Insert representative rows into every table present at v1.

    Shared across all snapshot tests (REQ-TEST-3) so each snapshot covers
    both structure and data transforms through the migration chain.
    """
    db.execute(
        "INSERT INTO apps (name, manifest_name, version, runtime_type, repo_path, "
        "local_port, description, memory_mb, cpu_millicores, gpu, public_paths, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "seedapp",
            "seedapp",
            "1.2.3",
            "serverfull",
            "/repo/seedapp",
            19001,
            "Seed app",
            256,
            500,
            0,
            '["/public"]',
            "2024-01-01T00:00:00",
            "2024-01-01T00:00:00",
        ),
    )
    db.execute(
        "INSERT INTO app_databases (app_name, db_name, db_path) VALUES (?,?,?)",
        ("seedapp", "seed_db", "/data/seed.db"),
    )
    db.execute(
        "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES (?,?,?,?)",
        ("seedapp", "http", 8080, 19501),
    )
    db.execute(
        "INSERT INTO owner (id, username, password_hash, password_needs_set, created_at) VALUES (?,?,?,?,?)",
        (1, "alice", "argon2-stub", 0, "2024-01-01T00:00:00"),
    )
    db.execute(
        "INSERT INTO refresh_tokens (token_hash, expires_at, revoked) VALUES (?,?,?)",
        ("rt-hash-1", "2099-01-01T00:00:00", 0),
    )
    db.execute(
        "INSERT INTO api_tokens (name, token_hash, expires_at, created_at) VALUES (?,?,?,?)",
        ("ci-key", "api-hash-1", "2099-01-01T00:00:00", "2024-01-01T00:00:00"),
    )
    db.execute(
        "INSERT INTO app_tokens (app_name, token_hash) VALUES (?,?)",
        ("seedapp", "app-hash-1"),
    )
    db.execute(
        "INSERT INTO service_providers (service_name, app_name) VALUES (?,?)",
        ("mailer", "seedapp"),
    )
    db.execute(
        "INSERT INTO permissions (consumer_app, permission_key) VALUES (?,?)",
        ("seedapp", "net.egress"),
    )


def _dump_data(db: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Ordered dump of every user table's rows (excluding schema_version).

    schema_version is excluded so the dump is stable across version bumps;
    the version is checked separately.
    """
    data: dict[str, list[dict[str, Any]]] = {}
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name != 'schema_version' "
            "ORDER BY name"
        ).fetchall()
    ]
    for tbl in tables:
        col_names = [row[1] for row in db.execute(f"PRAGMA table_info({tbl})").fetchall()]
        order_by = ", ".join(f'"{c}"' for c in col_names)
        rows = db.execute(f'SELECT {order_by} FROM "{tbl}" ORDER BY {order_by}').fetchall()
        data[tbl] = [dict(zip(col_names, r, strict=False)) for r in rows]
    return data


def _snapshot(db: sqlite3.Connection) -> dict[str, Any]:
    return {
        "schema": get_schema_snapshot(db),
        "data": _dump_data(db),
    }


def _apply_and_snapshot(db_path: str, registry: list[Migration], seed: bool) -> dict[str, Any]:
    """Build a snapshot by bootstrapping to v1, seeding, and applying numbered migrations.

    Matches the flow described in REQ-TEST-3: shared seed is inserted at
    the earliest version the snapshots cover (v1), then every numbered
    migration in ``registry`` runs against the seeded data.
    """
    # Start the DB at v0 by writing the oldest legacy schema directly.
    init = sqlite3.connect(db_path)
    init.executescript(_OLDEST_LEGACY_SCHEMA)
    init.close()

    # Bootstrap v0 -> v1 via the legacy path (empty registry here).
    apply_migrations(db_path, registry=[])

    # Seed at v1 so numbered migrations see representative rows.
    if seed:
        db = sqlite3.connect(db_path, isolation_level=None)
        try:
            _seed_dataset(db)
        finally:
            db.close()

    # Apply the caller's numbered migrations.
    apply_migrations(db_path, registry=registry)

    db = sqlite3.connect(db_path)
    try:
        snap = _snapshot(db)
    finally:
        db.close()
    return snap


SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"


def _snapshot_path(version: int) -> Path:
    return SNAPSHOTS_DIR / f"v{version:04d}.json"


def _normalise_snapshot_for_compare(snap: dict[str, Any]) -> str:
    return json.dumps(snap, sort_keys=True, indent=2, default=str)


# --------------------------------------------------------------------------- #
# Registry validation (REQ-REG-2)
# --------------------------------------------------------------------------- #


class _FakeMigration(Migration):
    def __init__(self, version: int):
        self.version = version  # type: ignore[misc]

    def up(self, db: sqlite3.Connection) -> None:  # pragma: no cover - unused
        pass


class TestRegistryValidation:
    def test_empty_registry_is_valid(self):
        validate_registry([])

    def test_single_v2_is_valid(self):
        validate_registry([_FakeMigration(2)])

    def test_gap_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(2), _FakeMigration(4)])

    def test_duplicate_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(2), _FakeMigration(2)])

    def test_wrong_start_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(3)])

    def test_out_of_order_is_rejected(self):
        with pytest.raises(RuntimeError, match="not strictly increasing"):
            validate_registry([_FakeMigration(3), _FakeMigration(2)])


# --------------------------------------------------------------------------- #
# Fresh-DB init (REQ-INIT-1, REQ-INIT-2)
# --------------------------------------------------------------------------- #


class TestFreshInit:
    def test_empty_file_goes_through_fresh_path(self, tmp_path):
        db_path = str(tmp_path / "fresh.db")
        apply_migrations(db_path)

        db = sqlite3.connect(db_path)
        try:
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            assert "apps" in tables
            assert "schema_version" in tables
            assert read_version(db) == highest_registered_version(REGISTRY)
        finally:
            db.close()

    def test_fresh_init_does_not_replay_migrations(self, tmp_path):
        """After a fresh init, a second run must be a pure no-op."""
        db_path = str(tmp_path / "fresh.db")

        class SpyMigration(Migration):
            version = 2
            calls: list[int] = []

            def up(self, db):
                SpyMigration.calls.append(1)

        # Fresh init with the spy registered.
        apply_migrations(db_path, registry=[SpyMigration()])
        # Fresh path stamps to highest (= 2), so migration should NOT have run.
        assert SpyMigration.calls == []

        # Second run: version already 2, nothing to do.
        apply_migrations(db_path, registry=[SpyMigration()])
        assert SpyMigration.calls == []

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Version gating (REQ-VER-4)
# --------------------------------------------------------------------------- #


class TestVersionGating:
    def test_db_ahead_of_code_aborts(self, tmp_path):
        db_path = str(tmp_path / "future.db")
        apply_migrations(db_path)  # fresh init

        # Hand-stamp a version higher than any registered.
        db = sqlite3.connect(db_path)
        try:
            db.execute("UPDATE schema_version SET version = 99 WHERE id = 1")
            db.commit()
        finally:
            db.close()

        with pytest.raises(RuntimeError, match="newer than the highest version"):
            apply_migrations(db_path)


# --------------------------------------------------------------------------- #
# Legacy bootstrap (REQ-LEG-1..3, REQ-TEST-5)
# --------------------------------------------------------------------------- #


class TestLegacyBootstrap:
    def _make_v0_fixture(self, path: str) -> None:
        db = sqlite3.connect(path)
        db.executescript(_OLDEST_LEGACY_SCHEMA)
        db.execute(
            "INSERT INTO apps (name, base_path, subdomain, version, runtime_type, "
            "repo_path, local_port) VALUES (?,?,?,?,?,?,?)",
            ("legacy_app", "/legacy_app", "legacy_app", "1.0", "serverfull", "/r", 20001),
        )
        db.execute("INSERT INTO owner (id, username, password_hash) VALUES (1, 'bob', 'bcrypt-stub')")
        db.execute("INSERT INTO refresh_tokens (token, expires_at) VALUES ('plaintext', '2099-01-01T00:00:00')")
        db.commit()
        db.close()

    def test_v0_upgrades_to_v1_and_stamps(self, tmp_path):
        db_path = str(tmp_path / "legacy.db")
        self._make_v0_fixture(db_path)

        apply_migrations(db_path)

        db = sqlite3.connect(db_path)
        try:
            # Ends at v1 (since no v2+ registered in the real REGISTRY).
            assert read_version(db) == highest_registered_version(REGISTRY)
            # Data preserved through legacy path.
            row = db.execute("SELECT name FROM apps WHERE name = 'legacy_app'").fetchone()
            assert row is not None
            # Refresh tokens were hashed.
            tok = db.execute("SELECT token_hash FROM refresh_tokens").fetchone()
            assert tok is not None and tok[0] != "plaintext"
        finally:
            db.close()

    def test_second_startup_does_not_call_legacy_again(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "legacy.db")
        self._make_v0_fixture(db_path)

        # First startup: v0 → v1.
        apply_migrations(db_path)

        # Monkey-patch legacy_migrate to explode if called again.
        def _boom(_db):
            raise AssertionError("legacy migrate() should not run on an already-v1 DB")

        monkeypatch.setattr(runner_mod, "legacy_migrate", _boom)
        apply_migrations(db_path)  # must not raise

    def test_legacy_matches_fresh_schema(self, tmp_path):
        """REQ-TEST-5 / REQ-TEST-4: legacy-bootstrapped DB has the same
        schema as a fresh DB initialized from schema.sql alone."""
        legacy_path = str(tmp_path / "legacy.db")
        fresh_path = str(tmp_path / "fresh.db")

        self._make_v0_fixture(legacy_path)
        apply_migrations(legacy_path)
        apply_migrations(fresh_path)

        legacy_db = sqlite3.connect(legacy_path)
        fresh_db = sqlite3.connect(fresh_path)
        try:
            assert_schemas_equal(get_schema_snapshot(fresh_db), get_schema_snapshot(legacy_db))
        finally:
            legacy_db.close()
            fresh_db.close()

    def test_crash_mid_bootstrap_leaves_db_unstamped(self, tmp_path, monkeypatch):
        """Crashing between migrate() and the v1 stamp must leave version == 0.

        Both migrate() and schema.sql are idempotent, so the whole bootstrap
        replays cleanly on the next startup. The stamp happens AFTER
        schema.sql so a failure during either step keeps the DB at v0.
        """
        db_path = str(tmp_path / "crash.db")
        self._make_v0_fixture(db_path)

        original_execute = runner_mod.execute_sql_script
        call_count = {"n": 0}

        def flaky_execute(db, sql):
            call_count["n"] += 1
            # Only the first call (legacy bootstrap's schema.sql) should fail.
            if call_count["n"] == 1:
                raise RuntimeError("simulated schema.sql crash")
            return original_execute(db, sql)

        monkeypatch.setattr(runner_mod, "execute_sql_script", flaky_execute)

        with pytest.raises(RuntimeError, match="simulated schema.sql crash"):
            apply_migrations(db_path)

        # migrate() may have created the schema_version table, but the row
        # must not have been stamped — version must read back as 0.
        probe = sqlite3.connect(db_path)
        try:
            assert read_version(probe) == 0
        finally:
            probe.close()

        # Retry under normal conditions: bootstrap replays cleanly.
        monkeypatch.setattr(runner_mod, "execute_sql_script", original_execute)
        apply_migrations(db_path)

        final = sqlite3.connect(db_path)
        try:
            assert read_version(final) == highest_registered_version(REGISTRY)
            # Data from the v0 fixture survived the retry.
            row = final.execute("SELECT name FROM apps WHERE name = 'legacy_app'").fetchone()
            assert row is not None
        finally:
            final.close()


# --------------------------------------------------------------------------- #
# Numbered migration plumbing (REQ-MF-*, REQ-RUN-*)
# --------------------------------------------------------------------------- #


class TestNumberedMigrationRunner:
    def _v1_db(self, path: str) -> None:
        apply_migrations(path)  # fresh init stamps v1 (empty registry case)

    def test_migration_applied_atomically(self, tmp_path):
        db_path = str(tmp_path / "mig.db")
        self._v1_db(db_path)

        class AddTableMig(Migration):
            version = 2

            def up(self, db):
                db.execute("CREATE TABLE added (x INTEGER NOT NULL)")
                db.execute("INSERT INTO added (x) VALUES (42)")

        apply_migrations(db_path, registry=[AddTableMig()])

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
            row = db.execute("SELECT x FROM added").fetchone()
            assert row[0] == 42
        finally:
            db.close()

    def test_failing_migration_rolls_back(self, tmp_path):
        """REQ-MF-4: failing migration leaves DB + version unchanged."""
        db_path = str(tmp_path / "rollback.db")
        self._v1_db(db_path)

        class BadMig(Migration):
            version = 2

            def up(self, db):
                db.execute("CREATE TABLE will_be_gone (x INTEGER)")
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            apply_migrations(db_path, registry=[BadMig()])

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 1
            row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='will_be_gone'").fetchone()
            assert row is None, "Failed migration's DDL must be rolled back"
        finally:
            db.close()

        # Re-running cleanly retries from last successful version (REQ-RUN-5).
        class GoodMig(Migration):
            version = 2

            def up(self, db):
                db.execute("CREATE TABLE ok (x INTEGER)")

        apply_migrations(db_path, registry=[GoodMig()])
        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
        finally:
            db.close()

    def test_migrations_apply_in_order(self, tmp_path):
        db_path = str(tmp_path / "ordered.db")
        self._v1_db(db_path)

        order: list[int] = []

        def make(ver):
            class M(Migration):
                version = ver

                def up(self, db):
                    order.append(ver)
                    db.execute(f"CREATE TABLE step_{ver} (x INTEGER)")

            return M()

        apply_migrations(db_path, registry=[make(2), make(3), make(4)])
        assert order == [2, 3, 4]

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 4
        finally:
            db.close()

    def test_info_log_per_applied_migration(self, tmp_path):
        """REQ-RUN-4: one INFO line per applied migration with version + duration."""
        db_path = str(tmp_path / "log.db")
        self._v1_db(db_path)

        captured: list[str] = []
        sink_id = logger.add(lambda msg: captured.append(str(msg)), level="INFO")
        try:

            class MigA(Migration):
                version = 2

                def up(self, db):
                    db.execute("CREATE TABLE t_a (x INTEGER)")

            class MigB(Migration):
                version = 3

                def up(self, db):
                    db.execute("CREATE TABLE t_b (x INTEGER)")

            apply_migrations(db_path, registry=[MigA(), MigB()])
        finally:
            logger.remove(sink_id)

        blob = "\n".join(captured)
        assert "v1 \u2192 v2" in blob
        assert "v2 \u2192 v3" in blob
        # Each applied-migration line includes a duration in seconds.
        assert re.search(r"in \d+\.\d+s", blob)


# --------------------------------------------------------------------------- #
# SqlFileMigration (REQ-MF-3)
# --------------------------------------------------------------------------- #


class TestSqlFileMigration:
    def test_runs_sibling_sql_file(self, tmp_path):
        # Build a throwaway package so inspect.getfile() can resolve the
        # subclass's source path.
        pkg_dir = tmp_path / "migpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "m0002.sql").write_text(
            "CREATE TABLE from_sql_file (id INTEGER PRIMARY KEY);\nINSERT INTO from_sql_file (id) VALUES (7);\n"
        )
        (pkg_dir / "m0002.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "class M0002(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'm0002.sql'\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = importlib.import_module("migpkg.m0002")
        finally:
            sys.path.pop(0)
        M0002 = mod.M0002

        db_path = str(tmp_path / "sqlfile.db")
        apply_migrations(db_path)  # fresh init
        apply_migrations(db_path, registry=[M0002()])

        db = sqlite3.connect(db_path)
        try:
            row = db.execute("SELECT id FROM from_sql_file").fetchone()
            assert row[0] == 7
            assert read_version(db) == 2
        finally:
            db.close()

    def test_multi_statement_sql_uses_caller_transaction(self, tmp_path):
        """A SqlFileMigration that issues many statements must NOT auto-commit
        mid-way — a later exception must still roll everything back.
        """
        pkg_dir = tmp_path / "multipkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "m.sql").write_text(
            "-- Multiple statements to exercise the splitter.\n"
            "CREATE TABLE multi_a (id INTEGER);\n"
            "CREATE TABLE multi_b (id INTEGER);\n"
            "INSERT INTO multi_a (id) VALUES (1);\n"
            "INSERT INTO multi_b (id) VALUES (2);\n"
        )
        (pkg_dir / "m.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "import sqlite3\n"
            "class MSql(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'm.sql'\n"
            "class MBoom(MSql):\n"
            "    def up(self, db):\n"
            "        super().up(db)\n"
            "        raise RuntimeError('nope')\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = importlib.import_module("multipkg.m")
        finally:
            sys.path.pop(0)
        MBoom = mod.MBoom

        db_path = str(tmp_path / "txsafe.db")
        apply_migrations(db_path)
        with pytest.raises(RuntimeError, match="nope"):
            apply_migrations(db_path, registry=[MBoom()])

        db = sqlite3.connect(db_path)
        try:
            for tbl in ("multi_a", "multi_b"):
                row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone()
                assert row is None, f"{tbl} must have been rolled back"
            assert read_version(db) == 1
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# execute_sql_script helper
# --------------------------------------------------------------------------- #


class TestExecuteSqlScript:
    def test_schema_sql_runs_cleanly(self, tmp_path):
        db_path = str(tmp_path / "schema.db")
        db = sqlite3.connect(db_path, isolation_level=None)
        try:
            with open(_schema_path()) as f:
                execute_sql_script(db, f.read())
            # Smoke: the canonical tables exist.
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            for expected in ("apps", "owner", "refresh_tokens", "app_tokens", "schema_version"):
                assert expected in tables
        finally:
            db.close()

    def test_empty_and_comment_only_inputs(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        db = sqlite3.connect(db_path, isolation_level=None)
        try:
            execute_sql_script(db, "")
            execute_sql_script(db, "-- just a comment\n\n-- another\n")
            # Should not have created any tables or errored.
            row = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            assert row is None
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Concurrency (REQ-TEST-6, REQ-RUN-1)
# --------------------------------------------------------------------------- #


class TestConcurrency:
    def test_two_concurrent_processes_serialize(self, tmp_path):
        """Two OS processes call apply_migrations against the same DB at once.

        fcntl.flock locks are per open-file-description, so this exercises
        real cross-process serialization — stronger than a thread-level
        test. Only one process applies the migration; the other waits
        for the lock, observes the already-bumped version, and returns
        a no-op.
        """
        db_path = str(tmp_path / "race.db")
        # Bring DB to v1 before the race.
        apply_migrations(db_path)

        counter_path = tmp_path / "counter.txt"
        counter_path.write_text("")

        worker_script = tmp_path / "worker.py"
        worker_script.write_text(
            "import sys, time\n"
            "from compute_space.db.versioned import Migration, apply_migrations\n"
            "\n"
            "DB_PATH = sys.argv[1]\n"
            "COUNTER = sys.argv[2]\n"
            "\n"
            "class CounterMig(Migration):\n"
            "    version = 2\n"
            "    def up(self, db):\n"
            "        with open(COUNTER, 'a') as f:\n"
            "            f.write('x')\n"
            "        # Hold the exclusive lock long enough for the other\n"
            "        # process to contend and queue behind us.\n"
            "        time.sleep(0.5)\n"
            "        db.execute('CREATE TABLE race_done (x INTEGER)')\n"
            "\n"
            "apply_migrations(DB_PATH, registry=[CounterMig()])\n"
        )

        def spawn():
            return subprocess.Popen(
                [sys.executable, str(worker_script), db_path, str(counter_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        p1 = spawn()
        # Small nudge so p1 almost certainly wins the flock race. The
        # assertion below still holds either way — only one applies.
        time.sleep(0.1)
        p2 = spawn()

        out1, err1 = p1.communicate(timeout=30)
        out2, err2 = p2.communicate(timeout=30)

        assert p1.returncode == 0, f"worker 1 crashed: {err1.decode()}"
        assert p2.returncode == 0, f"worker 2 crashed: {err2.decode()}"

        # Exactly one process ran CounterMig.up() — the other saw
        # version==2 already and no-op'd.
        assert counter_path.read_text() == "x", (
            f"Exactly one worker must apply the migration; got counter={counter_path.read_text()!r}"
        )

        db = sqlite3.connect(db_path)
        try:
            assert read_version(db) == 2
            # The applied migration's table must exist.
            row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='race_done'").fetchone()
            assert row is not None
        finally:
            db.close()


# --------------------------------------------------------------------------- #
# Snapshot tests (REQ-TEST-1..4)
# --------------------------------------------------------------------------- #


def _build_snapshot_for_version(tmp_path: Path, target_version: int) -> dict[str, Any]:
    """Apply REGISTRY truncated at target_version, seed, dump."""
    sub_registry = [m for m in REGISTRY if m.version <= target_version]
    db_path = str(tmp_path / f"snap_v{target_version}.db")
    return _apply_and_snapshot(db_path, sub_registry, seed=True)


@pytest.mark.parametrize(
    "migration",
    REGISTRY,
    ids=[f"v{m.version}-{type(m).__name__}" for m in REGISTRY],
)
def test_snapshot_per_version(tmp_path, migration):
    """REQ-TEST-1/2/3: snapshot of schema + data after applying migrations
    up to and including ``migration.version`` matches the checked-in file.
    """
    target = migration.version
    snap_path = _snapshot_path(target)
    actual = _build_snapshot_for_version(tmp_path, target)

    if os.environ.get("UPDATE_MIGRATION_SNAPSHOTS") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(_normalise_snapshot_for_compare(actual))
        pytest.skip(f"Updated snapshot v{target}")

    assert snap_path.exists(), (
        f"Missing snapshot file {snap_path}. Re-run with UPDATE_MIGRATION_SNAPSHOTS=1 to create."
    )
    expected = json.loads(snap_path.read_text())
    assert _normalise_snapshot_for_compare(expected) == _normalise_snapshot_for_compare(actual)


# --------------------------------------------------------------------------- #
# Migrations-from-empty equivalence with schema.sql (REQ-TEST-4)
# --------------------------------------------------------------------------- #


class TestSchemaSqlEquivalence:
    def test_chain_from_v0_matches_schema_sql(self, tmp_path):
        legacy_path = str(tmp_path / "legacy.db")
        fresh_path = str(tmp_path / "fresh.db")

        init = sqlite3.connect(legacy_path)
        init.executescript(_OLDEST_LEGACY_SCHEMA)
        init.close()

        apply_migrations(legacy_path)
        apply_migrations(fresh_path)

        l_db = sqlite3.connect(legacy_path)
        f_db = sqlite3.connect(fresh_path)
        try:
            assert_schemas_equal(get_schema_snapshot(f_db), get_schema_snapshot(l_db))
            # Version stamped to the same highest on both paths.
            assert read_version(l_db) == read_version(f_db)
        finally:
            l_db.close()
            f_db.close()


# --------------------------------------------------------------------------- #
# PRAGMA foreign_keys heuristic (REQ-TEST-7)
# --------------------------------------------------------------------------- #


_PRAGMA_FK_RE = re.compile(r"PRAGMA\s+foreign_keys", re.IGNORECASE)


def _scan_migration_for_unsafe_ops(migration: Migration) -> list[str]:
    """Flag ops known to confuse SQLite's transactional rollback.

    Currently: ``PRAGMA foreign_keys`` toggles inside the migration body
    (the PRAGMA is a no-op inside a tx, which tends to silently defeat
    the author's intent). Extend as we encounter more gotchas.
    """
    findings: list[str] = []
    if isinstance(migration, SqlFileMigration):
        sql_path = Path(inspect.getfile(migration.__class__)).resolve().parent / migration.sql_file
        if sql_path.exists():
            text = sql_path.read_text()
            if _PRAGMA_FK_RE.search(text):
                findings.append(f"{sql_path.name}: PRAGMA foreign_keys inside SQL migration body")
    # Also scan the Python source of custom up() methods.
    try:
        src = inspect.getsource(type(migration))
    except (OSError, TypeError):
        src = ""
    if _PRAGMA_FK_RE.search(src):
        findings.append(
            f"{type(migration).__module__}.{type(migration).__name__}: PRAGMA foreign_keys inside Python migration"
        )
    return findings


class TestPragmaHeuristic:
    def test_registered_migrations_have_no_unsafe_pragma_toggles(self):
        """Scan REGISTRY for PRAGMA foreign_keys toggles, which are
        known to silently no-op inside a transaction. Best-effort; emits a
        UserWarning rather than failing hard so migration authors can opt in
        with an inline justification comment.
        """
        findings: list[str] = []
        for mig in REGISTRY:
            findings.extend(_scan_migration_for_unsafe_ops(mig))
        for f in findings:
            _warnings.warn(f"Non-tx-safe op in migration: {f}", stacklevel=2)
        # The scan itself must run without error. Findings are advisory.

    def test_scanner_catches_pragma_in_sql_file(self, tmp_path):
        """Positive control: the heuristic actually flags a bad migration."""
        pkg_dir = tmp_path / "badpkg"
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("")
        (pkg_dir / "bad.sql").write_text(
            "PRAGMA foreign_keys=OFF;\nCREATE TABLE t (x INTEGER);\nPRAGMA foreign_keys=ON;\n"
        )
        (pkg_dir / "bad.py").write_text(
            "from compute_space.db.versioned import SqlFileMigration\n"
            "class Bad(SqlFileMigration):\n"
            "    version = 2\n"
            "    sql_file = 'bad.sql'\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            mod = importlib.import_module("badpkg.bad")
        finally:
            sys.path.pop(0)
        Bad = mod.Bad

        findings = _scan_migration_for_unsafe_ops(Bad())
        assert findings, "Scanner must flag PRAGMA foreign_keys in a migration SQL file"
        assert "PRAGMA foreign_keys" in findings[0]
