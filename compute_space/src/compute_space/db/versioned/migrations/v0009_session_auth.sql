-- v9: replace JWT/refresh-token auth with opaque session tokens.
--
-- The pre-v9 schema had a single ``owner`` row plus a ``refresh_tokens``
-- table that backed the JWT access-token / refresh-token rotation.  The
-- new auth code keeps an opaque token in ``sessions`` (sha256-hashed)
-- and a multi-user-shaped ``users`` table (still only one row in
-- practice, but the schema no longer hard-codes singularity).
--
-- The old data is dropped wholesale: the migration runs once during the
-- upgrade, after which the operator must re-run /setup (which now
-- inserts into ``users`` and ``sessions``).  This matches what the
-- operator already has to do, since the JWT signing key on disk is
-- regenerated on first boot of the new code anyway.

DROP TABLE IF EXISTS refresh_tokens;
DROP TABLE IF EXISTS owner;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);
