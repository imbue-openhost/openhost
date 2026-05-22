"""V2 permission operations: grant, revoke, query with JSON payloads and scopes."""

import json

import attr

from compute_space.db import get_db

# Grant payloads are JSON-shaped values defined by each service. A grant is
# either an opaque string (e.g. "FULL_ACCESS") or a JSON structure
# (e.g. {"key": "DB_URL"}); top-level numbers are excluded since a bare
# scalar isn't a meaningful grant identity.
type GrantAtom = list[GrantAtom] | dict[str, GrantAtom] | str | int | float | bool
type Grant = list[GrantAtom] | dict[str, GrantAtom] | str


@attr.s(auto_attribs=True, frozen=True)
class GrantedPermission:
    grant: Grant
    scope: str
    provider_app_id: str | None


@attr.s(auto_attribs=True, frozen=True)
class PermissionRecord:
    consumer_app_id: str
    service_url: str
    grant: Grant
    scope: str
    provider_app_id: str | None


def get_granted_permissions_v2(
    consumer_app_id: str,
    service_url: str,
) -> list[GrantedPermission]:
    """Return all grant objects for a consumer+service pair."""
    db = get_db()
    rows = db.execute(
        """SELECT grant_payload, scope, provider_app_id
           FROM permissions_v2
           WHERE consumer_app_id = ? AND service_url = ?""",
        (consumer_app_id, service_url),
    ).fetchall()
    return [
        GrantedPermission(
            grant=json.loads(row["grant_payload"]),
            scope=row["scope"],
            provider_app_id=row["provider_app_id"] or None,
        )
        for row in rows
    ]


def grant_permission_v2(
    consumer_app_id: str,
    service_url: str,
    grant_payload: Grant,
    scope: str = "global",
    provider_app_id: str | None = None,
) -> None:
    """Grant a permission. Idempotent."""
    db = get_db()
    payload_json = json.dumps(grant_payload, sort_keys=True)
    db.execute(
        """INSERT OR IGNORE INTO permissions_v2
           (consumer_app_id, service_url, grant_payload, scope, provider_app_id)
           VALUES (?, ?, ?, ?, ?)""",
        (consumer_app_id, service_url, payload_json, scope, provider_app_id or ""),
    )
    db.commit()


def revoke_permission_v2(
    consumer_app_id: str,
    service_url: str,
    grant_payload: Grant,
    scope: str = "global",
    provider_app_id: str | None = None,
) -> bool:
    """Revoke a permission. Returns True if a row was deleted, False if not found."""
    db = get_db()
    payload_json = json.dumps(grant_payload, sort_keys=True)
    cursor = db.execute(
        """DELETE FROM permissions_v2
           WHERE consumer_app_id = ? AND service_url = ? AND grant_payload = ? AND scope = ? AND provider_app_id = ?""",
        (consumer_app_id, service_url, payload_json, scope, provider_app_id or ""),
    )
    db.commit()
    return cursor.rowcount > 0


def get_all_permissions_v2(
    consumer_app_id: str | None = None,
) -> list[PermissionRecord]:
    """Return all v2 permissions, optionally filtered by consumer app."""
    db = get_db()
    if consumer_app_id:
        rows = db.execute(
            """SELECT consumer_app_id, service_url, grant_payload, scope, provider_app_id
               FROM permissions_v2 WHERE consumer_app_id = ?
               ORDER BY service_url""",
            (consumer_app_id,),
        ).fetchall()
    else:
        rows = db.execute(
            """SELECT consumer_app_id, service_url, grant_payload, scope, provider_app_id
               FROM permissions_v2
               ORDER BY consumer_app_id, service_url"""
        ).fetchall()
    return [
        PermissionRecord(
            consumer_app_id=row["consumer_app_id"],
            service_url=row["service_url"],
            grant=json.loads(row["grant_payload"]),
            scope=row["scope"],
            provider_app_id=row["provider_app_id"] or None,
        )
        for row in rows
    ]
