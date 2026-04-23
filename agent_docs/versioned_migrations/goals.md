# Goals: Versioned Migrations

## Problem

`compute_space/compute_space/db/migrations.py` is a single, monolithic `migrate()` function that infers the DB's schema state at runtime by probing tables and columns (`PRAGMA table_info`, checking which columns exist, etc.). Every schema change means editing this one file and adding yet another conditional block that guesses whether the migration already ran. The file grows without bound, state detection gets increasingly fragile, and there is no way to know what version a given DB is at or to test individual migration steps in isolation.

## What We Want

A conventional forward-migration system where each schema change is a numbered, isolated unit, and the DB itself records which version it's at.

Specifically:

- **Explicit schema version stored in the DB.** A single scalar version is recorded in a metadata table. The application reads it at startup instead of guessing state. No per-migration history table (can be added later if we ever want it).
- **One file per migration.** Each migration lives in its own numbered file (e.g. `0002_add_public_paths.py`). The runner applies them in order based on the version stored in the DB.
- **Pluggable migration implementations.** Migrations are Python, defined by subclassing a small base class. A trivial "run this `.sql` file" implementation is provided for pure-schema changes; data-transforming migrations (e.g. hashing plaintext tokens) write whatever Python they need.
- **Explicit registry.** Migrations are discovered via a hand-maintained Python list, not a filesystem scan. Adding a migration = write the file + append to the list.
- **Optional `down`.** Migrations may define a reverse step; not required. Useful mostly for manual test/dev iteration, since many real migrations lose or reshape data in ways that can't be cleanly reversed.
- **Concurrency-safe.** Two processes starting against the same DB at once must not both apply migrations. Serialize via SQLite locking (e.g. `BEGIN EXCLUSIVE`).
- **Transparent upgrade.** No CLI, no ops step. Each openhost version is compatible with exactly one DB schema version; migrations run automatically at startup. No partial / dev-controlled state.
- **`schema.sql` stays.** Still used for fast initialization of brand-new DBs (stamp to latest version, skip replaying migrations). A sanity-check test asserts that applying all migrations from empty produces a DB equivalent to one initialized from `schema.sql`.
- **Snapshot tests per migration.** For each version N, a golden test seeds a fresh DB with sample rows, applies migrations 1..N, and asserts both the schema and the resulting data match a checked-in snapshot. Catches unintended schema drift *and* broken data transforms.
- **Graceful upgrade of existing deployed DBs.** DBs that predate this system (no `schema_version` row, or `version = 0`) are upgraded by running the existing legacy `migrate()` code one last time, which brings them to v1 and stamps the version. From v1 onward, only numbered migrations run. Numbered migration files therefore begin at `0002_*.py`. DBs that already have `version >= 1` skip the legacy path entirely.

## Version Number Convention

- **v0**: legacy / unknown. Represented by a missing `schema_version` row or an explicit `version = 0`. Handled by a single last-ever pass through the existing `migrate()` function.
- **v1**: known baseline. State the existing `migrate()` produces, with the version row stamped as a final step of that same run. This is the point from which numbered migrations take over.
- **v2+**: one numbered migration file each.

## Why

- Today's migration file already has bugs-in-waiting: it re-reads column lists multiple times, recovers from mid-crash temp tables, and re-derives state that should have been recorded. Each new migration compounds this.
- With numbered migrations and a recorded version, the mental model collapses to "DB says vN; apply N+1, N+2, …". No detection, no guessing.
- Snapshot tests turn "does my migration work?" from a manual review exercise into an automated check that runs on every change.

## Scope

- Only the openhost `compute_space` SQLite DB (`compute_space/compute_space/db/`). Apps under `apps/` (e.g. `dau_tracker`, `partaay`) are out of scope.
- The existing `migrate()` logic is preserved verbatim as the bootstrap-to-v1 step, with one small addition: stamping `schema_version = 1` at the end of its run. We are not reworking any of its internals.

## Non-Goals

- No rewrite of the existing legacy migration code.
- No mandatory `down` migrations / full reversibility.
- No ORM / SQLAlchemy / Alembic adoption. Hand-rolled, since scope is small and a library would fit poorly (Alembic's autogen assumes SA metadata; full SA + Alembic is a much bigger refactor unrelated to the migration pain point).
- No migration-history table.
- No CLI / ops tooling.
- No cross-app migration framework.
- No DB-engine change (still SQLite).
