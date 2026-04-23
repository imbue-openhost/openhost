"""
Tests for the yoyo-managed migration path: the 3-state ``init_db`` dispatch
plus a snapshot / golden-test harness that rebuilds every scenario fixture
by walking the yoyo migrations forward.

The snapshot harness guards against silent DDL or data-handling drift:
any divergence between ``at_<NNNN>.sql`` and the live migration output is
a test failure.  Re-run
``uv run python compute_space/tests/fixtures/migrations/regenerate.py``
to deliberately update the fixtures.
"""

import difflib
import hashlib
import sqlite3

import pytest

from compute_space.db import connection as _connection_module
from compute_space.db.connection import _classify_db_state
from testing_helpers.schema_helpers import assert_schemas_equal as _assert_schemas_equal
from testing_helpers.schema_helpers import get_schema_snapshot as _get_schema_snapshot

from ._migration_helpers import OLDEST_ROUTER_SCHEMA
from ._migration_helpers import fresh_db as _fresh_db
from ._migration_helpers import run_init_db as _run_init_db
from ._snapshot_harness import apply_pending
from ._snapshot_harness import discover_scenario_dirs
from ._snapshot_harness import dump_application_db
from ._snapshot_harness import fixture_path
from ._snapshot_harness import load_snapshot
from ._snapshot_harness import materialize_state
from ._snapshot_harness import present_snapshot_ids
from ._snapshot_harness import snapshot_header


def _fresh_bootstrap_snapshot(tmp_path):
    """Snapshot of a freshly-bootstrapped DB, built once per test invocation."""
    fresh_path = str(tmp_path / "fresh_expected.db")
    _fresh_db(fresh_path)
    fresh_db = sqlite3.connect(fresh_path)
    try:
        return _get_schema_snapshot(fresh_db)
    finally:
        fresh_db.close()


def _applied_yoyo_migrations(db_path):
    """Return the set of migration ids yoyo has marked as applied."""
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute("SELECT migration_id FROM _yoyo_migration").fetchall()
        return {row[0] for row in rows}
    finally:
        db.close()


class TestYoyoDispatch:
    """Cover the three init_db startup paths: fresh / legacy / managed.

    Fresh -> 0001 schema parity is validated here, not in the snapshot
    suite.  Snapshots additionally include scenario-specific data that
    a migration alone does not produce, so snapshots can't substitute
    for the fresh-DB schema check.
    """

    def test_fresh_db_applies_all_migrations(self, tmp_path):
        """Empty file -> yoyo creates every table from 0001 and records it."""
        db_path = str(tmp_path / "fresh.db")
        sqlite3.connect(db_path).close()

        db = sqlite3.connect(db_path)
        assert _classify_db_state(db) == "fresh"
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        snap = _get_schema_snapshot(db)
        has_yoyo = (
            db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='_yoyo_migration'").fetchone()
            is not None
        )
        db.close()

        assert has_yoyo, "yoyo tracking table must exist after init_db on fresh DB"
        _assert_schemas_equal(_fresh_bootstrap_snapshot(tmp_path), snap)
        assert "0001_initial" in _applied_yoyo_migrations(db_path)

    def test_legacy_db_runs_migrate_and_applies_0001(self, tmp_path):
        """Legacy DB with apps table -> migrate() runs, then yoyo applies all
        migrations (0001's IF-NOT-EXISTS statements are no-ops for tables
        migrate() already built, and fill in any it doesn't touch). Data is
        preserved and the final schema matches a fresh DB.
        """
        db_path = str(tmp_path / "legacy.db")
        db = sqlite3.connect(db_path)
        db.executescript(OLDEST_ROUTER_SCHEMA)
        db.execute(
            "INSERT INTO apps (name, base_path, subdomain, version, runtime_type, repo_path, local_port) "
            "VALUES ('legacyapp', '/legacyapp', 'legacyapp', '1.0', 'serverfull', '/repo', 9010)"
        )
        db.execute("INSERT INTO refresh_tokens (token, expires_at) VALUES ('legacy-token', '2099-01-01T00:00:00')")
        db.commit()
        assert _classify_db_state(db) == "legacy"
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        apps_row = db.execute("SELECT name FROM apps WHERE name='legacyapp'").fetchone()
        token_row = db.execute("SELECT token_hash FROM refresh_tokens").fetchone()
        snap = _get_schema_snapshot(db)
        db.close()

        assert apps_row is not None
        assert token_row["token_hash"] == hashlib.sha256(b"legacy-token").hexdigest()
        assert "0001_initial" in _applied_yoyo_migrations(db_path)
        _assert_schemas_equal(_fresh_bootstrap_snapshot(tmp_path), snap)

    def test_managed_db_skips_legacy_migrate(self, tmp_path, monkeypatch):
        """Once yoyo tracks the DB, the frozen legacy migrate() must never run again."""
        db_path = str(tmp_path / "managed.db")
        sqlite3.connect(db_path).close()
        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        assert _classify_db_state(db) == "managed"
        db.close()

        calls: list = []

        def _boom(connection):
            calls.append(connection)
            raise AssertionError("migrate() must not run on a managed DB")

        monkeypatch.setattr(_connection_module, "migrate", _boom)

        _run_init_db(db_path)
        assert calls == []

        db = sqlite3.connect(db_path)
        snap = _get_schema_snapshot(db)
        db.close()
        _assert_schemas_equal(_fresh_bootstrap_snapshot(tmp_path), snap)

    def test_managed_db_preserves_data_across_restart(self, tmp_path):
        """Restarting on a managed DB must not clobber data inserted between runs."""
        db_path = str(tmp_path / "restart.db")
        sqlite3.connect(db_path).close()
        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port) VALUES ('after-boot', '1.0', '/repo', 9100)"
        )
        db.commit()
        db.close()

        _run_init_db(db_path)

        db = sqlite3.connect(db_path)
        row = db.execute("SELECT name FROM apps WHERE name='after-boot'").fetchone()
        db.close()
        assert row is not None


