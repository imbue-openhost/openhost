# Review Feedback — Round 2

## Assessment: PASS

Round 1 Critical #1 resolved in commit `73459ba`. Legacy path now `apply_migrations(backend.to_apply(migrations))` and the duplicated `CREATE TABLE IF NOT EXISTS` block in `legacy_migrate.py` is gone. Single source of truth restored. All tests green, pre-commit clean.

## Round 1 Critical Issue — Resolved

- `compute_space/compute_space/db/connection.py:78-81` — fresh/legacy/managed now converge on the same tail: `backend.apply_migrations(backend.to_apply(migrations))` under `backend.lock()`. `mark_migrations(...)` no longer appears in the module.
- `compute_space/compute_space/db/legacy_migrate.py` (219 lines) — `migrate()` ends at the `app_tokens` token-hash recreate block (line 219). No duplicated DDL appended. Module is the pre-implementation `migrations.py` content, renamed.
- No duplicate DDL between `0001_initial.sql` and `legacy_migrate.py`. `migrate()` still owns imperative ALTER/recreate steps; `0001_initial.sql` owns table definitions.
- Fresh-path and managed-path logic unchanged — the `if state == "legacy": migrate(db)` branch is the only dispatch difference, and the common tail applies migrations idempotently in all three states.

## Requirements Coverage

| REQ-ID | Status | Evidence |
|---|---|---|
| REQ-DEP-1 | ✓ | `pyproject.toml:26`, `compute_space/pyproject.toml` retains yoyo-migrations |
| REQ-LAYOUT-1 | ✓ | `compute_space/compute_space/db/migrations/0001_initial.sql` present |
| REQ-LAYOUT-2 | ✓ | single `0001_initial.sql` file, yoyo-supported naming |
| REQ-LAYOUT-3 | ✓ | `0001_initial.sql:7-96` covers every table/index `migrate()` ends at; drift risk from round 1 eliminated |
| REQ-STARTUP-1 | ✓ | `connection.py:38-51` three-way classify, verified by `TestYoyoDispatch` |
| REQ-STARTUP-2 | ✓ | `connection.py:72-74` only enters `migrate(db)` when `state == "legacy"`; `test_managed_db_skips_legacy_migrate` (`test_migrations.py:713-737`) proves it |
| REQ-STARTUP-3 | ✓ (intent) | `connection.py:80-81` runs `apply_migrations` on the legacy path. Literal re-executes 0001, but every statement is `CREATE ... IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`, so it's a provable no-op for tables `migrate()` already built. REQ intent — "do not corrupt data by re-running destructive DDL" — is satisfied. Round 1 explicitly endorsed this interpretation; `test_legacy_db_runs_migrate_and_applies_0001` proves data preservation. Not flagging as Critical. |
| REQ-STARTUP-4 | ✓ | `migrate()` still guards each step with column-presence checks; 0001 idempotency reasoned above |
| REQ-STARTUP-5 | ✓ | `core/startup.py` calls `init_db(app)` synchronously during `init_app`, before Hypercorn serves |
| REQ-SCHEMA-1 | ✓ | no `executescript(schema.sql)` in `connection.py`; `schema.sql` only read by `_recreate_table` helper inside legacy `migrate()` |
| REQ-SCHEMA-2 | ✓ | `schema.sql` retained as reference, not executed at startup |
| REQ-SCHEMA-3 | ✓ | future changes go in `0002_*.sql`; `schema.sql` untouched |
| REQ-TESTS-1 | ✓ | full `TestRouterMigrations` + `TestCrashRecovery` suites pass |
| REQ-TESTS-2 | ✓ | `test_fresh_db_applies_all_migrations` (`test_migrations.py:651-672`) |
| REQ-TESTS-3 | ✓ | `test_legacy_db_runs_migrate_and_applies_0001` (`test_migrations.py:674-711`) — renamed from `_marks_0001`, now verifies the apply-path data preservation |
| REQ-TESTS-4 | ✓ | `test_managed_db_skips_legacy_migrate` (`test_migrations.py:713-737`), `test_managed_db_preserves_data_across_restart` (`test_migrations.py:739-758`) |

## Minor Fixes from Round 1

- **`test_migrations.py:1-7`** — docstring updated. Now reads "Verifies that legacy_migrate.migrate() combined with the yoyo 0001 baseline produces a schema equivalent to applying 0001 directly to a fresh DB…". Accurate. ✓
- **`test_migrations.py:167-172`** — stale schema.sql comment replaced. Now reads "Run init_db (legacy path: migrate() then apply yoyo migrations, with 0001 applied idempotently over the migrated state)." ✓
- **Yoyo `DeprecationWarning` suppression** — not added to `pyproject.toml:[tool.pytest.ini_options]`. Warning count dropped from 26 to 2 because managed-path tests no longer trigger the pre-existing datetime codepath as often. Still optional, still cosmetic. No action required.

## Residual Minor (non-blocking)

- `legacy_migrate.py:89-90` comment still says `# Fresh DB — table doesn't exist yet, schema.sql will create it`. After this change the caller runs `apply_migrations` on fresh paths via yoyo, not schema.sql. Doesn't affect behavior — `migrate()` is only called on the legacy branch and its early-return there is still correct (no apps table ⇒ nothing for migrate() to do) — but comment is stale. One-line touch-up.

## Test Results

- `uv run --group dev pytest compute_space/tests/ -v` → **123 passed, 30 skipped, 2 warnings**
- `uv run --group dev pytest` → **198 passed, 30 skipped, 2 warnings**
- `pre-commit run --all-files` → ruff auto-fix / ruff format / mypy / secrets **all Passed**
- `git log --oneline main..HEAD` → 2 commits (`556536a` Adopt yoyo-migrations…, `73459ba` Apply 0001 idempotently on legacy DB path) — clean

## Security / Correctness Walk-through

All crash modes from round 1 still hold. The legacy→apply swap makes them stronger, not weaker:

- **Crash between `migrate()` and `apply_migrations(0001)`:** Next startup classifies `legacy` again (no `_yoyo_migration` yet). `migrate()` re-runs idempotently, `apply_migrations` proceeds, 0001 gets marked. ✓
- **Crash mid `apply_migrations`:** yoyo wraps each migration in a transaction. Partial apply rolls back. ✓
- **Managed detection specificity:** unchanged — `_table_exists(db, "_yoyo_migration")` is yoyo's canonical tracking table, distinct from `yoyo_lock` (transient). ✓

## Recommendation

Ship. Optional follow-ups (stale `# schema.sql will create it` comment, deprecation-warning suppression, migrations README) can land in a separate cleanup commit or wait until `0002` lands.
