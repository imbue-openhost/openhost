# Review Feedback — Pure-Replay Refactor + 0002_dummy

## Assessment: PASS

## Summary

`builder.py` modules and the `seed_at` protocol are fully excised. Snapshots are now self-contained — each `at_NNNN.sql` carries application schema, application data, yoyo internal DDL, and persisted `_yoyo_migration` + `_yoyo_version` rows — so replay is pure: load → hand to yoyo → dump. `0002_dummy.py` is a genuine yoyo module (`steps: list[step] = []`), and the two `at_0002.sql` files differ from their `at_0001.sql` counterparts only in the header line and the appended tracking row, exactly as the no-op contract requires. Parametrised matrix produces exactly the two advertised cases; full suite + pre-commit + `regenerate.py --check` all green.

## Refactor Verification (b644f43)

1. **Builders gone.** `Glob compute_space/tests/fixtures/migrations/**/builder.py` → no files. Both scenario dirs contain only `at_*.sql`.
2. **No `seed_at` / `seed_fn` / `load_scenario_builder` / `scenario_seed_fn` in code.** Grep returns hits only in `docs/db_migrations/implementation-notes.md:134,157,168` (historical prose) and `docs/db_migrations/review-feedback-snapshot.md:57` (prior review). Zero hits in `.py`.
3. **Dumps include `_yoyo_migration` rows.** `scenario_refresh_tokens/at_0001.sql:113`: `INSERT INTO "_yoyo_migration" ... '0001_initial', '2025-01-01 00:00:00'`. `scenario_port_mappings/at_0001.sql:117` same. Both also persist `_yoyo_version` (`at_0001.sql:114` / `at_0001.sql:118`). `_yoyo_log` and `yoyo_lock` DDL kept (needed so yoyo can write there at apply time) but their rows stripped — rationale documented at `_snapshot_harness.py:13-21, 41-47`.
4. **Regenerate is pure replay.** `regenerate.py:60-74` `_replay()`: load `at_<from>.sql` into a tempdir sqlite file, call `apply_pending(db_path, up_to_inclusive=to_id)`, dump. `apply_pending` (`_snapshot_harness.py:83-100`) calls `backend.to_apply(migrations)` — yoyo's own tracking rows in the loaded snapshot determine what's pending. CLI flags `--scenario`, `--from`, `--to`, `--check` at `regenerate.py:98-103`. Missing-snapshots error at `regenerate.py:82-84`: clear message naming the scenario.
5. **No virtual `empty` state.** `_snapshot_cases()` at `test_yoyo_migrations.py:186-194` iterates `present_snapshot_ids(scenario_dir)` and pairs only real files. Scenarios with a single snapshot contribute zero cases, matching the docstring at `test_yoyo_migrations.py:179-183`.
6. **`TestYoyoDispatch` rationale comment present.** `test_yoyo_migrations.py:58-66` docstring explicitly: "Fresh -> 0001 schema parity is validated here, not in the snapshot suite. Snapshots additionally include scenario-specific data that a migration alone does not produce, so snapshots can't substitute for the fresh-DB schema check."

## 0002_dummy Verification (5d3e836)

1. `compute_space/compute_space/db/migrations/0002_dummy.py:12` — `steps: list[step] = []`. Docstring at lines 1-8 explains the "promote next integer for real changes, leave this alone" policy so the snapshot chain stays stable.
2. `scenario_refresh_tokens/at_0002.sql` and `scenario_port_mappings/at_0002.sql` both present.
3. **Diff `at_0001.sql` vs `at_0002.sql`**: two hunks per scenario, both trivial.
   - Header: `-- After migration: 0001_initial` → `0002_dummy` (line 3).
   - Appended row: `INSERT INTO "_yoyo_migration" ... '25d06ca3...', '0002_dummy', '2025-01-01 00:00:00'` (refresh_tokens line 113; port_mappings line 117). Sort-key ordering (`_snapshot_harness.py:121-123`) places the new hash `25d06ca3...` before the existing `3ef54af1...`, so the 0001 row shifts down one line.
   - Schema, application data, `_yoyo_version`, and `_yoyo_log` / `yoyo_lock` DDL all byte-identical.

## Test Matrix

```
pytest --collect-only compute_space/tests/test_yoyo_migrations.py::TestSnapshot
  test_migration_produces_expected_snapshot[scenario_port_mappings-0001->0002]
  test_migration_produces_expected_snapshot[scenario_refresh_tokens-0001->0002]
```

Exactly the two cases the task spec asked for.

## Pit Traps Checked

