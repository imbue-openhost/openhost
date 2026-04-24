# Requirements: Versioned Migrations

Derived from `goals.md`. Scope: the openhost `compute_space` SQLite DB only. Version numbering convention used throughout: **v0** = legacy/unknown (no row or `version = 0`), **v1** = known baseline produced by running the existing `migrate()` one last time, **v2+** = numbered migration files.

## Schema Version Tracking

- **REQ-VER-1**: The DB MUST record its current schema version as a single integer scalar in a dedicated metadata table.
- **REQ-VER-2**: A DB in which the version metadata table does not exist, or in which the version row is absent or `0`, MUST be treated as v0 (legacy).
- **REQ-VER-3**: The runner MUST read the version exactly once per startup, before deciding what to apply.
- **REQ-VER-4**: If the recorded version is greater than the highest version known to the running code, the runner MUST raise a hard error at startup and refuse to serve traffic. No forward-compatibility is assumed.
- **REQ-VER-5**: No migration-history table is required at this stage.

## Legacy Bootstrap (v0 → v1)

- **REQ-LEG-1**: If the runner observes v0, it MUST invoke the existing `migrate()` function exactly once, then stamp the version table to `1`, then exit the legacy path permanently for this DB.
- **REQ-LEG-2**: The existing `migrate()` logic MUST NOT be rewritten; only a final step that creates the version metadata table (if missing) and writes `version = 1` may be added.
- **REQ-LEG-3**: Once a DB is at v1, the runner MUST NOT call `migrate()` again on subsequent startups.

## Migration Framework

- **REQ-MF-1**: A migration MUST be defined by subclassing a small base class with at minimum an `up(db)` method and an integer `version` attribute identifying the target version after the migration runs.
- **REQ-MF-3**: A ready-made subclass MUST be provided that runs a sibling `.sql` file as its `up` step, for pure-schema changes.
- **REQ-MF-4**: Each migration's `up` MUST be executed inside a single SQLite transaction. On any exception, the transaction MUST be rolled back and the recorded version MUST NOT be bumped.
- **REQ-MF-5**: After successful `up`, the runner MUST update the recorded version in the same transaction as the migration's DB changes, so that "migration applied" and "version bumped" are atomic.
- **REQ-MF-6**: Migration authors MAY perform non-transaction-safe operations when strictly necessary, but MUST document why in a comment; tests SHOULD flag such operations (see REQ-TEST-7).

## Registry & Ordering

- **REQ-REG-1**: Migrations MUST be registered in a hand-maintained Python list. Filesystem scanning MUST NOT be used for discovery.
- **REQ-REG-2**: Versions in the registry MUST be strictly increasing and contiguous starting at `2`. Startup MUST hard-error if any gap or duplicate version is detected.
- **REQ-REG-3**: Adding a new migration requires (a) writing the migration file under the migrations directory, and (b) appending an entry to the registry list.

## Runtime Behavior

- **REQ-RUN-1**: At startup, the runner MUST acquire an exclusive SQLite lock (e.g. `BEGIN EXCLUSIVE`) before applying any migrations, so that concurrent processes serialize cleanly and only one applies migrations.
- **REQ-RUN-2**: After acquiring the lock, the runner MUST re-read the recorded version, then apply in order each registered migration whose version is strictly greater than the recorded version, up to the highest registered version.
- **REQ-RUN-3**: If the recorded version already equals the highest registered version, the runner MUST apply no migrations and proceed.
- **REQ-RUN-4**: Each applied migration MUST be logged at `INFO` level including the source version, target version, and duration.
- **REQ-RUN-5**: If a migration raises, the runner MUST log the error at `ERROR` level including the failing version, let the transaction roll back, and propagate the exception so the process exits. Re-running the process MUST cleanly retry from the last successful version.
- **REQ-RUN-6**: No CLI, flag, or ops tool for applying migrations manually is required. Migration application MUST be transparent on startup.

## Fresh Database Initialization

- **REQ-INIT-1**: `compute_space/compute_space/db/schema.sql` MUST remain the canonical "current full schema" and MUST be used to initialize brand-new DBs in one step.
- **REQ-INIT-2**: When the runner initializes a DB from `schema.sql`, it MUST stamp the version metadata to the highest registered version, so migrations are not replayed on an already-current DB.
- **REQ-INIT-3**: Developers are responsible for keeping `schema.sql` in sync with the cumulative effect of all migrations when they add a migration.

