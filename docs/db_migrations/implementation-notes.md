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

## Snapshot-test refactor

The single `compute_space/tests/test_migrations.py` split into two files
focused on the two migration paths, plus a reusable golden-test harness:

- `compute_space/tests/_migration_helpers.py` — shared helpers (`FakeApp`,
  `fresh_db`, `run_init_db`, `OLDEST_ROUTER_SCHEMA`, and path constants).
- `compute_space/tests/test_legacy_migrations.py` — `TestRouterMigrations`
  and `TestCrashRecovery` (tests that exercise the frozen
  `legacy_migrate.migrate()` and the idempotent 0001 re-apply).
- `compute_space/tests/test_yoyo_migrations.py` — `TestYoyoDispatch` (the
  three-state startup) plus the new parametrized `TestSnapshot` class.
- `compute_space/tests/_snapshot_harness.py` — enumerates yoyo migrations,
  walks them one at a time with a per-checkpoint seed hook, and produces
  a canonical textual dump that round-trips through `executescript`.

The original `test_migrations.py` was deleted.

`TestYoyoDispatch` no longer hard-codes the set of expected tables
(`_EXPECTED_TABLES` is gone).  Its three former uses now compare against
a fresh-bootstrap snapshot built from `schema.sql` via
`testing_helpers.schema_helpers.assert_schemas_equal` — the same pattern
already used by the legacy tests.  Adding a column in a new migration no
longer requires updating a duplicated table list.

### Snapshot fixtures

`compute_space/tests/fixtures/migrations/` holds one directory per
scenario.  Two scenarios are present:

- `scenario_refresh_tokens/` — four `refresh_tokens` rows covering every
  combination of `revoked` and expiry windows, plus the minimum `owner`
  row.
- `scenario_port_mappings/` — two apps each with three
  `app_port_mappings` rows at distinct labels and ports, plus `owner`.

Each `at_<NNNN>.sql` is a full textual dump of the scenario's database
after yoyo migration `NNNN` (and all prior) have been applied.  Files
open with a `-- Generated by regenerate.py` banner and are committed to
the repo.

### Test harness

`TestSnapshot.test_migration_produces_expected_snapshot` is
parametrised over every `(scenario, from, to)` triple where `from` is
`empty` or an existing `at_<N>.sql` and `to` is a later `at_<N>.sql`.
Each case:

1. Materializes the `from` state in a temp sqlite file (empty or via
   `executescript(at_<from>.sql)`).
2. Walks yoyo migrations `(from, to]` using `backend.to_apply()` filtered
   on migration id, seeding via the scenario's `seed_at` hook after each
   migration (matching the generator).
3. Dumps the DB and compares to the committed `at_<to>.sql`; any drift
   surfaces as a unified diff in the pytest failure.

### Regenerator

`compute_space/tests/fixtures/migrations/regenerate.py` is the single
entry point for producing fixtures:

    uv run python compute_space/tests/fixtures/migrations/regenerate.py

It discovers every `scenario_*` directory, imports its `builder.py`
dynamically, walks the yoyo migrations in order, and writes
`at_<NNNN>.sql` after each checkpoint.  `--check` mode exits non-zero
without writing if anything would change — handy for CI.  Adding a new
yoyo migration (e.g. `0002_*.sql`) and re-running produces an
`at_0002.sql` for every scenario with no builder edit required, because
builders only know about *data*, not migration ids.

## Snapshot harness v2

The first-cut snapshot harness (above) kept per-scenario `builder.py`
modules and walked migrations one at a time, calling a `seed_at` hook
between each step.  That design muddled migration state with data
loading: every snapshot test both applied migrations and re-ran the
builder, so drift in either path looked like drift in the other, and
adding a new migration required thinking about whether every scenario's
builder was still reachable at the right checkpoint.

v2 flips the model to **pure replay** from committed snapshots:

- **Builders are gone.**  Each scenario's `at_<NNNN>.sql` is the full
  source of truth for both schema and data at that migration point.
  There is no more `builder.py`, no `seed_at`, no per-step hook.  The
  first snapshot of each scenario is hand-bootstrapped (application data
  + one `_yoyo_migration` row recording that 0001 is applied); every
  subsequent snapshot is derived by replaying migrations from the
  previous one.

- **Snapshots embed yoyo tracking.**  `_yoyo_migration` and
  `_yoyo_version` rows live in the dumps (with timestamps canonicalised
  to `2025-01-01 00:00:00`).  When yoyo opens a loaded snapshot it sees
  "0001 applied, version 2" and treats only the later migrations as
  pending.  The DDL for `_yoyo_log` and `yoyo_lock` is emitted too, with
  rows stripped: yoyo writes to both during apply and won't recreate
  missing tables once `_yoyo_version` is already at max.

- **`regenerate.py` is a pure-replay CLI.**
  `regenerate.py [--scenario NAME] [--from FROM_ID] [--to TO_ID] [--check]`
  loads `at_<FROM>.sql`, hands the DB to yoyo
  (`backend.apply_migrations(backend.to_apply(...))`), dumps, writes
  `at_<TO>.sql`.  `--from` defaults to the highest existing snapshot per
  scenario; `--to` defaults to the latest migration; `--check` diffs
  against the committed file for CI.

- **Test harness is pair-only.**  `TestSnapshot` enumerates ordered
  pairs `(from_id, to_id)` of existing `at_*.sql` files and verifies
  that replay from `from` produces exactly `to`.  No virtual `empty`
  starting state — fresh → 0001 schema parity is validated by
  `TestYoyoDispatch.test_fresh_db_applies_all_migrations` instead
  (snapshots include data the migration itself doesn't produce, so they
  can't substitute for that schema check).

- **`0002_dummy.py` lives in `compute_space/db/migrations/`** — a no-op
  migration (`steps: list[step] = []`) whose sole purpose is to give
  the harness a real migration pair to exercise.  When a real next
  migration comes along it should take the next number (0003_…); leave
  0002 alone so existing snapshots continue to chain unchanged.
