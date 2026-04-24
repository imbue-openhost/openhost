"""Snapshot tests for registered migrations.

Exercises each migration end-to-end by diffing real SQLite dumps
against committed golden files under
``compute_space/tests/snapshots/<set>/vNNNN.sql``. The framework
itself (base class, registry validation, runner behaviour, legacy
bootstrap, concurrency, etc.) is tested in
``test_versioned_migrations.py`` — this file is for the migrations.

Snapshot format
---------------

Each ``vNNNN.sql`` is the output of ``sqlite3.Connection.iterdump()``
joined with newlines, with ``sqlite_sequence`` lines filtered out
(environment-dependent AUTOINCREMENT state). Everything else is
included verbatim, including the ``schema_version`` row.

Layout::

    compute_space/tests/snapshots/
      <set-name>/
        v0001.sql    # state after legacy bootstrap + set-specific seed
        v0002.sql    # after applying REGISTRY up to v2 on top of v0001
        ...

Harness
-------

For each set, pair up the ``vNNNN.sql`` files *present* in sorted
order (not consecutive integers — a set with ``v0001.sql`` and
``v0015.sql`` only yields one pair covering the 2..15 chain). Per
pair ``(vA, vB)``: load ``vA.sql`` into a fresh tmp DB via
``executescript``, assert ``schema_version == A``, run
``apply_migrations(db, REGISTRY)``, assert ``schema_version == B``,
compute the dump, compare against ``vB.sql``.

Mismatch workflow
-----------------

On mismatch (or missing golden) the harness writes the actual dump
to ``<name>.sql.new`` next to the expected file and fails with a
unified diff. Review the ``.sql.new``; rename to ``.sql`` to accept.
No ``--update-snapshots`` pytest flag.

Bulk regeneration (initial bootstrap, wholesale resets) via
``_regenerate_snapshots`` at the bottom of this file — not a test,
invoked manually.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

from compute_space.db.versioned import REGISTRY
from compute_space.db.versioned import apply_migrations
from compute_space.db.versioned import read_version
from testing_helpers.schema_helpers import assert_schemas_equal
from testing_helpers.schema_helpers import get_schema_snapshot

SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"
_VERSION_FILE_RE = re.compile(r"^v(\d+)\.sql$")


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _snapshot_sets() -> list[tuple[str, list[Path]]]:
    """Discover ``snapshots/<set>/v*.sql`` groupings.

    Returns a list of ``(set_name, sorted_files)`` for every subfolder
    with at least one ``vNNNN.sql`` file.
    """
    if not SNAPSHOTS_DIR.exists():
        return []
    sets: list[tuple[str, list[Path]]] = []
    for set_dir in sorted(SNAPSHOTS_DIR.iterdir()):
        if not set_dir.is_dir():
            continue
        versioned: list[tuple[int, Path]] = []
        for f in set_dir.iterdir():
            m = _VERSION_FILE_RE.match(f.name)
            if m:
                versioned.append((int(m.group(1)), f))
        versioned.sort()
        if versioned:
            sets.append((set_dir.name, [p for _, p in versioned]))
    return sets


def _version_of(path: Path) -> int:
    m = _VERSION_FILE_RE.match(path.name)
    assert m, f"not a vNNNN.sql snapshot: {path.name}"
    return int(m.group(1))


def _snapshot_pairs() -> list[tuple[str, Path, Path]]:
    """Pairs of consecutive vN files *present* in each set."""
    out: list[tuple[str, Path, Path]] = []
    for set_name, files in _snapshot_sets():
        for a, b in zip(files, files[1:], strict=False):
            out.append((set_name, a, b))
    return out


def _dump_for_snapshot(db_path: str) -> str:
    """Render a DB to its canonical .sql snapshot form.

    Uses :meth:`sqlite3.Connection.iterdump`, joined with newlines. The
    only whitelisted exclusion is ``sqlite_sequence`` — its state is a
    function of prior AUTOINCREMENT allocations and is environment-noise
    for schema/data correctness checks.
    """
    db = sqlite3.connect(db_path)
    try:
        lines = [line for line in db.iterdump() if "sqlite_sequence" not in line]
    finally:
        db.close()
    return "\n".join(lines) + "\n"


def _write_new_and_fail(expected_path: Path, actual: str, msg_prefix: str) -> None:
    """Write ``<name>.sql.new`` next to the expected file and fail with a diff.

    Developer workflow: inspect the ``.sql.new``, rename to ``.sql`` once
    the change is intentional. No --update-snapshots flag.
    """
    new_path = expected_path.with_suffix(expected_path.suffix + ".new")
    new_path.write_text(actual)
    rel_expected = expected_path.relative_to(SNAPSHOTS_DIR.parent)
    rel_new = new_path.relative_to(SNAPSHOTS_DIR.parent)
    if expected_path.exists():
        expected = expected_path.read_text()
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(rel_expected),
                tofile=str(rel_new),
            )
        )
        pytest.fail(f"{msg_prefix}\nWrote actual dump to {rel_new}. Diff:\n{diff}")
    else:
        pytest.fail(
            f"{msg_prefix}\nMissing golden {rel_expected}. Wrote generated dump to {rel_new}; "
            "review and rename to accept."
        )


# --------------------------------------------------------------------------- #
# Pair-based snapshot tests (REQ-TEST-1..3)
# --------------------------------------------------------------------------- #


# Compute these once at import time. Calling ``_snapshot_sets()`` /
# ``_snapshot_pairs()`` inside the parametrize decorator *and* in the
# ``ids=`` kwarg would walk the filesystem twice for the same answer.
_SNAPSHOT_SETS = _snapshot_sets()
_SNAPSHOT_PAIRS = _snapshot_pairs()


def test_snapshots_are_present():
    """Fail loudly if the snapshot tree disappeared.

    Parametrized tests over an empty list silently degrade to "0 tests
    collected", which would hide accidental deletion of the snapshots
    directory. This guard makes that failure mode obvious.
    """
    assert _SNAPSHOT_SETS, (
        f"No snapshot sets found under {SNAPSHOTS_DIR}. "
        "Expected at least one subfolder with vNNNN.sql files. "
        "Regenerate with test_migration_snapshots._regenerate_snapshots()."
    )


@pytest.mark.parametrize(
    ("set_name", "va", "vb"),
    _SNAPSHOT_PAIRS,
    ids=[f"{name}-v{_version_of(a)}-to-v{_version_of(b)}" for name, a, b in _SNAPSHOT_PAIRS],
)
def test_snapshot_pair(set_name, va, vb, tmp_path):
    """From ``<set>/vA.sql``, apply REGISTRY, must dump to ``<set>/vB.sql``."""
    a_version = _version_of(va)
    b_version = _version_of(vb)

    db_path = str(tmp_path / f"{set_name}_v{a_version}_to_v{b_version}.db")
    # Load the starting snapshot verbatim.
    loader = sqlite3.connect(db_path)
    try:
        loader.executescript(va.read_text())
    finally:
        loader.close()

    probe = sqlite3.connect(db_path)
    try:
        assert read_version(probe) == a_version, (
            f"{va.relative_to(SNAPSHOTS_DIR.parent)} claims v{a_version} but loads as v{read_version(probe)}"
        )
    finally:
        probe.close()

    apply_migrations(db_path, registry=REGISTRY)

    probe = sqlite3.connect(db_path)
    try:
        final_version = read_version(probe)
    finally:
        probe.close()
    assert final_version == b_version, (
        f"After applying REGISTRY starting at v{a_version}, expected v{b_version} but DB is at v{final_version}"
    )

    actual = _dump_for_snapshot(db_path)
    expected = vb.read_text()
    if actual != expected:
        _write_new_and_fail(
            vb,
            actual,
            f"Snapshot mismatch for set={set_name} pair=v{a_version}->v{b_version}",
        )


# --------------------------------------------------------------------------- #
# Snapshot vs schema.sql sanity (REQ-TEST-4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("set_name", "files"),
    _SNAPSHOT_SETS,
    ids=[name for name, _ in _SNAPSHOT_SETS],
)
def test_latest_snapshot_schema_matches_schema_sql(set_name, files, tmp_path):
    """Latest snapshot's schema must match a fresh ``schema.sql`` init.

    Only the schema is compared — snapshots carry seed data whereas a
    fresh init is empty; ``assert_schemas_equal`` ignores row contents.
    """
    latest = files[-1]
    snap_path = str(tmp_path / f"{set_name}_latest.db")
    loader = sqlite3.connect(snap_path)
    try:
        loader.executescript(latest.read_text())
    finally:
        loader.close()

    fresh_path = str(tmp_path / f"{set_name}_fresh.db")
    apply_migrations(fresh_path, registry=REGISTRY)

    snap_db = sqlite3.connect(snap_path)
    fresh_db = sqlite3.connect(fresh_path)
    try:
        assert_schemas_equal(get_schema_snapshot(fresh_db), get_schema_snapshot(snap_db))
    finally:
        snap_db.close()
        fresh_db.close()


# --------------------------------------------------------------------------- #
# Snapshot generation (manual, not a test)
# --------------------------------------------------------------------------- #


def _seed_empty(_db: sqlite3.Connection) -> None:  # pragma: no cover - generator
    pass


def _seed_service_with_ports(db: sqlite3.Connection) -> None:  # pragma: no cover - generator
    """Minimal realistic seed: one app, one port mapping, one service provider."""
    db.execute(
        "INSERT INTO apps (name, manifest_name, version, runtime_type, repo_path, "
        "local_port, description, memory_mb, cpu_millicores, gpu, public_paths, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "orders",
            "orders",
            "1.0.0",
            "serverfull",
            "/repo/orders",
            19100,
            "Order service",
            256,
            500,
            0,
            "[]",
            "2024-01-01T00:00:00",
            "2024-01-01T00:00:00",
        ),
    )
    db.execute(
        "INSERT INTO app_port_mappings (app_name, label, container_port, host_port) VALUES (?,?,?,?)",
        ("orders", "grpc", 8000, 19500),
    )
    db.execute(
        "INSERT INTO service_providers (service_name, app_name) VALUES (?,?)",
        ("payments", "orders"),
    )


_SEEDS = {
    "empty": _seed_empty,
    "service_with_ports": _seed_service_with_ports,
}


def _regenerate_snapshots(root: Path = SNAPSHOTS_DIR) -> None:  # pragma: no cover - generator
    """Rebuild every ``<set>/vNNNN.sql`` from the live code + seed functions.

    Not a test. Invoke as::

        uv run --group dev python -c 'import sys; sys.path.insert(0, "compute_space/tests"); \\
            from test_migration_snapshots import _regenerate_snapshots; _regenerate_snapshots()'

    Writes the entire snapshot tree in place. Review diffs before committing.
    The harness's normal .sql.new workflow is preferred for small updates;
    this generator is for initial bootstrap and wholesale resets.
    """
    root.mkdir(parents=True, exist_ok=True)

    for set_name, seed in _SEEDS.items():
        set_dir = root / set_name
        set_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            # v1: bootstrap to v1 + seed.
            v1_db = str(td_path / f"{set_name}_v1.db")
            apply_migrations(v1_db, registry=[])
            with sqlite3.connect(v1_db, isolation_level=None) as conn:
                seed(conn)
            (set_dir / "v0001.sql").write_text(_dump_for_snapshot(v1_db))

            # Higher versions: start from the v1 dump, apply REGISTRY *up
            # to and including* the target version — not the full REGISTRY
            # — so vNNNN.sql captures the state exactly at version NNNN.
            # Passing the full registry would overshoot and every file
            # below the highest registered version would be mis-stamped.
            for migration in REGISTRY:
                target = migration.version
                sub_registry = [m for m in REGISTRY if m.version <= target]
                next_db = str(td_path / f"{set_name}_v{target}.db")
                loader = sqlite3.connect(next_db)
                try:
                    loader.executescript((set_dir / "v0001.sql").read_text())
                finally:
                    loader.close()
                apply_migrations(next_db, registry=sub_registry)
                (set_dir / f"v{target:04d}.sql").write_text(_dump_for_snapshot(next_db))
