-- v7: widen the archive_backend.backend CHECK constraint to admit
-- 'disabled' as a valid state, and shift the column default from
-- 'local' to 'disabled' for fresh inserts.
--
-- Why: pre-v7, brand-new zones came up with backend='local' silently,
-- writing app_archive bytes to local disk under a "fallback" pretext
-- without the operator ever choosing.  Post-v7, fresh zones come up
-- with backend='disabled' and apps that opt into the archive tier
-- refuse to install until the operator picks local-or-s3 on the
-- System tab.  See compute_space/src/compute_space/core/archive_backend.py
-- and the get / post route handlers for how the disabled state
-- threads through.
--
-- SQLite cannot ALTER a CHECK constraint in place; the only path is
-- the canonical rename-create-copy-drop dance.  The single-row
-- archive_backend table makes the copy a one-row INSERT-SELECT, so
-- the migration is fast and atomic.  Existing rows (backend='local'
-- from v5) are preserved verbatim — only the CHECK widens and the
-- default shifts; nothing flips an already-configured zone.
--
-- No outer BEGIN/COMMIT here: SqlFileMigration.apply() wraps this
-- file in BEGIN EXCLUSIVE / COMMIT itself (see compute_space/db/
-- versioned/base.py).  Adding our own outer transaction would
-- nest and SQLite rejects nested transactions with
-- "cannot start a transaction within a transaction".

-- 1. Stash the old table out of the way.  We rename rather than
-- DROP-then-CREATE so the failure modes are clean: a crash between
-- steps leaves the schema in a recoverable state with the data
-- still in archive_backend_old.
ALTER TABLE archive_backend RENAME TO archive_backend_old;

-- 2. Create the new table with the widened CHECK + new default.
-- Schema body kept identical to schema.sql apart from the two
-- changes; keep the two in lockstep when we add columns later.
CREATE TABLE archive_backend (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    backend TEXT NOT NULL DEFAULT 'disabled' CHECK(backend IN ('disabled', 'local', 's3')),
    state TEXT NOT NULL DEFAULT 'idle' CHECK(state IN ('idle', 'switching')),
    s3_bucket TEXT,
    s3_region TEXT,
    s3_endpoint TEXT,
    s3_prefix TEXT,
    s3_access_key_id TEXT,
    s3_secret_access_key TEXT,
    juicefs_volume_name TEXT NOT NULL DEFAULT 'openhost',
    last_switched_at TEXT,
    state_message TEXT
);

-- 3. Copy the single row across verbatim.  Existing local-backend
-- zones stay at 'local'; nothing flips them to 'disabled'.  This is
-- intentional: the disabled-default change is for fresh zones only.
INSERT INTO archive_backend (
    id, backend, state, s3_bucket, s3_region, s3_endpoint, s3_prefix,
    s3_access_key_id, s3_secret_access_key, juicefs_volume_name,
    last_switched_at, state_message
)
SELECT
    id, backend, state, s3_bucket, s3_region, s3_endpoint, s3_prefix,
    s3_access_key_id, s3_secret_access_key, juicefs_volume_name,
    last_switched_at, state_message
FROM archive_backend_old;

-- 4. Drop the renamed-aside copy.
DROP TABLE archive_backend_old;
