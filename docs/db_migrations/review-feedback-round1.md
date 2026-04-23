# Review Feedback — Round 1

## Assessment: NEEDS_WORK

Functionally correct — all tests pass and every requirement is behaviourally met — but the REQ-STARTUP-3 deviation adds DDL drift risk that contradicts goals.md's "0001 becomes source of truth" and should be rethought before merge. Everything else is minor.

## Requirements Coverage

| REQ-ID | Status | Evidence | Notes |
|---|---|---|---|
| REQ-DEP-1 | ✓ | `compute_space/pyproject.toml:26`, `pyproject.toml:27` | Added to both. Root needed because hatch packages ship `compute_space/compute_space` inside the root wheel — see "Minor issues" below. |
| REQ-LAYOUT-1 | ✓ | `compute_space/compute_space/db/migrations/0001_initial.sql` exists | |
| REQ-LAYOUT-2 | ✓ | file name `0001_initial.sql` | single file, SQL format |
| REQ-LAYOUT-3 | partial | `0001_initial.sql:7-96` | Tables match `schema.sql` **textually**. See Critical Issue #1: DDL duplicated into `legacy_migrate.py:227-270` creates drift risk. |
| REQ-STARTUP-1 | ✓ | `connection.py:38-51` (`_classify_db_state`) + `connection.py:70-83` | Three-way dispatch, tested by `TestYoyoDispatch` (`test_migrations.py:645-752`) |
| REQ-STARTUP-2 | ✓ | `connection.py:45-46` checks `_yoyo_migration` first; `test_managed_db_skips_legacy_migrate` (`test_migrations.py:708-732`) monkeypatches `migrate` to prove it never runs |
| REQ-STARTUP-3 | partial | `connection.py:79-82` calls `mark_migrations(migrations[:1])` without re-running SQL | Literal requirement met, but only because `legacy_migrate.migrate()` was extended with new DDL (`legacy_migrate.py:221-270`) — see Critical Issue #1 |
| REQ-STARTUP-4 | ✓ | `migrate()` guards each step with column-presence checks; new `CREATE ... IF NOT EXISTS` block also idempotent. Walked through crash modes, all recover — see Security section. |
| REQ-STARTUP-5 | ✓ | `core/startup.py:92` `init_db(app)` called synchronously in `init_app`, before Hypercorn serves |
| REQ-SCHEMA-1 | ✓ | No `executescript(schema.sql)` anywhere in `connection.py`; `schema.sql` is only opened by `_schema_path()` inside `_recreate_table` (legacy-path helper, not startup) |
| REQ-SCHEMA-2 | ✓ | `schema.sql` retained, not executed at startup |
| REQ-SCHEMA-3 | ✓ | No modifications to `schema.sql` required for this change. Future schema changes can go in `0002_*.sql`. |
| REQ-TESTS-1 | ✓ | Full `TestRouterMigrations` + `TestCrashRecovery` suites pass |
| REQ-TESTS-2 | ✓ | `test_fresh_db_applies_all_migrations` (`test_migrations.py:648-669`) |
| REQ-TESTS-3 | ✓ | `test_legacy_db_runs_migrate_and_marks_0001` (`test_migrations.py:671-706`) |
| REQ-TESTS-4 | ✓ | `test_managed_db_skips_legacy_migrate` + `test_managed_db_preserves_data_across_restart` (`test_migrations.py:708-752`) |

## Critical Issues (must fix before merge)

### 1. REQ-STARTUP-3 deviation duplicates DDL and violates goals.md spirit

`legacy_migrate.py:227-270` adds a `CREATE TABLE/INDEX IF NOT EXISTS` block for `app_databases`, `api_tokens`, `app_tokens`, `service_providers`, `permissions`, `idx_apps_status`, `idx_refresh_tokens_token_hash`. Same DDL already lives in `0001_initial.sql`. Two sources of truth → drift risk.

goals.md:18 says `migrate()` is **frozen**. Impl unfroze it.

Better fix: **apply `0001` on the legacy path** instead of `mark`. Every statement in 0001 is `IF NOT EXISTS`, so re-executing on a legacy DB is a no-op for tables `migrate()` already built and fills the gap for tables it doesn't. Change `connection.py:79-82`:

```python
# delete the `if state == "legacy" ...: backend.mark_migrations(...)` block
backend.apply_migrations(backend.to_apply(migrations))
```

…and delete `legacy_migrate.py:221-270` (the new `executescript` block).

