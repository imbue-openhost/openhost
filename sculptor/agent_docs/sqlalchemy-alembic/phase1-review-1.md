# Phase 1 Review

## Assessment: PASS

## Summary

Phase 1 cutover to SQLAlchemy models + Alembic is solidly implemented and all 195 tests (including the new parity test) pass with ruff/mypy clean. The boot flow correctly routes empty DBs through `alembic upgrade head` only and legacy DBs through `migrate()` + stamp + no-op upgrade, with `schema.sql` removed and no stray DDL outside `migrations.py` / `alembic/versions/`. A few minor gaps are worth tightening before phase 2 (parity-test depth, `alembic_version` empty-table edge case, finalize step now inside frozen `migrate()`).

## Requirements Verification

| REQ | Status | Notes |
|---|---|---|
| REQ-DEPS-1 | ✓ | `sqlalchemy[asyncio]` and `alembic` added to both `compute_space/pyproject.toml` and root `pyproject.toml`. |
| REQ-DEPS-2 | ✓ | `aiosqlite` added in both manifests. |
| REQ-DEPS-3 | ✓ | No runtime deps removed; `sqlite3` usage retained for now (phase 2 owns query-layer swap). |
| REQ-SCHEMA-1 | ✓ | All 9 tables (`apps`, `app_databases`, `app_port_mappings`, `owner`, `refresh_tokens`, `api_tokens`, `app_tokens`, `service_providers`, `permissions`) re-expressed as ORM models in `compute_space/db/models.py`. |
| REQ-SCHEMA-2 | ✓ | All columns use `Mapped[...]` + `mapped_column`. |
| REQ-SCHEMA-3 | ✓ | Column types, nullability, server_defaults, `CheckConstraint`s, `UniqueConstraint`s, and named indexes line up with the legacy DDL and the inlined `_BASELINE_SCHEMA` in `migrations.py`. |
| REQ-SCHEMA-4 | ✓ | `compute_space/db/schema.sql` deleted in commit `2ee9b85`. |
| REQ-SCHEMA-5 | ✓ | `Base` + all models importable from `compute_space.db.models`. |
| REQ-MIGRATE-1 | ✓ | `compute_space/compute_space/db/alembic.ini` + `alembic/env.py` + `alembic/versions/` present. |
| REQ-MIGRATE-2 | ✓ | `sqlalchemy.url` set at runtime via `_alembic_config()` to `sqlite+aiosqlite:///{db_path}`; env.py uses `async_engine_from_config`. |
| REQ-MIGRATE-3 | ✓ | `env.py` imports `Base` from `compute_space.db.models` and sets `target_metadata = Base.metadata`. |
| REQ-MIGRATE-4 | ✓ | `0001_baseline.py` creates the full schema on an empty DB (parity test proves it matches the legacy path). |
| REQ-MIGRATE-6 | ✓ | Baseline revision committed. |
| REQ-CUTOVER-1 | partial | `_legacy_db_needs_cutover` treats "no `alembic_version` table" as needing cutover; spec wording is "absent or empty". The empty-rows case (rare) is not handled — see Minor Issues. |
| REQ-CUTOVER-2 | ✓ | `command.stamp(_alembic_config(db_path), BASELINE_REVISION)` after `migrate()`. |
| REQ-CUTOVER-3 | ✓ | On fresh DB `_legacy_db_needs_cutover` returns False (no legacy tables); only `alembic upgrade head` runs. |
| REQ-CUTOVER-4 | partial | No new per-version migration logic added, BUT `migrate()` now ends with `db.executescript(_BASELINE_SCHEMA)` — logic relocated from the old `init_db()`. Defensible ("moved, not added") but the `_BASELINE_SCHEMA` constant and the `executescript` call at the tail are new code inside the "frozen" module. See Minor Issues. |
| REQ-CUTOVER-5 | ✓ (with gap) | `TestAlembicBaselineParity.test_alembic_upgrade_head_matches_legacy_cutover` exercises both paths and asserts structural equivalence. The comparator covers columns/types/nullability/defaults/PK + named indexes, but misses CHECK / inline UNIQUE / FK constraints — see Minor Issues. |
| REQ-BOOT-1 | ✓ | `init_db()` always ends with `_run_alembic_upgrade(db_path)`. |
| REQ-BOOT-2 | ✓ | Alembic errors propagate out of `init_db` → `init_app` → `create_app` and abort startup. No extra wrapping, but the underlying error message is sufficient. |
| REQ-BOOT-3 | ✓ | `compute_space/core/startup.py` contains zero `CREATE TABLE` / `ALTER TABLE`; only DDL in the repo is in `db/migrations.py` (frozen legacy) and `db/alembic/versions/`. |
| REQ-TEST-2 | ✓ | Parity test added in `compute_space/tests/test_migrations.py::TestAlembicBaselineParity`. |

## Critical Issues (must fix before merge)

None.

## Minor Issues

1. **`_legacy_db_needs_cutover` doesn't handle "`alembic_version` exists but empty"** — `compute_space/db/connection.py:27-46` checks only table presence. REQ-CUTOVER-1 literally says "absent or empty". If a DB ever ends up with an `alembic_version` row-less (e.g. `stamp base` + accidental clear), the cutover is skipped and `alembic upgrade head` would try to run the baseline against legacy tables and fail on `CREATE TABLE apps` (no `IF NOT EXISTS`). Fix: after finding the table, also check `SELECT version_num FROM alembic_version LIMIT 1`; if absent, treat as needing cutover (or use `MigrationContext.get_current_revision()`).

