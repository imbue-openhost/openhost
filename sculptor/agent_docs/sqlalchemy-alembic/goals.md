# Goals: SQLAlchemy + Alembic Migration (compute_space)

## Problem

`compute_space` uses raw SQL strings and a hand-rolled migration system (`db/migrations.py`). As the schema grows, migrations have become increasingly painful to write, apply, and reason about. The ad-hoc migration logic (temp-table recreation, crash recovery) is fragile and hard to extend.

## Goals

1. **Schema as code.** Replace raw SQL schema definitions with SQLAlchemy ORM models. Tables, columns, and relationships are defined in Python — not scattered across `.sql` files and string literals.

2. **Structured query interface.** Replace raw SQL strings in all `compute_space` modules — including query sites (`apps.py`, `ports.py`, `permissions.py`, etc.) and startup schema logic (`startup.py`) — with SQLAlchemy ORM. Reduces risk of typos, SQL injection surface, and makes refactors safer. Uses SQLAlchemy's async API (`AsyncSession`, async engine) to remain compatible with the existing Quart/hypercorn async stack.

3. **Managed migrations via Alembic.** Future schema changes are expressed as Alembic migration scripts — versioned, reproducible, and reviewable. No more manual ALTER TABLE or table-recreation logic.

4. **Safe cutover from existing migration system.** On boot, if the DB has no Alembic version stamp, the frozen legacy `migrate()` runs to bring the DB to the baseline schema, then Alembic stamps it as the initial revision. From there on, all schema evolution — including on fresh DBs — goes through `alembic upgrade head`. No module other than Alembic creates or alters tables.

5. **No change to runtime behavior.** SQLite backend stays. Auth, proxying, container management, and all existing functionality remain unchanged.

## Out of Scope

- The `secrets` app and any other apps outside `compute_space`
- Switching from SQLite to another database engine
- Changes to the JWT auth system or HTTP routing layer
- Adding new schema features or business logic as part of this migration
