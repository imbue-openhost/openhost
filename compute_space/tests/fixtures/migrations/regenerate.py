#!/usr/bin/env python3
"""Regenerate ``at_<NNNN>.sql`` snapshots by pure migration replay.

Usage:
  regenerate.py [--scenario NAME] [--from FROM_ID] [--to TO_ID] [--check]

For each scenario the script loads ``at_<FROM>.sql`` into a fresh DB,
hands the DB to yoyo (``backend.apply_migrations(backend.to_apply(...))``
— the tracking rows in the loaded snapshot tell yoyo what's already
applied), dumps the post-apply state, and writes ``at_<TO>.sql``.

Defaults:
  --scenario : iterate every scenario_* directory
  --from     : highest existing at_*.sql in the scenario dir (error if
               none exists — bootstrap that first snapshot by hand, then
               re-run).
  --to       : the latest migration in the migrations dir.

With ``--check`` the output is written to a tempdir and compared byte-
for-byte against the committed fixture; non-zero exit on any mismatch
or missing file.  Safe to run in CI / pre-commit.

This script does NOT inject scenario data.  The first snapshot of each
scenario is hand-curated (application data + one ``_yoyo_migration``
row) and every subsequent snapshot is produced by replaying migrations
from the previous one.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

# Make the shared harness importable as a top-level module when invoked as
# a plain script.  ``compute_space.tests`` is not a real subpackage of the
# installed ``compute_space`` wheel — tests live on disk alongside it — so
# we put the tests directory on ``sys.path`` and import directly.
_TESTS_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[4]
for _p in (_REPO_ROOT, _TESTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _snapshot_harness import apply_pending  # noqa: E402
from _snapshot_harness import discover_scenario_dirs  # noqa: E402
from _snapshot_harness import dump_application_db  # noqa: E402
from _snapshot_harness import fixture_path  # noqa: E402
from _snapshot_harness import list_migration_ids  # noqa: E402
from _snapshot_harness import load_snapshot  # noqa: E402
from _snapshot_harness import materialize_state  # noqa: E402
from _snapshot_harness import present_snapshot_ids  # noqa: E402
from _snapshot_harness import resolve_migration_id  # noqa: E402
from _snapshot_harness import snapshot_header  # noqa: E402


def _replay(scenario_dir: Path, from_id: str, to_id: str) -> str:
    """Load at_<from>.sql, apply migrations up to to_id, return the dump."""
    from_sql = load_snapshot(scenario_dir, from_id)
    if from_sql is None:
        raise RuntimeError(f"missing fixture at_{from_id.split('_', 1)[0]}.sql for {scenario_dir.name}")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "replay.db")
        materialize_state(db_path, from_sql)
        apply_pending(db_path, up_to_inclusive=to_id)
        conn = sqlite3.connect(db_path)
        try:
            return dump_application_db(conn, header_lines=snapshot_header(scenario_dir.name, to_id))
        finally:
            conn.close()


def _resolve_from(scenario_dir: Path, user_from: str | None) -> str:
    if user_from is not None:
        return resolve_migration_id(user_from)
    present = present_snapshot_ids(scenario_dir)
    if not present:
        raise RuntimeError(
            f"{scenario_dir.name}: no at_*.sql found — bootstrap the first snapshot by hand, then re-run"
        )
    return present[-1]


def _resolve_to(user_to: str | None) -> str:
    if user_to is not None:
        return resolve_migration_id(user_to)
    ids = list_migration_ids()
    if not ids:
        raise RuntimeError("no migrations found")
    return ids[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", help="Single scenario name (e.g. scenario_port_mappings)")
    parser.add_argument("--from", dest="from_id", help="Starting migration id or prefix")
    parser.add_argument("--to", dest="to_id", help="Ending migration id or prefix")
    parser.add_argument("--check", action="store_true", help="Compare to on-disk fixtures; non-zero exit on drift")
    args = parser.parse_args()

    all_scenarios = discover_scenario_dirs()
    if args.scenario:
        scenarios = [d for d in all_scenarios if d.name == args.scenario]
        if not scenarios:
            print(f"No scenario named {args.scenario!r}", file=sys.stderr)
            return 1
    else:
        scenarios = all_scenarios
    if not scenarios:
        print("No scenarios found.", file=sys.stderr)
        return 1

    to_id = _resolve_to(args.to_id)

    drift = False
    for scenario_dir in scenarios:
        from_id = _resolve_from(scenario_dir, args.from_id)
        dump = _replay(scenario_dir, from_id, to_id)
        out = fixture_path(scenario_dir, to_id)

        if args.check:
            existing = out.read_text() if out.exists() else None
            if existing != dump:
                drift = True
                status = "missing" if existing is None else "drift"
                print(f"  {status}: {out.relative_to(scenario_dir.parent)}", file=sys.stderr)
            else:
                print(f"{scenario_dir.name}: ok ({from_id} -> {to_id})")
        else:
            out.write_text(dump)
            print(f"{scenario_dir.name}: wrote {out.name} ({from_id} -> {to_id})")

    return 1 if drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
