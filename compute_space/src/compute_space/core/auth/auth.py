import hashlib
import re
import sqlite3
from datetime import UTC
from datetime import datetime

from compute_space.db import get_db

# Lowercase alphanumeric + dots/underscores/hyphens, starting with an alphanumeric.
# Length cap matches Mastodon's 30-char column. Lowercase-only so usernames are safe for subdomains.
OWNER_USERNAME_MAX_LEN = 30
_OWNER_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,29}$")
DEFAULT_OWNER_USERNAME = "owner"


def validate_owner_username(value: str) -> str | None:
    """Return None if valid, else an error string for the setup/settings form."""
    if not value:
        return "Username is required."
    if len(value) > OWNER_USERNAME_MAX_LEN:
        return f"Username must be at most {OWNER_USERNAME_MAX_LEN} characters."
    if not _OWNER_USERNAME_RE.match(value):
        return (
            "Username must start with a lowercase letter or digit and contain only"
            " lowercase letters, digits, `.`, `_` or `-`."
        )
    return None


def read_owner_username(db: sqlite3.Connection) -> str | None:
    """Return the configured owner username, or None if no owner row exists (pre-setup) or the column is empty."""
    row = db.execute("SELECT username FROM owner WHERE id = 1").fetchone()
    if row is None:
        return None
    username: str = row["username"]
    if not username:
        return None
    return username


def update_owner_username(db: sqlite3.Connection, new_username: str) -> None:
    """Replace the owner row's username. Caller must validate input and commit.

    Raises ValueError if no owner row exists (pre-setup).
    """
    cursor = db.execute("UPDATE owner SET username = ? WHERE id = 1", (new_username,))
    if cursor.rowcount == 0:
        raise ValueError("No owner row exists; cannot update username before /setup runs.")


def validate_api_token(token: str) -> dict[str, str] | None:
    """Validate a bearer token against the api_tokens table.

    Returns a claims dict (owner-level access) or None.
    """
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute(
        "SELECT name, expires_at FROM api_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
        return None
    owner_username = read_owner_username(db)
    if owner_username is None:
        return None
    # TODO: give this a proper type?
    return {"sub": owner_username, "username": owner_username}


def resolve_app_from_token(token: str) -> str | None:
    """Look up a Bearer token in the app_tokens table, return the app_id or None."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute("SELECT app_id FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    return row["app_id"] if row else None
