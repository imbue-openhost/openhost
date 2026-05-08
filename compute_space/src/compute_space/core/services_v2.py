import sqlite3

from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version

from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.services import ServiceNotAvailable
from compute_space.db.connection import make_atomic_with_savepoint


class ShortnameNotDeclared(Exception):
    """Consumer's manifest does not declare the requested shortname."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def lookup_shortname(consumer_app: str, shortname: str, db: sqlite3.Connection) -> tuple[str, str]:
    """Resolve (service_url, version_spec) by shortname from the consumer's stored manifest.

    Manifest is read from apps.manifest_raw and parsed on each call (typically a few KB of TOML).
    """
    row = db.execute("SELECT manifest_raw FROM apps WHERE name = ?", (consumer_app,)).fetchone()
    if not row or not row["manifest_raw"]:
        raise ShortnameNotDeclared(f"No manifest stored for app '{consumer_app}'")
    manifest = parse_manifest_from_string(row["manifest_raw"])
    for perm in manifest.consumes_services_v2:
        if perm.shortname == shortname:
            return perm.service, perm.version
    raise ShortnameNotDeclared(f"Shortname '{shortname}' not declared in '{consumer_app}' manifest")


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
                "INSERT OR REPLACE INTO service_providers_v2 (service_url, app_name, service_version, endpoint) VALUES (?, ?, ?, ?)",
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
        """SELECT sp.service_version, sp.endpoint, a.local_port, a.status
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
        v = Version(row["service_version"])
    except InvalidVersion as e:
        raise ServiceNotAvailable(f"Provider '{target_app}' has invalid version '{row['service_version']}'") from e

    if v not in spec:
        raise ServiceNotAvailable(
            f"Provider '{target_app}' version {row['service_version']} does not match '{version_specifier}'"
        )

    return target_app, row["local_port"], row["service_version"], row["endpoint"]
