"""Permission operations: grant, revoke, query."""

from typing import overload

from compute_space.db import get_db


@overload
def get_granted_permissions(consumer_app_id: str) -> set[str]: ...
@overload
def get_granted_permissions() -> dict[str, set[str]]: ...


def get_granted_permissions(
    consumer_app_id: str | None = None,
) -> set[str] | dict[str, set[str]]:
    """Return granted permissions. With app_id: set of keys. Without: dict of app_id -> set of keys."""
    db = get_db()
    if consumer_app_id is not None:
        rows = db.execute(
            "SELECT permission_key FROM permissions WHERE consumer_app_id = ?",
            (consumer_app_id,),
        ).fetchall()
        return {row["permission_key"] for row in rows}
    rows = db.execute(
        "SELECT consumer_app_id, permission_key FROM permissions ORDER BY consumer_app_id, permission_key"
    ).fetchall()
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(row["consumer_app_id"], set()).add(row["permission_key"])
    return result


def grant_permissions(consumer_app_id: str, permission_keys: list[str]) -> None:
    """Grant permissions to an app. Idempotent."""
    db = get_db()
    for key in permission_keys:
        db.execute(
            "INSERT OR IGNORE INTO permissions (consumer_app_id, permission_key) VALUES (?, ?)",
            (consumer_app_id, key),
        )
    db.commit()


def revoke_permissions(consumer_app_id: str, permission_keys: list[str]) -> None:
    """Revoke permissions from an app. Silently succeeds if a key isn't granted."""
    db = get_db()
    for key in permission_keys:
        db.execute(
            "DELETE FROM permissions WHERE consumer_app_id = ? AND permission_key = ?",
            (consumer_app_id, key),
        )
    db.commit()
