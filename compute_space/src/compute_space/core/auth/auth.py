import hashlib
import re
import sqlite3
from datetime import UTC
from datetime import datetime

from compute_space.db import get_db

# starting with an alphanumeric.  Length cap matches Mastodon's 30-
# char column with headroom; leading-alphanumeric avoids the
# common SSO pitfall of leading-dot identifiers.
#
# Note we do NOT enforce the strictest downstream constraint
# (PeerTube's ``[a-z0-9._]{1,50}`` — lowercase only, no hyphen).
# Forcing lowercase or dropping ``-`` would prevent legitimate
# identifiers like ``Andrew`` or ``zack-2``; instead, downstream
# apps that need to lowercase or transform the value are expected
# to do so on receive.  The validator's job is to reject values
# that no SSO consumer can possibly accept (whitespace, control
# chars, non-ASCII, identifier-shaped collisions like
# ``alice@example.com``), not to second-guess each downstream's
# narrowest charset.
OWNER_USERNAME_MAX_LEN = 50
_OWNER_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
DEFAULT_OWNER_USERNAME = "owner"


def validate_owner_username(value: str) -> str | None:
    """Return None if ``value`` is a valid owner username, else an
    operator-readable error string suitable for re-rendering on the
    setup / settings forms.

    Empty input is INVALID here — callers that want "leave blank to
    keep current value" semantics must filter that out before
    calling.  This separation keeps the validator's contract simple
    (one branch, one rule).
    """
    if not value:
        return "Username is required."
    if len(value) > OWNER_USERNAME_MAX_LEN:
        return f"Username must be at most {OWNER_USERNAME_MAX_LEN} characters."
    if not _OWNER_USERNAME_RE.match(value):
        return "Username must start with a letter or digit and contain only letters, digits, ``.``, ``_`` or ``-``."
    return None


def read_owner_username(db: sqlite3.Connection) -> str | None:
    """Return the configured owner username, or ``None`` if no owner
    row exists yet (pre-setup) or the column is empty.

    Used by the per-app provisioning path to stamp
    ``OPENHOST_OWNER_USERNAME`` onto containers.  Returning ``None``
    on the no-owner path lets callers decide whether to omit the
    env var entirely (preferred — apps then know "no name configured"
    rather than seeing a misleading default).

    Indexes by column name (``row["username"]``) — every connection
    in this codebase is opened with ``row_factory = sqlite3.Row``
    (see ``db.connection.get_db``) and tests use the same factory
    via the shared ``db`` fixture.  Tuple-row connections aren't
    used anywhere; if a future caller passes one in, the
    ``sqlite3.Row``-style indexing will raise a TypeError loudly
    rather than silently mis-fetch.
    """
    row = db.execute("SELECT username FROM owner WHERE id = 1").fetchone()
    if row is None:
        return None
    # row["username"] is typed as Any by sqlite3.Row; coerce to the
    # declared return type so mypy's no-any-return doesn't fire.
    # The column is declared TEXT NOT NULL in the schema so a non-None
    # row reliably yields a string.
    username: str = row["username"]
    if not username:
        return None
    return username


def update_owner_username(db: sqlite3.Connection, new_username: str) -> None:
    """Replace the owner row's ``username`` with ``new_username``.

    Caller is responsible for validating the input + committing the
    transaction.  Validation lives in ``validate_owner_username``;
    the SQL ``NOT NULL`` constraint here only blocks ``NULL`` (not
    the empty string), so callers MUST run the validator first.

    Raises:
        ValueError: if no owner row exists yet.  ``id = 1`` is
            seeded only by the /setup flow; calling this against an
            empty owner table would silently no-op the UPDATE and
            return a misleading "ok" response upstream.

    SQLite's underlying ``sqlite3.Error`` subclasses (e.g.
    ``OperationalError`` from a lock timeout, ``IntegrityError`` if
    a future schema change adds a non-trivial constraint) propagate
    unwrapped — callers that need to differentiate transient retry-
    able errors from validation errors should catch them at the
    transaction boundary.
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
