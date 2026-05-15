import hashlib
from datetime import UTC
from datetime import datetime

from compute_space.db import get_db


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
    owner = db.execute("SELECT username FROM owner WHERE id = 1").fetchone()
    if not owner:
        return None
    # TODO: give this a proper type?
    return {"sub": owner["username"], "username": owner["username"]}


def resolve_app_from_token(token: str) -> str | None:
    """Look up a Bearer token in the app_tokens table, return the app_id or None."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db = get_db()
    row = db.execute("SELECT app_id FROM app_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    return row["app_id"] if row else None
