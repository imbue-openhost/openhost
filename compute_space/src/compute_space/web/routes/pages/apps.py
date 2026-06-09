import json
import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from litestar import Router
from litestar import get
from litestar.exceptions import HTTPException
from litestar.response import Template

from compute_space.config import Config
from compute_space.core.app_id import is_valid_app_name
from compute_space.core.apps import list_builtin_apps
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.containers import get_docker_logs
from compute_space.core.git_ops import get_head_sha
from compute_space.core.git_ops import parse_repo_url
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest_from_string
from compute_space.core.services_v2 import ServiceNotAvailable
from compute_space.core.services_v2 import resolve_provider
from compute_space.web.auth.auth import require_owner_auth

EDIT_APP_SERVICE_URL = "github.com/imbue-openhost/claude-code-container/services/open-workspace"
EDIT_APP_VERSION_SPEC = "<1.0"


@get(["/", "/dashboard"], guards=[require_owner_auth])
async def dashboard(db: sqlite3.Connection) -> Template:
    apps_list = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    return Template(template_name="dashboard.html", context={"apps": apps_list})


@get("/app_detail/{app_name:str}", guards=[require_owner_auth])
async def app_detail(app_name: str, db: sqlite3.Connection, config: Config, next: str = "") -> Template:
    if not is_valid_app_name(app_name):
        raise HTTPException(detail="Invalid app name", status_code=400)
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        raise HTTPException(detail="App not found", status_code=404)
    app_id = app_row["app_id"]
    databases = db.execute("SELECT * FROM app_databases WHERE app_id = ?", (app_id,)).fetchall()
    port_mappings = db.execute(
        "SELECT label, container_port, host_port FROM app_port_mappings WHERE app_id = ? ORDER BY label",
        (app_id,),
    ).fetchall()
    services_provided = db.execute(
        "SELECT service_url, service_version FROM service_providers_v2 WHERE app_id = ? ORDER BY service_url",
        (app_id,),
    ).fetchall()
    logs = get_docker_logs(app_name, config.temporary_data_dir, app_row["container_id"])

    # Permissions: granted + manifest-declared but not yet granted
    granted_perms = [
        {"service_url": p.service_url, "grant": p.grant, "scope": p.scope}
        for p in get_all_permissions_v2(consumer_app_id=app_id)
    ]
    ungranted_perms: list[dict[str, object]] = []
    manifest_raw = app_row["manifest_raw"]
    if manifest_raw:
        try:
            manifest = parse_manifest_from_string(manifest_raw)
            granted_set = {(p["service_url"], json.dumps(p["grant"], sort_keys=True)) for p in granted_perms}
            for consume in manifest.consumes_services_v2:
                for grant_payload in consume.grants:
                    key = (consume.service, json.dumps(grant_payload, sort_keys=True))
                    if key not in granted_set:
                        ungranted_perms.append(
                            {
                                "service_url": consume.service,
                                "grant": grant_payload,
                                "shortname": consume.shortname,
                            }
                        )
        except Exception:
            logger.opt(exception=True).warning("Failed to parse manifest for permission display (app %s)", app_id)

    edit_app = await _resolve_edit_app(app_row["repo_url"], app_row["repo_path"], db, config)

    return Template(
        template_name="app_detail.html",
        context={
            "app": app_row,
            "databases": databases,
            "port_mappings": port_mappings,
            "services_provided": services_provided,
            "logs": logs,
            "next_url": next,
            "granted_permissions": granted_perms,
            "ungranted_permissions": ungranted_perms,
            "edit_app": edit_app,
        },
    )


async def _resolve_edit_app(
    repo_url: str | None,
    repo_path: str,
    db: sqlite3.Connection,
    config: Config,
) -> dict[str, str] | None:
    """Describe an "Edit this app" affordance for the template.

    Returns one of:
      - ``{"mode": "service", "action": ..., "repo": ..., "ref": ...}`` — POST to
        a provider of the open-workspace service (see
        github.com/imbue-openhost/claude-code-container/services/open-workspace).
      - ``{"mode": "repo", "href": ...}`` — fallback link to the repo URL.
      - ``None`` — no actionable URL available.
    """
    if not repo_url:
        return None
    base_url, ref_from_url = parse_repo_url(repo_url)
    ref = ref_from_url
    if not ref:
        try:
            ref = await get_head_sha(Path(repo_path))
        except Exception:
            logger.opt(exception=True).warning("Failed to read HEAD sha for %s", repo_path)

    try:
        provider_app_id, _, _, endpoint = resolve_provider(EDIT_APP_SERVICE_URL, EDIT_APP_VERSION_SPEC, db)
    except ServiceNotAvailable:
        return {"mode": "repo", "href": base_url}

    if not ref:
        return {"mode": "repo", "href": base_url}

    provider_row = db.execute("SELECT name FROM apps WHERE app_id = ?", (provider_app_id,)).fetchone()
    if not provider_row:
        logger.warning("resolve_provider returned unknown app_id %s", provider_app_id)
        return {"mode": "repo", "href": base_url}

    proto = "https" if config.tls_enabled else "http"
    # Pass repo+ref in the query string too: the openhost router 302's
    # unauthenticated POSTs to /login, and the post-login redirect comes back
    # as a GET (only 307/308 preserve method), dropping the form body. Query
    # params survive the bounce, and the provider falls back to them.
    qs = urlencode({"repo": base_url, "ref": ref})
    action = f"{proto}://{provider_row['name']}.{config.zone_domain}{endpoint}?{qs}"
    return {"mode": "service", "action": action, "repo": base_url, "ref": ref}


@get("/add_app", guards=[require_owner_auth])
async def add_app(config: Config, repo: str = "", next: str = "") -> Template:
    return Template(
        template_name="add_app.html",
        context={
            "builtin_apps": list_builtin_apps(config),
            "initial_repo": repo,
            "next_url": next,
        },
    )


pages_apps_routes = Router(
    path="/",
    route_handlers=[dashboard, app_detail, add_app],
)
