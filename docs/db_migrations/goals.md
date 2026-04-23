# Goal: Structured DB Migration System

## Problem

Current migration code in `compute_space/compute_space/db/migrations.py` is imperative and fragile:
- Guesses current DB state by inspecting column presence rather than tracking schema version
- Each migration is hard to reason about in isolation
- Adding future migrations requires modifying a single growing function
- No structured record of what migrations have been applied

## Goal

Adopt **yoyo-migrations** as a lightweight, structured migration framework. Future migrations are written as versioned SQL files with a clear apply order. The DB itself records which migrations have run, eliminating guesswork about current state.

## Scope

- `compute_space` component only (the SQLite DB it manages)
- Existing `migrate()` logic is correct; freeze it as migration `0001` so live DBs upgrade cleanly
- No ORM adoption — keep raw `sqlite3` usage in application code

## Startup Behavior

Three DB states must be handled by the startup path:

1. **Legacy DB** (no yoyo tracking table, has existing `apps` table): run existing `migrate()` once to bring schema current, then mark migration `0001` as applied via yoyo. Never call `migrate()` again on this DB.
2. **Fresh DB** (no tables at all): yoyo applies all migrations from scratch. Migration `0001` contains the full current schema.
3. **Yoyo-managed DB** (tracking table present): yoyo applies any pending migrations.

After onboarding, `migrate()` and `schema.sql` are no longer consulted.

## Non-Goals

- Migrating app code to SQLAlchemy or any ORM
- Down/rollback migrations
- Schema changes themselves — this is infrastructure work only
- Preserving `schema.sql` long-term (migration `0001` becomes the source of truth)