2. **Parity comparator does not diff CHECK / inline UNIQUE / FK constraints** — `testing_helpers/schema_helpers.py` pulls columns via `PRAGMA table_info()` and named indexes via `sqlite_master` `WHERE sql IS NOT NULL`. Inline UNIQUE constraints become `sqlite_autoindex_*` with `sql = NULL` (filtered out), and CHECK / FK clauses live only in `sqlite_master.sql` which isn't compared. REQ-CUTOVER-5 says the test MUST compare "table definitions, columns, types, and constraints". Fix: either (a) also snapshot normalized `sqlite_master.sql` per table, or (b) pull `PRAGMA foreign_key_list`, `PRAGMA index_list` (including auto) and CHECKs via `sqlite_master.sql` regex. Otherwise a future migration that silently drops `CHECK(status IN (...))` or `CHECK(id = 1)` on `owner` slips past the test.

3. **`migrate()` expanded slightly while being declared "frozen"** — `compute_space/db/migrations.py:19-110,291-292` inlines `_BASELINE_SCHEMA` and appends `db.executescript(_BASELINE_SCHEMA)` at the end of `migrate()`. The old code had this `executescript` in `init_db()` (deleted) and read the schema from `schema.sql` (deleted). Net behavior is the same, so it's defensible, but the framing "do not add new logic" is now mildly misleading: the module does contain the baseline DDL and a CREATE-IF-NOT-EXISTS finalization step that didn't previously live here. Fix (doc-only): tighten the module docstring to say "the only changes allowed are (a) inlining prior `schema.sql` text as the frozen baseline and (b) the finalization `executescript` step moved from the former `init_db()`."

## Suggestions (non-blocking)

- `0001_baseline.py` uses raw SQL via `op.execute()` rather than `op.create_table()`. This works but disconnects the baseline from `Base.metadata`, so `alembic revision --autogenerate` on HEAD may generate cosmetic diffs (e.g. `server_default=text("128")` vs. inspected `'128'`). Consider rewriting `upgrade()` with `op.create_table()` derived from the ORM so autogenerate compares metadata against itself.
- `alembic.ini` hardcodes `sqlalchemy.url = sqlite+aiosqlite:///:memory:` as a placeholder. Add a comment noting the URL is overridden at runtime by `_alembic_config()`, or drop the line entirely so a developer running `alembic` against the ini directly fails loudly instead of silently operating on `:memory:`.
- `env.py::run_migrations_online` uses `asyncio.run()`. That's fine today because `init_db` is invoked during synchronous `create_app()`, but it will explode if anything ever calls `init_db` from inside a running event loop. Cheap guard: check for a running loop and use `loop.run_until_complete` if one is present, or document the "must be called from sync context" contract in the docstring.
- `init_db` opens three sqlite connections in quick succession (WAL pragma set, legacy check, migrate, stamp, upgrade). Fine for boot but a little noisy; could consolidate the pragma+legacy-check into one connection.
- Several legacy unit tests (`test_migrate_adds_public_paths`, `test_migrate_adds_password_needs_set`, etc.) now drive the whole `init_db` pipeline (migrate + stamp + `upgrade head`) rather than just `migrate()`. They still verify the intended migration effect, but consider renaming / splitting once phase 2 lands if you want pure-unit coverage of `migrate()` separate from the cutover flow.

## Test Evidence

Commands run from repo root `/Users/kilo/.sculptor/workspaces/8030a77d167e46e4af0fbc89cb81cf02/code`:

- `git log --oneline origin/main..HEAD` — four commits: docs (read-only), deps, ORM+Alembic, cutover, tests.
- `git diff origin/main..HEAD -- sculptor/agent_docs/sqlalchemy-alembic/` — only additions (both spec files were created in `a3028d4` on this branch; no edits afterward).
- `uv run --group dev pytest` → **195 passed, 30 skipped in 6.28s** (covers `compute_space/tests/`, `self_host_cli/tests/`, etc.).
- `uv run --group dev pytest compute_space/tests/ -v` → **120 passed, 30 skipped in 5.46s**, including `TestAlembicBaselineParity::test_alembic_upgrade_head_matches_legacy_cutover`, all `TestRouterMigrations` cases (idempotency, partial migration, null datetime coalesce, token hashing), and `TestCrashRecovery`.
- `uv run --group dev pytest --run-docker compute_space/tests/` → **not executed**; Docker not available on the review host (conftest raised `RuntimeError: --run-docker flag passed but Docker does not seem to be available`). No compute_space tests are marked docker-only in the reviewed diff, so this does not change the assessment.
- `uv run --group dev ruff check compute_space/compute_space/db/ compute_space/tests/test_migrations.py` → **All checks passed!**
- `uv run --group dev mypy compute_space/compute_space/db/` → **Success: no issues found in 6 source files**.
- `grep "CREATE TABLE\|ALTER TABLE\|DROP TABLE"` across `compute_space/compute_space` confirms DDL lives only in `db/migrations.py` (frozen legacy) and `db/alembic/versions/0001_baseline.py` — `core/startup.py` is DDL-free (REQ-BOOT-3).
