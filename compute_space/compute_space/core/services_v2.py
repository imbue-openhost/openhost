import sqlite3

from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version

from compute_space.core.manifest import AppManifest
from compute_space.core.services import ServiceNotAvailable
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


def resolve_provider(
    service_url: str,
    version_specifier: str,
    db: sqlite3.Connection,
    provider_app: str | None = None,
) -> tuple[str, int, str, str]:
    """Resolve a provider for a service URL, checking version compatibility.

    Returns (app_name, local_port, version, endpoint).

    If provider_app is given, that specific app is used (fail if it doesn't exist or is version-incompatible).
    Otherwise the default provider is used (same semantics - fail if it doesn't exist or is version-incompatible).
    """
    try:
        spec = SpecifierSet(version_specifier)
    except InvalidSpecifier as e:
        raise ServiceNotAvailable(f"Invalid version specifier: {version_specifier}") from e

    if provider_app:
        target_app = provider_app
    else:
        default = db.execute(
            "SELECT app_name FROM service_defaults WHERE service_url = ?",
            (service_url,),
        ).fetchone()
        if not default:
            raise ServiceNotAvailable(f"No provider for service '{service_url}'")
        target_app = default["app_name"]

    row = db.execute(
        """SELECT sp.version, sp.endpoint, a.local_port, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.name = sp.app_name
           WHERE sp.service_url = ? AND sp.app_name = ?""",
        (service_url, target_app),
    ).fetchone()

    if not row:
        raise ServiceNotAvailable(f"Provider '{target_app}' not found for service '{service_url}'")

    if row["status"] != "running":
        raise ServiceNotAvailable(f"Provider '{target_app}' for '{service_url}' is not running")

    try:
        v = Version(row["version"])
    except InvalidVersion as e:
        raise ServiceNotAvailable(f"Provider '{target_app}' has invalid version '{row['version']}'") from e

    if v not in spec:
        raise ServiceNotAvailable(
            f"Provider '{target_app}' version {row['version']} does not match '{version_specifier}'"
        )

    return target_app, row["local_port"], row["version"], row["endpoint"]
