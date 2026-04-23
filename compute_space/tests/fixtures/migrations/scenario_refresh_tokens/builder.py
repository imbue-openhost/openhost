"""
Seed data focused on the ``refresh_tokens`` table.

Four rows cover every combination of ``revoked`` and expiry windows:
  (a) active + future expiry,
  (b) revoked + future expiry,
  (c) active + past expiry,
  (d) revoked + past expiry.

Timestamps are fixed strings (not ``datetime('now')``) so snapshots are
deterministic.
"""

from __future__ import annotations

import hashlib
import sqlite3

# Fixed anchor instants so ``at_*.sql`` snapshots do not depend on clock.
_FUTURE_EXPIRY = "2099-01-01T00:00:00"
_PAST_EXPIRY = "2000-01-01T00:00:00"
_FIXED_CREATED_AT = "2025-01-01T00:00:00"


def _hash(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def seed_at(conn: sqlite3.Connection, migration_id: str) -> None:
    """Insert scenario data appropriate for the given migration checkpoint."""
    if migration_id == "0001_initial":
        _seed_baseline(conn)


def _seed_baseline(conn: sqlite3.Connection) -> None:
    # Owner is required by FK-adjacent checks and exercises the ``owner``
    # table schema (``password_needs_set``, nullable ``password_hash``).
    conn.execute(
        "INSERT INTO owner (id, username, password_hash, password_needs_set, created_at) VALUES (?, ?, ?, ?, ?)",
        (1, "admin", None, 1, _FIXED_CREATED_AT),
    )

    conn.executemany(
        "INSERT INTO refresh_tokens (id, token_hash, expires_at, revoked) VALUES (?, ?, ?, ?)",
        [
            (1, _hash("active-future"), _FUTURE_EXPIRY, 0),
            (2, _hash("revoked-future"), _FUTURE_EXPIRY, 1),
            (3, _hash("active-past"), _PAST_EXPIRY, 0),
            (4, _hash("revoked-past"), _PAST_EXPIRY, 1),
        ],
    )
