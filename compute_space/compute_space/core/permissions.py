"""Permission operations: grant, revoke, query."""

from typing import overload

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from compute_space.db import get_session
from compute_space.db.models import Permission


@overload
async def get_granted_permissions(consumer_app: str) -> set[str]: ...
@overload
async def get_granted_permissions() -> dict[str, set[str]]: ...


async def get_granted_permissions(
    consumer_app: str | None = None,
) -> set[str] | dict[str, set[str]]:
    """Return granted permissions. With app: set of keys. Without: dict of app -> set of keys."""
    session = get_session()
    if consumer_app is not None:
        stmt = select(Permission.permission_key).where(Permission.consumer_app == consumer_app)
        rows = await session.execute(stmt)
        return set(rows.scalars().all())
    stmt_all = select(Permission.consumer_app, Permission.permission_key).order_by(
        Permission.consumer_app, Permission.permission_key
    )
    rows = await session.execute(stmt_all)
    result: dict[str, set[str]] = {}
    for app_name, key in rows.all():
        result.setdefault(app_name, set()).add(key)
    return result


async def grant_permissions(consumer_app: str, permission_keys: list[str]) -> None:
    """Grant permissions to an app. Idempotent."""
    if not permission_keys:
        return
    session = get_session()
    for key in permission_keys:
        stmt = sqlite_insert(Permission).values(consumer_app=consumer_app, permission_key=key)
        stmt = stmt.on_conflict_do_nothing(index_elements=["consumer_app", "permission_key"])
        await session.execute(stmt)
    await session.commit()


async def revoke_permissions(consumer_app: str, permission_keys: list[str]) -> None:
    """Revoke permissions from an app. Silently succeeds if a key isn't granted."""
    if not permission_keys:
        return
    session = get_session()
    for key in permission_keys:
        await session.execute(
            delete(Permission).where(
                Permission.consumer_app == consumer_app,
                Permission.permission_key == key,
            )
        )
    await session.commit()
