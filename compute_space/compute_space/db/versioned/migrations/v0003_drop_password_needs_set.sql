-- v3: tighten owner schema.
--
-- password_hash should never be null — /setup always writes a bcrypt
-- hash before any request can authenticate. password_needs_set was
-- added alongside the nullable column but nothing ever read or
-- cleared it.
--
-- SQLite cannot change a column's NOT NULL constraint in place, so
-- we recreate the table. Any owner row with NULL password_hash is
-- in an invalid pre-setup state and can't satisfy the new constraint;
-- drop it so the migration succeeds and /setup can run again.

DELETE FROM owner WHERE password_hash IS NULL;

CREATE TABLE owner_new (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO owner_new (id, username, password_hash, created_at)
SELECT id, username, password_hash, created_at FROM owner;

DROP TABLE owner;

ALTER TABLE owner_new RENAME TO owner;
