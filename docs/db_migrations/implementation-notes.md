# Implementation Notes: yoyo-migrations adoption

## What changed

- Added `yoyo-migrations` to both `compute_space/pyproject.toml` and root
  `pyproject.toml`. The root pyproject is what `uv run` uses for the dev
  env, so yoyo must live there for tests to import it.
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
    frozen `legacy_migrate.migrate()`, then mark `0001` applied via
    `backend.mark_migrations()`, then apply any newer migrations.
  - `managed` (`_yoyo_migration` present): only apply pending migrations.
  `schema.sql` is no longer read at startup.
- Updated `testing_helpers/schema_helpers.py` to filter yoyo's internal
  tables (`_yoyo_*`, `yoyo_lock`) out of schema snapshots so migration
  tests compare only application schema.
- Added `TestYoyoDispatch` test class covering the three paths
  (REQ-TESTS-2/3/4) plus a test that monkey-patches `migrate` to prove it
  is never invoked on a managed DB (REQ-STARTUP-2).

## Deviation from requirements (flagged)

`legacy_migrate.migrate()` as originally written does NOT produce the full
`0001` baseline. Historically `schema.sql` was executed on every startup
after `migrate()` and silently created tables that `migrate()` doesn't
touch: `app_databases`, `api_tokens`, `service_providers`, `permissions`,
plus a couple of indexes. Those tables were added to the codebase assuming
`schema.sql` would keep creating them.

To honor REQ-STARTUP-3 ("0001 MUST be marked applied in yoyo without
re-executing it") I appended `CREATE TABLE/INDEX IF NOT EXISTS` statements
for all of these to the end of `legacy_migrate.migrate()`. This strictly
*extends* `migrate()` so that after it runs the DB matches the 0001
baseline. It does not modify the existing imperative ALTER/recreate logic.
The alternative was to let yoyo apply `0001` on the legacy path (safe,
because every statement is IF-NOT-EXISTS), but that would have violated
REQ-STARTUP-3 literally.

If you would rather have 0001 actually run on legacy DBs, delete the new
`executescript` block at the end of `legacy_migrate.migrate()` and change
the legacy branch of `init_db()` to call `backend.apply_migrations(...)`
instead of `backend.mark_migrations(migrations[:1])`.

## How to verify

From repo root:

    uv run --group dev pytest compute_space/tests/ -v
    uv run --group dev pytest
    pre-commit run --all-files

All pass (19 migration tests, 198 lightweight suite). `mypy` clean.

## Open questions

- Root `pyproject.toml` also got a new `yoyo-migrations` entry even though
  the plan said "not root — compute_space has its own". The root pyproject
  is what the dev shell uses to install deps; without it, `import yoyo`
  fails in tests. If the intent was to keep root thin, we should instead
  wire the dev env to pick up `compute_space/pyproject.toml` directly.
- Yoyo emits a `DeprecationWarning` about Python 3.12's default datetime
  adapter. Harmless but noisy in test output.
- `schema.sql` is retained as reference but no code reads it at startup
  anymore. `_schema_path()` in `legacy_migrate.py` still points at it and
  is used by `_recreate_table()` for the legacy path — that usage is
  unchanged and still correct because legacy DBs migrating via
  `migrate()` predate the switch.
