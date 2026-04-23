# Implementation Notes: yoyo-migrations adoption

## What changed

- Added `yoyo-migrations` to both `compute_space/pyproject.toml` and root
  `pyproject.toml`. Both entries are required: the root hatch wheel
  packages `compute_space/compute_space`, and `uv run` resolves against
  the root project, so `import yoyo` must be satisfied there at runtime
  and in the dev/test shell.
- Renamed `compute_space/compute_space/db/migrations.py` →
  `compute_space/compute_space/db/legacy_migrate.py`. The file name had to
  change because the new migrations *directory* collides with the module
  name and yoyo mistakes `__init__.py` for a migration (it filters only by
  suffix `.py`/`.sql`).
- Created `compute_space/compute_space/db/migrations/0001_initial.sql`
  mirroring the current `schema.sql`, all statements using
  `CREATE ... IF NOT EXISTS`.
- Refactored `init_db()` in `connection.py` to dispatch on three DB states:
  - `fresh` (no tables): yoyo applies all migrations.
  - `legacy` (`apps` or `apps_new` present, no `_yoyo_migration`): run the
    frozen `legacy_migrate.migrate()`, then apply all yoyo migrations.
    0001 is composed entirely of `CREATE ... IF NOT EXISTS` statements,
    so applying it over a legacy DB is a no-op for tables `migrate()`
    already built and fills in any it doesn't touch.
  - `managed` (`_yoyo_migration` present): only apply pending migrations.
  `schema.sql` is no longer read at startup.
- Updated `testing_helpers/schema_helpers.py` to filter yoyo's internal
  tables (`_yoyo_*`, `yoyo_lock`) out of schema snapshots so migration
  tests compare only application schema.
- Added `TestYoyoDispatch` test class covering the three paths
  (REQ-TESTS-2/3/4) plus a test that monkey-patches `migrate` to prove it
  is never invoked on a managed DB (REQ-STARTUP-2).

## How to verify

From repo root:

    uv run --group dev pytest compute_space/tests/ -v
    uv run --group dev pytest
    pre-commit run --all-files

All pass. `mypy` clean.

## Notes

- Yoyo emits a `DeprecationWarning` about Python 3.12's default datetime
  adapter. Harmless but noisy in test output. Upstream yoyo issue, not
  ours; leave in place.
- `schema.sql` is retained as reference but no code reads it at startup
  anymore. `_schema_path()` in `legacy_migrate.py` still points at it and
  is used by `_recreate_table()` for the legacy path — that usage is
  unchanged and still correct because legacy DBs migrating via
  `migrate()` predate the switch. A future cleanup could delete
  `schema.sql` once `_recreate_table` is refactored to hold its DDL
  literals inline.

## Round 1 fixes

Review feedback at `docs/db_migrations/review-feedback-round1.md` flagged
DDL drift between `0001_initial.sql` and a new block appended to
`legacy_migrate.migrate()`. Applied the reviewer's recommendation:

- `connection.py`: the legacy branch now runs
  `backend.apply_migrations(backend.to_apply(migrations))` for *all*
  migrations (same as the fresh/managed branches). The previous
  `backend.mark_migrations(migrations[:1])` call is removed. 0001's
  IF-NOT-EXISTS statements apply idempotently on a legacy DB already at
  baseline.
- `legacy_migrate.py`: removed the `CREATE TABLE/INDEX IF NOT EXISTS`
  `executescript` block that had been appended to the end of `migrate()`.
  `migrate()` is back to its pre-change, frozen state. `0001_initial.sql`
  is now the sole source of truth for the baseline schema.
- `test_migrations.py`: tightened file-level docstring, fixed a stale
  in-line comment that still referenced `schema.sql` execution, and
  renamed `test_legacy_db_runs_migrate_and_marks_0001` →
  `test_legacy_db_runs_migrate_and_applies_0001` with an updated
  docstring. Assertions unchanged (post-state is identical).
- `implementation-notes.md`: removed the "Deviation from requirements"
  section (deviation is eliminated) and closed the root-`pyproject.toml`
  open question — per the review, the root entry is required for runtime
  imports inside the deployed wheel.
