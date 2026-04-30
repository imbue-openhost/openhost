-- v4: archive backend state table.
--
-- The archive tier (app_archive bind mount) has an operator-selected
-- backend.  The default is local disk; the operator can switch to S3
-- (JuiceFS-backed) from the dashboard at runtime.  This table records
-- the current backend + the S3 details when applicable, so a restart
-- of openhost-core can reattach the JuiceFS mount without prompting
-- the operator again.
--
-- Single-row table — there's exactly one archive backend per zone.
-- The CHECK on id makes that explicit.
--
-- Threat model + plaintext credentials:
-- the S3 secret-access-key is stored in plaintext alongside the access-
-- key id.  This isn't great, but encrypting it would be theater: the
-- DB lives at ``router.db`` (mode 0600, owned by the ``host`` user),
-- and the only reader of any encryption key would also be the host
-- user.  Anyone who can read the DB to obtain ciphertext can also
-- read whatever key we'd derive.  Rather than imply a security
-- boundary that doesn't exist, store the secret in plaintext and
-- pin operator expectations: protecting the row means protecting the
-- file.

CREATE TABLE IF NOT EXISTS archive_backend (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    backend TEXT NOT NULL DEFAULT 'local' CHECK(backend IN ('local', 's3')),
    state TEXT NOT NULL DEFAULT 'idle' CHECK(state IN ('idle', 'switching')),
    s3_bucket TEXT,
    s3_region TEXT,
    s3_endpoint TEXT,
    s3_access_key_id TEXT,
    s3_secret_access_key TEXT,
    juicefs_volume_name TEXT NOT NULL DEFAULT 'openhost',
    last_switched_at TEXT,
    state_message TEXT
);

-- Seed the single row in 'local' state so reads always find a row
-- without the application code needing to insert-or-update.
INSERT OR IGNORE INTO archive_backend (id, backend) VALUES (1, 'local');
