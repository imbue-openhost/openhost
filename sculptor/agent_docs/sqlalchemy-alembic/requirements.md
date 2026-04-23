# Requirements: SQLAlchemy + Alembic Migration (compute_space)

_Derived from [goals.md](./goals.md)._

---

## REQ-DEPS — Dependencies

| ID | Requirement |
|----|-------------|
| REQ-DEPS-1 | `compute_space` MUST add `sqlalchemy[asyncio]` and `alembic` as runtime dependencies in its `pyproject.toml`. |
| REQ-DEPS-2 | An async-compatible SQLite driver MUST be added (e.g. `aiosqlite`). |
| REQ-DEPS-3 | No existing runtime dependency MAY be removed as part of this change unless it is superseded by SQLAlchemy (e.g. direct `sqlite3` imports inside `compute_space`). |

---

## REQ-SCHEMA — ORM Model Definitions

| ID | Requirement |
|----|-------------|
| REQ-SCHEMA-1 | All tables currently defined in `compute_space/db/schema.sql` MUST be re-expressed as SQLAlchemy `DeclarativeBase` ORM models in Python. |
| REQ-SCHEMA-2 | ORM models MUST use SQLAlchemy's async-compatible mapped column syntax (`mapped_column`, `Mapped`). |
| REQ-SCHEMA-3 | Column types, nullability, defaults, and constraints MUST exactly match the existing schema (no schema changes bundled in this migration). |
| REQ-SCHEMA-4 | The `schema.sql` file MUST be removed or explicitly marked as obsolete once ORM models are the authoritative schema source. |
| REQ-SCHEMA-5 | ORM models MUST be importable from a single well-known module path within `compute_space.db`. |

---

## REQ-MIGRATE — Alembic Setup

| ID | Requirement |
|----|-------------|
| REQ-MIGRATE-1 | An Alembic environment MUST be initialized within `compute_space/` with `alembic.ini` and a `migrations/` (or `alembic/`) directory. |
| REQ-MIGRATE-2 | Alembic MUST be configured to use the async SQLAlchemy engine targeting the same SQLite file path as the current system. |
| REQ-MIGRATE-3 | Alembic's `env.py` MUST import ORM model metadata so that `alembic revision --autogenerate` detects schema changes correctly. |
| REQ-MIGRATE-4 | An initial "baseline" Alembic migration MUST exist that creates the full current schema from an empty database. This migration is the single source of truth for fresh installs. |
| REQ-MIGRATE-5 | The developer workflow for schema changes MUST be: edit ORM models → run `alembic revision --autogenerate -m "<description>"` → review and commit generated script. |
| REQ-MIGRATE-6 | Generated migration scripts MUST be committed to version control. |

---

## REQ-CUTOVER — Legacy Migration Cutover

| ID | Requirement |
|----|-------------|
| REQ-CUTOVER-1 | On boot, if the database has no Alembic version stamp (`alembic_version` table absent or empty), the legacy `migrate()` function MUST run to bring the existing DB to the baseline schema. |
| REQ-CUTOVER-2 | After legacy `migrate()` completes, the system MUST stamp the DB with the baseline Alembic revision ID (equivalent to `alembic stamp <base_revision>`). |
| REQ-CUTOVER-3 | For fresh (empty) databases, `migrate()` MUST NOT run; instead `alembic upgrade head` applies the full schema from scratch. |
| REQ-CUTOVER-4 | The legacy `migrate()` function MUST be frozen: no new migration logic may be added to it after this work is complete. |
| REQ-CUTOVER-5 | A automated test MUST verify that the schema produced by the legacy `migrate()` + stamp path is structurally equivalent to the schema produced by `alembic upgrade head` from an empty DB. This MUST compare table definitions, columns, types, and constraints. |

---

## REQ-BOOT — Boot-time Behavior

| ID | Requirement |
|----|-------------|
| REQ-BOOT-1 | On every boot, `compute_space` MUST run `alembic upgrade head` (or its programmatic equivalent) before accepting requests. |
| REQ-BOOT-2 | If migration fails on boot, the application MUST fail to start with a clear error message. |
| REQ-BOOT-3 | No module other than the Alembic migration runner MAY issue `CREATE TABLE` or `ALTER TABLE` statements at runtime. The `startup.py` schema-creation logic MUST be removed. |

---

## REQ-QUERY — Query Layer

| ID | Requirement |
|----|-------------|
| REQ-QUERY-1 | All raw SQL strings (`execute("SELECT ...")`, f-string queries, etc.) in `compute_space` MUST be replaced with SQLAlchemy ORM query expressions. |
| REQ-QUERY-2 | This covers at minimum: `core/apps.py`, `core/ports.py`, `core/permissions.py`, `core/startup.py`, and any other `compute_space` module with direct SQL. |
| REQ-QUERY-3 | Database access MUST use `AsyncSession` throughout, compatible with Quart's async request lifecycle. |
| REQ-QUERY-4 | The existing per-request session lifecycle pattern (acquire session at request start, close on teardown) MUST be preserved. `get_db()` in `db/connection.py` MAY be adapted to return `AsyncSession`. |
| REQ-QUERY-5 | All writes MUST be wrapped in explicit transactions. No implicit auto-commit outside of session lifecycle boundaries. |
| REQ-QUERY-6 | WAL mode MUST remain enabled for the SQLite connection. |

---

## REQ-TEST — Testing

| ID | Requirement |
|----|-------------|
| REQ-TEST-1 | Existing tests MUST pass without modification at the SHA where the query layer migration is complete. |
| REQ-TEST-2 | The schema parity test described in REQ-CUTOVER-5 MUST be added as part of this work. |
| REQ-TEST-3 | Existing tests that use raw SQL against the DB MAY be left as-is initially; rewriting them to use SQLAlchemy is deferred to a follow-up. |
| REQ-TEST-4 | The committer MUST record the SHA at which CI first passes with all query layer changes complete, as a handoff marker for the follow-up test rewrite. |

---

## Out of Scope

- `apps/secrets/` and all apps outside `compute_space`
- Switching database backend from SQLite
- Changes to JWT auth, token logic, or HTTP routing
- New schema features or business logic
- Rewriting existing raw-SQL tests (deferred to follow-up after CI-passing SHA)
- Alembic `downgrade` support (not required)
- Connection pooling changes beyond what SQLAlchemy's async engine provides by default

---

## Acceptance Criteria Summary

| ID | Criterion | Verified by |
|----|-----------|-------------|
| REQ-DEPS-1/2 | SA + aiosqlite in dependencies | `pyproject.toml` diff |
| REQ-SCHEMA-1–4 | All tables as ORM models, `schema.sql` removed/obsolete | Code review |
| REQ-MIGRATE-4 | Baseline migration creates full schema from empty DB | Run `alembic upgrade head` on empty DB |
| REQ-CUTOVER-5 | Legacy path schema == alembic-from-scratch schema | Automated schema-diff test passes |
| REQ-BOOT-1–3 | Boot runs Alembic, no other table creation | Code review + integration test |
| REQ-QUERY-1–2 | No raw SQL strings remain in `compute_space` | `grep -r "execute(" compute_space/` returns zero query hits |
| REQ-QUERY-3 | All DB access via `AsyncSession` | Code review |
| REQ-TEST-1 | Existing tests pass unmodified | CI green at handoff SHA |
| REQ-TEST-4 | Handoff SHA recorded | Commit message / PR description |