- **Yoyo tracking schema match**: `at_0001.sql:97-102` declares `_yoyo_migration` with columns `migration_hash`, `migration_id`, `applied_at_utc` — matches yoyo's actual CREATE TABLE (same DDL string is copied verbatim from `sqlite_master.sql` at dump time, so it's tautologically correct). Same for `_yoyo_version` at lines 103-106. Verified: passing tests include `TestYoyoDispatch::test_fresh_db_applies_all_migrations` which lets yoyo create the table fresh, then a later run's `to_apply()` reads it back successfully.
- **Loading order (CREATE before INSERT)**: `dump_application_db` at `_snapshot_harness.py:205-218` emits all table DDL (app + yoyo) before any INSERT. Confirmed in `at_0001.sql`: CREATE `_yoyo_migration` at line 97, INSERT at line 113.
- **Right table**: `YOYO_TABLES_WITH_ROWS = ("_yoyo_migration", "_yoyo_version")` at `_snapshot_harness.py:47`. These are the two tables `to_apply()` consults — `_yoyo_log` is audit-only, yoyo does not read it to determine pending migrations. Empirically confirmed by four passing dispatch tests + two passing snapshot tests.
- **Non-determinism**: `_CANONICAL_TS_COLUMNS` at `_snapshot_harness.py:51` covers `applied_at_utc`, `installed_at_utc`, `created_at_utc`, `ctime` — every timestamp column across yoyo's internal schema. `_canonicalize_row` at `_snapshot_harness.py:130-131` substitutes `CANONICAL_TS` at dump time. `_yoyo_log` rows (UUIDs, hostnames) and `yoyo_lock` rows (pid) are stripped entirely, not canonicalised — `dump_application_db` at `_snapshot_harness.py:215-217` emits rows only for tables in `YOYO_TABLES_WITH_ROWS`. `regenerate.py --check` exit 0 proves end-to-end determinism.
- **`--check` idempotency**: `uv run python compute_space/tests/fixtures/migrations/regenerate.py --check` → exit 0, prints `scenario_port_mappings: ok (0002_dummy -> 0002_dummy)` / `scenario_refresh_tokens: ok (0002_dummy -> 0002_dummy)`.

## Runtime Checks

- `uv run --group dev pytest compute_space/tests/ -v` → **125 passed, 30 skipped**, 2 warnings (pre-existing yoyo `DeprecationWarning`). Includes 4 `TestYoyoDispatch` + 2 `TestSnapshot` cases.
- `uv run --group dev pytest` (full lightweight) → **200 passed, 30 skipped**.
- `pre-commit run --all-files` → ruff auto-fix, ruff format, mypy, secret-detect all Passed.
- `python compute_space/tests/fixtures/migrations/regenerate.py --check` → exit 0.
- `git log --oneline main..HEAD` → 8 commits (`556536a`, `73459ba`, `4a49c5e`, `bca1f75`, `6d2258c`, `80df8c6`, `b644f43`, `5d3e836`), linear.

## Critical

None.

## Minor

- **Default `--from` is the latest snapshot**, so `regenerate.py --check` with no args replays `at_0002.sql → apply_pending → at_0002.sql` (yoyo sees nothing pending) — a canonicalisation round-trip, not a true 0001→0002 replay check. The 0001→0002 replay is exercised by `TestSnapshot` in the pytest suite, so CI coverage is intact, but a reader who mentally models `regenerate.py --check` as "re-derive every snapshot from the first" will be surprised. Consider either (a) docstring tweak at `regenerate.py:13-17` clarifying that the default check only re-derives the newest, or (b) a `--all` / `--from-first` flag that chains through every snapshot.
- **Migration hash is baked into snapshots**. `at_0002.sql:113` hard-codes `25d06ca362a6cbcfa89d5e102a1660c576d0da583e97bd39a387aa08633511b4`. If anyone edits `0002_dummy.py` — even its docstring — yoyo's hash changes, snapshots break, and the fix is a regenerate run. That's the intended behaviour (the docstring at `0002_dummy.py:5-7` tells future contributors to leave this file alone), but worth calling out for anyone tempted to "just clean up" the module.
- **`_snapshot_harness.py` import hack.** `regenerate.py:42-57` mutates `sys.path` so `_snapshot_harness` loads as a top-level module rather than `compute_space.tests._snapshot_harness`. Documented at `_snapshot_harness.py:33-36`. Works, but means the module has dual identity (package member for tests, script import for the CLI). A future refactor that moves it to `tests/helpers/` or installs it under a proper package path would remove the hack. Not a blocker.
- **`resolve_migration_id`** at `_snapshot_harness.py:70-80` is called by the CLI but the `--from` / `--to` prefix-match feature is undocumented in `regenerate.py`'s `--help` (the argparse help text just says "Starting migration id or prefix" at `regenerate.py:100-101`, which is fine, but the module docstring at `regenerate.py:13-14` only mentions `FROM_ID` / `TO_ID` as full ids). One-line clarification would help.

## Suggestions

- Add a CI step (or pre-commit hook) running `regenerate.py --check` — cheap, catches snapshot rot before it reaches review.
- Consider teaching `regenerate.py` a `--from-earliest` mode that chains `at_0001 → at_0002 → ... → at_N` so the check also validates the full history, not just the newest step.

## Recommendation

**Ship.** Refactor achieves the goal (self-contained snapshots, pure replay, no per-scenario Python seed code), `0002_dummy` demonstrates the extension path cleanly, and the test + CI surface is green. The four minor notes above are all polish.
