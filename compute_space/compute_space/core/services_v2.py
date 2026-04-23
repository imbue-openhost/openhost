import sqlite3

from compute_space.core.manifest import AppManifest
from compute_space.db.connection import make_atomic_with_savepoint


def register_v2_service_providers(
    app_name: str,
    manifest: AppManifest,
    db: sqlite3.Connection,
) -> None:
    """Register V2 service providers from manifest. Sets default if none exists."""
    with make_atomic_with_savepoint(db):
        db.execute("DELETE FROM service_providers_v2 WHERE app_name = ?", (app_name,))
        for svc in manifest.provides_services_v2:
            db.execute(
                "INSERT OR REPLACE INTO service_providers_v2 (service_url, app_name, version, endpoint) VALUES (?, ?, ?, ?)",
                (svc.service, app_name, svc.version, svc.endpoint),
            )
            existing_default = db.execute(
                "SELECT 1 FROM service_defaults WHERE service_url = ?",
                (svc.service,),
            ).fetchone()
            if not existing_default:
                db.execute(
                    "INSERT INTO service_defaults (service_url, app_name) VALUES (?, ?)",
                    (svc.service, app_name),
                )