# ---------------------------------------------------------------------------
# Snapshot / golden-test harness
#
# For each scenario enumerate adjacent pairs (from, to) of checked-in
# ``at_<NNNN>.sql`` fixtures — skip-chains are transitively implied.
# For each pair:
#   1. Materialize the ``from`` snapshot (schema + data + yoyo tracking).
#   2. Hand the DB to yoyo — the loaded tracking rows tell yoyo which
#      migrations to skip; it applies everything up to ``to``.
#   3. Dump and compare to ``at_<to>.sql``.
#
# Scenarios with a single snapshot contribute zero cases (no pairs).
# There is no implicit "empty" starting state — snapshots encode both
# schema and data, and the first snapshot of each scenario is hand-
# bootstrapped because application data is not produced by migrations.
# ---------------------------------------------------------------------------


def _snapshot_cases():
    """Yield (scenario_dir, from_id, to_id) for each adjacent pair of
    committed snapshots. Non-adjacent pairs are transitively implied:
    if 1->2 and 2->3 each replay exactly, 1->3 must too."""
    cases = []
    for scenario_dir in discover_scenario_dirs():
        present = present_snapshot_ids(scenario_dir)
        for from_id, to_id in zip(present, present[1:], strict=True):
            cases.append((scenario_dir, from_id, to_id))
    return cases


def _case_id(case):
    scenario_dir, from_id, to_id = case
    return f"{scenario_dir.name}-{from_id.split('_', 1)[0]}->{to_id.split('_', 1)[0]}"


class TestSnapshot:
    """Round-trip every (from, to) pair of committed snapshots."""

    @pytest.mark.parametrize("case", _snapshot_cases(), ids=_case_id)
    def test_migration_produces_expected_snapshot(self, case, tmp_path):
        scenario_dir, from_id, to_id = case
        db_path = str(tmp_path / "snapshot.db")

        from_sql = load_snapshot(scenario_dir, from_id)
        assert from_sql is not None, f"missing fixture at_{from_id}.sql for {scenario_dir.name}"
        materialize_state(db_path, from_sql)

        apply_pending(db_path, up_to_inclusive=to_id)

        conn = sqlite3.connect(db_path)
        try:
            actual = dump_application_db(conn, header_lines=snapshot_header(scenario_dir.name, to_id))
        finally:
            conn.close()

        expected_path = fixture_path(scenario_dir, to_id)
        expected = expected_path.read_text()

        if actual != expected:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile=f"{expected_path} (committed)",
                    tofile="live migration output",
                )
            )
            pytest.fail(
                f"Snapshot drift for {scenario_dir.name} {from_id} -> {to_id}.\n"
                f"Re-run regenerate.py if the change is intentional.\n{diff}"
            )
