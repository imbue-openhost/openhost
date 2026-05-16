import hashlib
import secrets
import sqlite3
import time
from datetime import UTC
from datetime import datetime

import attr

SESSION_TTL_SECONDS = 7 * 24 * 60 * 60  # one week
SESSION_COOKIE_NAME = "session_token"


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedAccessor:
    pass


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedUser(AuthenticatedAccessor):
    user_id: int
    username: str


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedAPIKey(AuthenticatedAccessor):
    # TODO: fill this out with permissions etc
    pass


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedApp(AuthenticatedAccessor):
    app_id: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, db: sqlite3.Connection) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (_hash(token), user_id, int(time.time()) + SESSION_TTL_SECONDS),
    )
    return token


def validate_session_token(token: str, db: sqlite3.Connection) -> AuthenticatedUser | None:
    token_hash = _hash(token)
    query = """
        SELECT u.user_id, u.username, s.expires_at
        FROM sessions s JOIN users u ON u.user_id = s.user_id
        WHERE s.token_hash = ?"""
    if row := db.execute(query, (token_hash,)).fetchone():
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return AuthenticatedUser(user_id=row["user_id"], username=row["username"])
    return None


def validate_api_token(token: str, db: sqlite3.Connection) -> AuthenticatedAPIKey | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    if row := db.execute(
        "SELECT name, expires_at FROM api_tokens WHERE token_hash = ?",
        (token_hash,),
    ).fetchone():
        if datetime.fromisoformat(row["expires_at"]) >= datetime.now(UTC):
            return AuthenticatedAPIKey()
    return None


def validate_app_token(token: str, db: sqlite3.Connection) -> AuthenticatedApp | None:
    token_hash = _hash(token)
    query = """
        SELECT apps.app_id, apps.name, app_tokens.expires_at
        FROM app_tokens JOIN apps ON apps.app_id = app_tokens.app_id
        WHERE app_tokens.token_hash = ?"""
    if row := db.execute(query, (token_hash,)).fetchone():
        return AuthenticatedApp(app_id=row["app_id"])
    return None
