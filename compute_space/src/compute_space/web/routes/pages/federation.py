import sqlite3
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from litestar import Request
from litestar import Router
from litestar import get
from litestar.response import Template

from compute_space.config import Config
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.web.auth.auth import require_owner_auth
from compute_space.web.routes.pages.apps import CATALOG_APP_NAME


@get("/redirect/federation/connect", guards=[require_owner_auth])
async def federation_connect(
    request: Request[Any, Any, Any],
    db: sqlite3.Connection,
    config: Config,
    spec: str,
    source: str = "",
) -> Template:
    """Owner-facing page: route a federation invite to an installed app that serves ``spec``.

    Lives under the zone's ``/redirect/`` namespace: the shared my.* redirect domain forwards
    ``/redirect/...`` deep links verbatim, so published links can only ever land on pages under
    this reserved, side-effect-free prefix — never on arbitrary zone or app pages.

    GET-only and side-effect free: the page just lists matching apps; clicking through to an
    app's own connect page is the confirmation, and any mutation happens there via an
    authenticated POST.
    """
    proto = "https" if config.tls_enabled else "http"
    # The full original query string is passed through verbatim so the app's connect page
    # receives every invite parameter (source, secrets, etc.) without us enumerating them.
    query = request.url.query

    matching_apps: list[dict[str, str]] = []
    for row in db.execute("SELECT name, manifest_raw FROM apps ORDER BY name").fetchall():
        if not row["manifest_raw"]:
            continue
        try:
            manifest = parse_manifest_from_string(row["manifest_raw"])
        except Exception:
            logger.opt(exception=True).warning("Skipping unparseable manifest for app %s", row["name"])
            continue
        if manifest.federation_url == spec:
            connect_url = f"{proto}://{row['name']}.{config.zone_domain}{manifest.federation_connect_path}?{query}"
            matching_apps.append({"name": row["name"], "connect_url": connect_url})

    catalog_url = None
    if not matching_apps:
        catalog_installed = db.execute("SELECT 1 FROM apps WHERE name = ?", (CATALOG_APP_NAME,)).fetchone() is not None
        if catalog_installed:
            catalog_url = f"{proto}://{CATALOG_APP_NAME}.{config.zone_domain}/?federation_url={quote(spec, safe='')}"

    return Template(
        template_name="federation_connect.html",
        context={
            "spec": spec,
            "source_host": urlparse(source).netloc or source,
            "matching_apps": matching_apps,
            "catalog_url": catalog_url,
        },
    )


pages_federation_routes = Router(path="/", route_handlers=[federation_connect])
