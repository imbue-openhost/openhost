"""V2 permission operations: grant, revoke, query with JSON payloads and scopes."""

import json
from typing import Any

import attr

from compute_space.db import get_db


@attr.s(auto_attribs=True, frozen=True)
class GrantedPermission:
    grant: dict[str, Any]
    scope: str
    provider_app: str | None


@attr.s(auto_attribs=True, frozen=True)
class PermissionRecord:
    consumer_app: str
    service_url: str
    grant: dict[str, Any]
    scope: str
    provider_app: str | None


def get_granted_permissions_v2(
    consumer_app: str,
    service_url: str,
) -> list[GrantedPermission]:
    """Return all grant objects for a consumer+service pair."""
    db = get_db()
    rows = db.execute(
        """SELECT grant_payload, scope, provider_app
           FROM permissions_v2
           WHERE consumer_app = ? AND service_url = ?""",
        (consumer_app, service_url),
    ).fetchall()
    return [
        GrantedPermission(
            grant=json.loads(row["grant_payload"]),
            scope=row["scope"],
            provider_app=row["provider_app"] or None,
        )
        for row in rows
    ]


def grant_permission_v2(
    consumer_app: str,
    service_url: str,
    grant_payload: dict[str, Any],
    scope: str = "global",
    provider_app: str | None = None,
) -> None:
    """Grant a permission. Idempotent."""
    db = get_db()
    payload_json = json.dumps(grant_payload, sort_keys=True)
    db.execute(
        """INSERT OR IGNORE INTO permissions_v2
           (consumer_app, service_url, grant_payload, scope, provider_app)
           VALUES (?, ?, ?, ?, ?)""",
        (consumer_app, service_url, payload_json, scope, provider_app or ""),
    )
    db.commit()


def revoke_permission_v2(
    consumer_app: str,
    service_url: str,
    grant_payload: dict[str, Any],
    scope: str = "global",
    provider_app: str | None = None,
) -> bool:
    """Revoke a permission. Returns True if a row was deleted, False if not found."""
    db = get_db()
    payload_json = json.dumps(grant_payload, sort_keys=True)
    cursor = db.execute(
        """DELETE FROM permissions_v2
           WHERE consumer_app = ? AND service_url = ? AND grant_payload = ? AND scope = ? AND provider_app = ?""",
        (consumer_app, service_url, payload_json, scope, provider_app or ""),
    )
    db.commit()
    return cursor.rowcount > 0


def get_all_permissions_v2(
    consumer_app: str | None = None,
) -> list[PermissionRecord]:
    """Return all v2 permissions, optionally filtered by consumer app."""
    db = get_db()
    if consumer_app:
        rows = db.execute(
            """SELECT consumer_app, service_url, grant_payload, scope, provider_app
               FROM permissions_v2 WHERE consumer_app = ?
               ORDER BY service_url""",
            (consumer_app,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT consumer_app, service_url, grant_payload, scope, provider_app
               FROM permissions_v2
               ORDER BY consumer_app, service_url"""
        ).fetchall()
    return [
        PermissionRecord(
            consumer_app=row["consumer_app"],
            service_url=row["service_url"],
            grant=json.loads(row["grant_payload"]),
            scope=row["scope"],
            provider_app=row["provider_app"] or None,
        )
        for row in rows
    ]
