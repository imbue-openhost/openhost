from contextlib import closing
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from oauth_provider.core.credentials import get_provider_creds
from oauth_provider.core.models import StoredToken
from oauth_provider.core.models import TokenInfo
from oauth_provider.core.models import TokenResponse
from oauth_provider.core.providers import normalize_scopes
from oauth_provider.core.providers import refresh_access_token
from oauth_provider.db import get_db


def store_token(
    provider: str,
    scopes_key: str,
    account: str,
    access_token: str,
    refresh_token: str | None,
    expires_at: str | None,
) -> None:
    with closing(get_db()) as db:
        db.execute(
            """INSERT INTO oauth_tokens (provider, scopes, account, access_token, refresh_token, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (provider, scopes_key, account, access_token, refresh_token, expires_at),
        )
        db.commit()


def select_tokens(provider: str, scopes_key: str, account: str | None) -> list[StoredToken]:
    with closing(get_db()) as db:
        if account:
            rows = db.execute(
                "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ? AND account = ?",
                (provider, scopes_key, account),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ?",
                (provider, scopes_key),
            ).fetchall()
    return [StoredToken(**dict(r)) for r in rows]


def update_token_after_refresh(token_id: int, access_token: str, expires_at: str | None) -> None:
    with closing(get_db()) as db:
        db.execute(
            """UPDATE oauth_tokens
               SET access_token = ?, expires_at = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (access_token, expires_at, token_id),
        )
        db.commit()


def get_token_by_id(token_id: int) -> StoredToken | None:
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    return StoredToken(**dict(row)) if row else None


def remove_token_by_id(token_id: int) -> None:
    with closing(get_db()) as db:
        db.execute("DELETE FROM oauth_tokens WHERE id = ?", (token_id,))
        db.commit()


def find_and_remove_token(provider: str, scopes_key: str, account: str) -> StoredToken | None:
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ? AND account = ?",
            (provider, scopes_key, account),
        ).fetchone()
        if row:
            db.execute("DELETE FROM oauth_tokens WHERE id = ?", (row["id"],))
            db.commit()
    return StoredToken(**dict(row)) if row else None


def list_all_tokens() -> list[TokenInfo]:
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT id, provider, scopes, account, expires_at, created_at, updated_at "
            "FROM oauth_tokens ORDER BY provider, account"
        ).fetchall()
    return [TokenInfo(**dict(r)) for r in rows]


async def get_valid_token(provider_name: str, scopes: list[str], account: str) -> TokenResponse | None:
    """Look up a cached token, auto-refreshing if expired. Returns None if no valid token is found."""
    scopes_key = normalize_scopes(scopes)

    tokens = select_tokens(provider_name, scopes_key, account if account != "default" else None)
    if not tokens:
        return None

    # TODO: may want to fail if multiple tokens are returned
    token = tokens[0]

    if not token.expires_at:
        return TokenResponse(access_token=token.access_token, expires_at=None)

    exp = datetime.fromisoformat(token.expires_at)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if exp > datetime.now(UTC) + timedelta(seconds=60):
        return TokenResponse(access_token=token.access_token, expires_at=token.expires_at)

    if not token.refresh_token:
        return None

    client_id, client_secret = await get_provider_creds(provider_name)
    refreshed = await refresh_access_token(provider_name, token.refresh_token, client_id, client_secret)
    if refreshed and "access_token" in refreshed:
        new_expires_at = None
        if refreshed.get("expires_in"):
            new_expires_at = (datetime.now(UTC) + timedelta(seconds=refreshed["expires_in"])).isoformat()
        update_token_after_refresh(token.id, refreshed["access_token"], new_expires_at)
        return TokenResponse(access_token=refreshed["access_token"], expires_at=new_expires_at)

    return None


def get_accounts(provider: str, scopes_key: str) -> list[str]:
    """Get all accounts (emails/usernames) with stored tokens for a provider+scopes combo."""
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT account FROM oauth_tokens WHERE provider = ? AND scopes = ? ORDER BY account",
            (provider, scopes_key),
        ).fetchall()
    return [r["account"] for r in rows]
