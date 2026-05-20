-- v9: replace JWT/refresh-token auth with opaque session tokens.
--
-- The pre-v9 schema had a single ``owner`` row plus a ``refresh_tokens``
-- table that backed the JWT access-token / refresh-token rotation.  The
-- new auth code keeps an opaque token in ``sessions`` (sha256-hashed)
-- and a multi-user-shaped ``users`` table (still only one row in
-- practice, but the schema no longer hard-codes singularity).
--
-- The existing owner row is migrated into ``users`` so the operator
-- keeps their username/password and does not have to re-run /setup
-- after the upgrade. ``refresh_tokens`` is dropped (existing JWT
-- refresh tokens are not portable to the opaque-session scheme — any
-- logged-in browsers will just need to log in once).

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO users (username, password_hash, created_at)
SELECT username, password_hash, created_at FROM owner;

DROP TABLE IF EXISTS refresh_tokens;
DROP TABLE IF EXISTS owner;

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);
