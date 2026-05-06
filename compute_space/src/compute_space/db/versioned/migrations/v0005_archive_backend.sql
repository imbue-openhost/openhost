-- v5: archive backend state table.
--
-- The archive tier (app_archive bind mount) has an operator-selected
-- backend.  Fresh zones come up with backend='disabled'; the operator
-- configures S3 (JuiceFS-backed) once from the dashboard.  Apps that
-- opt into the app_archive tier refuse to install until the backend
-- is set to 's3'.
--
-- Single-row table — there's exactly one archive backend per zone.

CREATE TABLE IF NOT EXISTS archive_backend (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    backend TEXT NOT NULL DEFAULT 'disabled' CHECK(backend IN ('disabled', 's3')),
    s3_bucket TEXT,
    s3_region TEXT,
    s3_endpoint TEXT,
    s3_prefix TEXT,
    s3_access_key_id TEXT,
    s3_secret_access_key TEXT,
    juicefs_volume_name TEXT NOT NULL DEFAULT 'openhost',
    configured_at TEXT,
    state_message TEXT
);

INSERT OR IGNORE INTO archive_backend (id) VALUES (1);