## Testing

- **REQ-TEST-1**: For each registered migration version N (N ≥ 2), there MUST be a snapshot test that: starts from an empty DB, applies all registered migrations up to and including version N, and asserts the resulting schema and data match a checked-in snapshot file.
- **REQ-TEST-2**: The snapshot MUST cover both schema (e.g. normalized `sqlite_master` SQL and `PRAGMA table_info` output for every table) and data (canonical ordered dump of every row in every seeded table).
- **REQ-TEST-3**: A single shared seed dataset MUST be inserted into the empty DB before migrations run, exercising every table that existed at the earliest version covered by snapshots. The same seed is used for all snapshot tests.
- **REQ-TEST-4**: A sanity test MUST verify that applying all registered migrations against an empty DB produces a DB equivalent (same normalized schema) to one initialized from `schema.sql` alone.
- **REQ-TEST-5**: A legacy-bootstrap test MUST verify that running `migrate()` against a representative pre-v0 fixture, followed by the runner's normal startup, yields a DB at the highest registered version whose schema and data match the corresponding snapshot.
- **REQ-TEST-6**: A concurrency test SHOULD verify that two simulated startups against the same DB do not both apply migrations (one waits, observes the new version, applies nothing).
- **REQ-TEST-7**: Migration tests SHOULD detect and warn on operations known to break SQLite transactional rollback (e.g. `PRAGMA foreign_keys` toggles inside a tx). Best-effort: a simple heuristic scan is acceptable; full static analysis is not required.
- **REQ-TEST-8**: All migration tests MUST run in the lightweight `pytest` suite (i.e. without `--run-docker`). `sqlite3` is the only system dependency required.

## Out of Scope

- ORM / SQLAlchemy / Alembic adoption.
- Migrating apps under `apps/` (e.g. `dau_tracker`, `partaay`).
- Migration-history table (per-migration row with timestamp).
- CLI tooling for inspecting or applying migrations.
- Rewriting the existing `migrate()` body.
- Forward-compatibility with newer DB versions than the code knows about.
- Automatic generation of `schema.sql` from migrations.
- Branching / merging migration chains.
- Non-SQLite backends.
- Rollback / reverse migrations. The system is forward-only; there is no `down()` method on the base class.

## Acceptance Criteria Summary

| ID        | Acceptance check |
|-----------|------------------|
| REQ-VER-1 | Version metadata table exists and contains a single integer version row. |
| REQ-VER-4 | Starting the app against a DB whose recorded version exceeds the code's highest registered version aborts with a clear error. |
| REQ-LEG-1 | Starting against a pre-v0 DB runs `migrate()` once, stamps v1, and never runs `migrate()` again. |
| REQ-LEG-2 | Diff of `migrate()` pre-change vs post-change shows only the added version-stamping step. |
| REQ-MF-1  | A new migration can be defined in ~10 lines by subclassing the base class. |
| REQ-MF-3  | A schema-only migration can be written as a `.sql` file + one-line registry entry using the provided subclass. |
| REQ-MF-4  | Injecting a failing statement into a migration leaves the DB and version unchanged after startup aborts. |
| REQ-REG-2 | Registry with a gap or duplicate causes startup to hard-error. |
| REQ-RUN-1 | Two processes racing against one DB do not both run migrations (validated by REQ-TEST-6). |
| REQ-RUN-4 | Log output from a successful upgrade shows one INFO line per applied migration with version and duration. |
| REQ-INIT-1 | Initializing an empty file from `schema.sql` yields a DB stamped at the latest registered version with no migrations run. |
| REQ-INIT-2 | After fresh init, no migration entries execute on the next startup. |
| REQ-TEST-1 | For every registered migration, a checked-in snapshot file exists and its test passes. |
| REQ-TEST-4 | Sanity test comparing migrations-from-empty vs `schema.sql` passes. |
| REQ-TEST-5 | Legacy-bootstrap fixture test ends in a DB byte-equivalent (per snapshot normalization) to the latest-version snapshot. |
| REQ-TEST-8 | `uv run --group dev pytest` (no `--run-docker`) runs all migration tests. |