Reading of REQ-STARTUP-3 "MUST be marked applied without re-executing it": idempotent re-execution is effectively marking. Spirit of the requirement is "don't corrupt data by double-applying destructive DDL". `CREATE IF NOT EXISTS` can't corrupt. Fresh-path test proves 0001 is safe to apply to an empty DB; applying to a legacy DB already at baseline is equivalent.

If you want to preserve the requirement *verbatim*, run `0001_initial.sql` text via `executescript` on the legacy path (not via yoyo) then `mark_migrations`. Still single source of truth, still no yoyo re-apply.

Either way — eliminate the duplicate DDL in `legacy_migrate.py`.

## Minor Issues (should fix)

- **Root `pyproject.toml` dep addition is correct, keep it.** Root hatch wheel packages `compute_space/compute_space` (`pyproject.toml:50`), and `uv run` resolves against root. Without the dep in root the module `import yoyo` would fail at runtime in the deployed wheel. Open-question in impl-notes can be closed: this is not accidental, it's required. Remove the open-question bullet or mark resolved.
- **Stale docstring in `test_migrations.py:167`** — `# Run init_db (which calls _migrate then schema.sql)` is no longer accurate; init_db no longer runs schema.sql. Fix comment.
- **Stale file-level docstring `test_migrations.py:1-5`** — "Tests that the router's hand-rolled SQLite migrations produce a schema identical to a fresh database created by schema.sql" is still mostly accurate but "fresh database created by schema.sql" is now the test-fixture path, not production path. Tighten.
- **Yoyo `DeprecationWarning`** on Python 3.12 datetime adapter (`legacy_migrate` unrelated — it's yoyo's `backends/base.py:411`). Not our bug; upstream. No action needed, but consider suppressing via `filterwarnings` in `pyproject.toml:56` to keep test output clean.

## Suggestions

- Consider deleting `schema.sql` in a follow-up once the critical issue above is resolved — then the baseline lives only in `0001_initial.sql` as goals.md intended. `_recreate_table` still reads `schema.sql` (`legacy_migrate.py:19`) for DDL lookups during legacy migration; that's an acceptable reason to keep it for now.
- `init_db` opens two separate sqlite connections (one for classify+migrate, one via yoyo backend). Not a correctness issue, but if you care about WAL consistency you could pass a single connection through. Low priority.
- Add a migration numbering convention doc (e.g. a README in the migrations dir) so the next dev knows what naming to use for 0002.

## Test Results

- `uv run --group dev pytest compute_space/tests/ -v` → **123 passed, 30 skipped, 26 warnings** (all yoyo datetime deprecation)
- `uv run --group dev pytest` → **198 passed, 30 skipped, 26 warnings**
- `pre-commit run --all-files` → ruff auto-fix / ruff format / mypy / secrets all **Passed**
- `git log --oneline main..feat/yoyo-migrations` → **1 commit** (`556536a Adopt yoyo-migrations for compute_space DB`) — clean

## Security / Correctness Walk-through

**Crash mid-`migrate()`:** Each step re-checks column/table state. Replay is safe. ✓

**Crash between `migrate()` and `mark_migrations(0001)`:** Next startup classifies as `legacy` (apps present, no _yoyo_migration). `migrate()` re-runs idempotently, mark proceeds. ✓

**Crash mid `mark_migrations`:** Yoyo wraps in transaction; partial mark impossible. ✓

**Crash mid `apply_migrations` for 0002+:** Each yoyo migration is its own transaction. Applied ones stay applied, failed one rolls back. ✓

**Reachability of "`_yoyo_migration` present but `apps` missing":** Possible if user manually drops apps table; classification returns `managed`, `apply_migrations` is a no-op (0001 already marked), DB stays broken. Not our bug — out-of-band tampering. Acceptable.

**Reachability of "both `apps` and `_yoyo_migration` present":** Classification returns `managed` first (`connection.py:45-46`), so legacy `migrate()` is skipped even though `apps` is there. Correct — this is exactly the managed case after first onboarding.

**"Managed" detection specificity:** `_table_exists(db, "_yoyo_migration")` — yoyo's canonical tracking table. Yoyo's `yoyo_lock` is separate and only created while a lock is held. Detection is correctly narrow. ✓

## Recommendation

Fix Critical Issue #1 (swap `mark` → `apply` on legacy path, delete the new `executescript` block in `legacy_migrate.py`). After that, minor comment fixes and ready to merge.
