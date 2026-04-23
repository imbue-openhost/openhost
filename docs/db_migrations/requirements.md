# Requirements: Structured DB Migration System

## REQ-DEP: Dependencies

**REQ-DEP-1**: `yoyo-migrations` MUST be added as a runtime dependency of `compute_space` in `pyproject.toml`.

---

## REQ-LAYOUT: Migration File Layout

**REQ-LAYOUT-1**: A migrations directory MUST exist at `compute_space/compute_space/db/migrations/`.

**REQ-LAYOUT-2**: Migration files MUST follow the naming convention `{id}_{description}.sql` or `{id}_{description}.py` (yoyo supports both). Mixed formats within the directory are acceptable.

**REQ-LAYOUT-3**: Migration `0001_initial` MUST contain the full current schema as `CREATE TABLE IF NOT EXISTS` statements, matching the tables currently defined in `schema.sql`. This migration represents the baseline state after all historical `migrate()` operations have run.

---

## REQ-STARTUP: Startup / Migration Dispatch

**REQ-STARTUP-1**: On every application startup, `init_db()` MUST detect which of three DB states is present and handle each:

| State | Detection | Action |
|---|---|---|
| Legacy | `apps` table exists, `_yoyo_migration` table absent | Run `migrate()`, mark `0001` applied, apply pending |
| Fresh | Neither `apps` nor `_yoyo_migration` present | Apply all migrations via yoyo |
| Managed | `_yoyo_migration` table present | Apply pending migrations via yoyo |

**REQ-STARTUP-2**: `migrate()` MUST NOT be called on any DB that already has a `_yoyo_migration` tracking table.

**REQ-STARTUP-3**: After `migrate()` completes on a legacy DB, migration `0001` MUST be marked applied in yoyo without re-executing it. Any migrations newer than `0001` MUST then be applied normally.

**REQ-STARTUP-4**: If `migrate()` is interrupted and the process restarts, the retry MUST be safe. `migrate()` is idempotent; re-running it MUST NOT corrupt data or leave the DB in an inconsistent state.

**REQ-STARTUP-5**: Migration application MUST be synchronous and complete before the application begins serving requests.

---

## REQ-SCHEMA: Schema Source of Truth

**REQ-SCHEMA-1**: `schema.sql` MUST NOT be executed at startup after this change. `init_db()` MUST NOT call `executescript(schema.sql)`.

**REQ-SCHEMA-2**: Migration `0001` becomes the authoritative baseline schema. `schema.sql` MAY be retained in the repository as a human-readable reference but plays no role in DB initialization.

**REQ-SCHEMA-3**: All future schema changes MUST be expressed as new numbered migration files, not modifications to existing migrations or `schema.sql`.

---

## REQ-TESTS: Test Coverage

**REQ-TESTS-1**: Existing tests in `compute_space/tests/test_migrations.py` MUST continue to pass.

**REQ-TESTS-2**: Tests MUST cover the fresh-DB path: starting from an empty DB, yoyo applies migrations and all expected tables exist.

**REQ-TESTS-3**: Tests MUST cover the legacy-DB path: starting from a DB with existing tables but no yoyo tracking table, `migrate()` runs, `0001` is marked applied, and the DB ends in the correct final state.

**REQ-TESTS-4**: Tests MUST cover the managed-DB path: starting from a DB already at `0001`, startup no-ops cleanly.

---

## Out of Scope

- Down/rollback migrations
- SQLAlchemy ORM or any ORM adoption
- Changes to application query code (stays raw `sqlite3`)
- `yoyo` CLI tooling or ops workflows
- Schema changes beyond what `migrate()` already handles
- Migration of any DB outside `compute_space`

---

## Acceptance Criteria Summary

| ID | Criterion | Testable |
|---|---|---|
| REQ-DEP-1 | `yoyo-migrations` in `pyproject.toml` | `uv run python -c "import yoyo"` |
| REQ-LAYOUT-3 | `0001` migration matches current schema | Schema diff vs `schema.sql` |
| REQ-STARTUP-1 | All 3 DB states handled correctly | Tests per REQ-TESTS-2/3/4 |
| REQ-STARTUP-2 | `migrate()` not called on managed DB | Test: call `init_db()` twice on managed DB |
| REQ-STARTUP-3 | Legacy DB onboarded without re-running `0001` SQL | Test: data preserved after onboarding |
| REQ-STARTUP-4 | Legacy onboarding is retry-safe | Test: interrupt + re-run leaves DB intact |
| REQ-SCHEMA-1 | `schema.sql` not executed at startup | Read `init_db()` — no `executescript` call |
| REQ-TESTS-1–4 | All migration tests pass | `uv run pytest compute_space/tests/test_migrations.py` |
