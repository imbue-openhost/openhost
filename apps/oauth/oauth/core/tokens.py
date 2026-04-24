from contextlib import closing
from typing import Any

from oauth.db import get_db


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
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider, scopes, account) DO UPDATE SET
                   access_token = excluded.access_token,
                   refresh_token = COALESCE(excluded.refresh_token, oauth_tokens.refresh_token),
                   expires_at = excluded.expires_at,
                   updated_at = datetime('now')""",
            (provider, scopes_key, account, access_token, refresh_token, expires_at),
        )
        db.commit()


def get_token(provider: str, scopes_key: str, account: str) -> dict[str, Any] | None:
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ? AND account = ?",
            (provider, scopes_key, account),
        ).fetchone()
    return dict(row) if row else None


def get_tokens_for_provider_scopes(provider: str, scopes_key: str) -> list[dict[str, Any]]:
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ?",
            (provider, scopes_key),
        ).fetchall()
    return [dict(r) for r in rows]


def update_token_access(token_id: int, access_token: str, expires_at: str | None) -> None:
    with closing(get_db()) as db:
        db.execute(
            """UPDATE oauth_tokens
               SET access_token = ?, expires_at = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (access_token, expires_at, token_id),
        )
        db.commit()


def get_token_by_id(token_id: int) -> dict[str, Any] | None:
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
    return dict(row) if row else None


def remove_token_by_id(token_id: int) -> None:
    with closing(get_db()) as db:
        db.execute("DELETE FROM oauth_tokens WHERE id = ?", (token_id,))
        db.commit()


def find_and_remove_token(provider: str, scopes_key: str, account: str) -> dict[str, Any] | None:
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT * FROM oauth_tokens WHERE provider = ? AND scopes = ? AND account = ?",
            (provider, scopes_key, account),
        ).fetchone()
        if row:
            db.execute("DELETE FROM oauth_tokens WHERE id = ?", (row["id"],))
            db.commit()
    return dict(row) if row else None


def list_all_tokens() -> list[dict[str, Any]]:
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT id, provider, scopes, account, expires_at, created_at, updated_at "
            "FROM oauth_tokens ORDER BY provider, account"
        ).fetchall()
    return [dict(r) for r in rows]


def get_accounts(provider: str, scopes_key: str) -> list[str]:
    with closing(get_db()) as db:
        rows = db.execute(
            "SELECT account FROM oauth_tokens WHERE provider = ? AND scopes = ? ORDER BY account",
            (provider, scopes_key),
        ).fetchall()
    return [r["account"] for r in rows]
