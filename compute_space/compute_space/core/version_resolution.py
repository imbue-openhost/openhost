"""V2 service version resolution: match consumer version specifiers to providers."""

import sqlite3

from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion
from packaging.version import Version

from compute_space.core.services import ServiceNotAvailable


def find_compatible_provider(
    service_url: str,
    version_specifier: str,
    db: sqlite3.Connection,
) -> tuple[str, int, str, str]:
    """Find a provider app whose version satisfies the specifier.

    Returns (app_name, local_port, version, endpoint).

    Resolution order:
    1. Filter providers by version compatibility
    2. If the service has a default provider (in service_defaults) and it's
       compatible, use it
    3. Otherwise pick the provider with the highest compatible version
    """
    try:
        spec = SpecifierSet(version_specifier)
    except InvalidSpecifier as e:
        raise ServiceNotAvailable(f"Invalid version specifier: {version_specifier}") from e

    rows = db.execute(
        """SELECT sp.service_url, sp.app_name, sp.version, sp.endpoint,
                  a.local_port, a.status
           FROM service_providers_v2 sp
           JOIN apps a ON a.name = sp.app_name
           WHERE sp.service_url = ?""",
        (service_url,),
    ).fetchall()

    if not rows:
        raise ServiceNotAvailable(f"No provider for service '{service_url}'")

    compatible: list[tuple[str, int, str, str, bool]] = []
    default_app = db.execute(
        "SELECT app_name FROM service_defaults WHERE service_url = ?",
        (service_url,),
    ).fetchone()
    default_app_name = default_app["app_name"] if default_app else None

    for row in rows:
        if row["status"] != "running":
            continue
        try:
            v = Version(row["version"])
        except InvalidVersion:
            continue
        if v not in spec:
            continue
        is_default = row["app_name"] == default_app_name
        compatible.append(
            (
                row["app_name"],
                row["local_port"],
                row["version"],
                row["endpoint"],
                is_default,
            )
        )

    if not compatible:
        running = any(r["status"] == "running" for r in rows)
        if not running:
            raise ServiceNotAvailable(f"Provider(s) for '{service_url}' exist but none are running")
        raise ServiceNotAvailable(f"No provider for '{service_url}' matches version '{version_specifier}'")

    # Prefer the default provider if it's compatible
    for entry in compatible:
        if entry[4]:
            return entry[0], entry[1], entry[2], entry[3]

    # Otherwise pick highest version
    compatible.sort(key=lambda e: Version(e[2]), reverse=True)
    best = compatible[0]
    return best[0], best[1], best[2], best[3]
