import asyncio
import os
import re
import shutil
import sqlite3
import threading
from typing import Annotated
from typing import Any

import attr
from litestar import Request
from litestar import Response
from litestar import get
from litestar import post
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.app_id import is_valid_app_id
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.apps import app_log_path
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import git_pull
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import move_clone_to_app_temp_dir
from compute_space.core.apps import reload_app_background
from compute_space.core.apps import remove_app_background
from compute_space.core.apps import start_app_process
from compute_space.core.apps import validate_manifest
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import get_docker_logs
from compute_space.core.containers import stop_app_process
from compute_space.core.containers import stop_container
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest
from compute_space.core.ports import check_port_available
from compute_space.core.services import OAuthAuthorizationRequired
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_oauth_token
from compute_space.db import get_db


@attr.s(auto_attribs=True, frozen=True)
class CloneRepoForm:
    repo_url: str = ""


@attr.s(auto_attribs=True, frozen=True)
class AddAppForm:
    repo_url: str = ""
    app_name: str = ""
    clone_dir: str = ""
    grant_permissions: str = ""
    grant_permissions_v2: str = ""


@attr.s(auto_attribs=True, frozen=True)
class ReloadForm:
    update: str = ""


@attr.s(auto_attribs=True, frozen=True)
class RemoveForm:
    keep_data: str = ""


@attr.s(auto_attribs=True, frozen=True)
class RenameForm:
    name: str = ""


def _is_removing(app_row: sqlite3.Row | None) -> bool:
    return app_row is not None and app_row["status"] == "removing"


def _resolve_app_or_error(
    app_id: str,
) -> tuple[sqlite3.Row | None, Response[dict[str, Any]] | None]:
    if not is_valid_app_id(app_id):
        return None, Response(content={"error": "Invalid app_id"}, status_code=400)
    row = get_db().execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not row:
        return None, Response(content={"error": "App not found"}, status_code=404)
    return row, None


@post("/api/clone_and_get_app_info", status_code=200)
async def clone_and_get_app_info(
    data: Annotated[CloneRepoForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> Response[dict[str, Any]]:
    repo_url = (data.repo_url or "").strip()
    if not repo_url:
        return Response(content={"error": "No repository URL provided"}, status_code=400)

    config = get_config()
    add_app_url = f"//{config.zone_domain}/add_app?repo={repo_url}"
    manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to=add_app_url)

    if authorize_url:
        return Response(content={"authorize_url": authorize_url}, status_code=401)
    if error:
        return Response(content={"error": error}, status_code=400)
    if manifest is None:
        raise RuntimeError("manifest unexpectedly None after successful clone")
    db = get_db()
    validation_error = validate_manifest(manifest, db)
    info = attr.asdict(manifest)
    info.pop("raw_toml", None)
    return Response(
        content={
            "manifest": info,
            "clone_dir": clone_dir,
            "app_name": manifest.name,
            **({"validation_error": validation_error} if validation_error else {}),
        }
    )


@get("/api/check_port", sync_to_thread=False)
def check_port(user: dict[str, Any], port: str = "") -> Response[dict[str, Any]]:
    try:
        port_int = int(port)
    except (ValueError, TypeError):
        return Response(content={"error": "port must be an integer"}, status_code=400)
    if port_int < 1 or port_int > 65535:
        return Response(content={"error": "port must be 1-65535"}, status_code=400)
    db = get_db()
    available, used_by = check_port_available(port_int, db)
    return Response(content={"port": port_int, "available": available, "used_by": used_by})


@post("/api/add_app", status_code=200)
async def api_add_app(request: Request[Any, Any, Any], user: dict[str, Any]) -> Response[dict[str, Any]]:
    """Install an app. Optionally takes a clone_dir from a prior clone_and_get_app_info call.

    Reads the form directly so port_override.<label> dynamic keys are accessible.
    """
    config = get_config()
    form = await request.form()
    repo_url = (form.get("repo_url") or "").strip()
    app_name = (form.get("app_name") or "").strip() or None
    clone_dir = (form.get("clone_dir") or "").strip() or None
    grant_permissions_raw = form.get("grant_permissions")
    grant_permissions_v2 = (form.get("grant_permissions_v2") or "").lower() in ("1", "true", "yes")

    if not repo_url:
        return Response(content={"error": "No repository URL provided"}, status_code=400)

    manifest = None
    if not clone_dir or not os.path.isdir(clone_dir):
        manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to="/")
        if authorize_url:
            return Response(
                content={"error": "GitHub authorization required", "authorize_url": authorize_url},
                status_code=401,
            )
        if error:
            return Response(content={"error": error}, status_code=400)

    if clone_dir is None:
        raise RuntimeError("clone_dir unexpectedly None after successful clone")
    if manifest is None:
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            return Response(content={"error": str(e)}, status_code=400)

    if app_name is None:
        app_name = manifest.name

    db = get_db()
    validation_error = validate_manifest(manifest, db, app_name=app_name)
    if validation_error:
        shutil.rmtree(clone_dir, ignore_errors=True)
        return Response(content={"error": validation_error}, status_code=400)

    if manifest.app_archive:
        backend_state = archive_backend.read_state(db)
        if backend_state.backend != "s3":
            shutil.rmtree(clone_dir, ignore_errors=True)
            return Response(
                content={
                    "error": "This app uses the app_archive data tier, but "
                    "S3 archive storage has not been configured on "
                    "this zone.  Visit the System page to configure an "
                    "S3 backend before deploying this app."
                },
                status_code=400,
            )
        if not archive_backend.is_archive_dir_healthy(config, db):
            shutil.rmtree(clone_dir, ignore_errors=True)
            return Response(
                content={
                    "error": "Archive backend is not healthy; refusing to deploy "
                    "an archive-using app until the JuiceFS mount is live "
                    "again (see the dashboard's Archive backend panel)."
                },
                status_code=503,
            )

    final_dir = move_clone_to_app_temp_dir(clone_dir, app_name, config)

    if grant_permissions_raw is None:
        logger.warning("add_app called without grant_permissions field")
        grant_permissions: set[str] = set()
    else:
        grant_permissions = {k.strip() for k in grant_permissions_raw.split(",") if k.strip()}

    port_overrides: dict[str, int] | None = None
    for key in form:
        if key.startswith("port_override."):
            label = key.removeprefix("port_override.")
            try:
                port_overrides = port_overrides or {}
                port_overrides[label] = int(form[key])
            except ValueError:
                return Response(
                    content={"error": f"Invalid port override value for '{label}': {form[key]}"},
                    status_code=400,
                )

    try:
        app_id = insert_and_deploy(
            manifest,
            final_dir,
            config,
            db,
            grant_permissions=grant_permissions,
            grant_permissions_v2=grant_permissions_v2,
            app_name=app_name,
            repo_url=repo_url,
            port_overrides=port_overrides,
        )
    except (RuntimeError, ValueError) as e:
        return Response(content={"error": str(e)}, status_code=400)

    return Response(content={"ok": True, "app_id": app_id, "app_name": app_name, "status": "building"})


@get("/api/apps", sync_to_thread=False)
def api_apps(user: dict[str, Any]) -> list[dict[str, Any]]:
    db = get_db()
    rows = db.execute("SELECT app_id, name, status, error_message FROM apps ORDER BY name").fetchall()
    return [
        {
            "app_id": row["app_id"],
            "name": row["name"],
            "status": row["status"],
            "error_message": row["error_message"],
        }
        for row in rows
    ]


@get("/api/app_status/{app_id:str}", sync_to_thread=False)
def app_status(app_id: str, user: dict[str, Any]) -> Response[dict[str, Any]]:
    if not is_valid_app_id(app_id):
        return Response(content={"error": "Invalid app_id"}, status_code=400)
    db = get_db()
    app_row = db.execute("SELECT status, error_message FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if not app_row:
        return Response(content={"error": "not found"}, status_code=404)
    error_msg = app_row["error_message"]
    error_kind = None
    if error_msg and (BUILD_CACHE_CORRUPT_MARKER in error_msg or "[CACHE_CORRUPT]" in error_msg):
        error_kind = "build_cache_corrupt"
        error_msg = "Container build cache is corrupted."
    return Response(content={"status": app_row["status"], "error": error_msg, "error_kind": error_kind})


@get("/app_logs/{app_id:str}", sync_to_thread=False)
def app_logs(app_id: str, user: dict[str, Any]) -> Response[bytes]:
    config = get_config()
    app_row, err = _resolve_app_or_error(app_id)
    if err is not None:
        return err  # type: ignore[return-value]
    assert app_row is not None
    logs = get_docker_logs(app_row["name"], config.temporary_data_dir, app_row["container_id"])
    return Response(content=logs.encode(), media_type="text/plain; charset=utf-8")


@post("/stop_app/{app_id:str}", sync_to_thread=False, status_code=200)
def stop_app(app_id: str, user: dict[str, Any]) -> Response[dict[str, Any]]:
    app_row, err = _resolve_app_or_error(app_id)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content={"error": "App is being removed"}, status_code=409)
    db = get_db()
    stop_app_process(app_row)
    stop_container(f"openhost-{app_row['name']}")
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()
    return Response(content={"ok": True})


async def _reload_app_impl(request: Request[Any, Any, Any], app_id: str) -> Any:
    """Reload an app, optionally pulling git updates first."""
    config = get_config()
    db = get_db()
    app_row, err = _resolve_app_or_error(app_id)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content={"error": "App is being removed"}, status_code=409)
    app_name = app_row["name"]

    if not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_requires_archive(app_row["manifest_raw"] or ""):
            return Response(
                content={
                    "error": "Archive backend is not healthy; refusing to "
                    "reload an app that requires app_archive until "
                    "the operator-configured archive mount is live "
                    "again (see the dashboard's Archive backend panel)."
                },
                status_code=503,
            )

    update = False
    if request.method == "POST":
        form = await request.form()
        update = (form.get("update") or "") == "1"
    continue_oauth = request.query_params.get("continue_oauth_update") == "1"

    log_file = app_log_path(app_name, config)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not continue_oauth:
        open(log_file, "w").close()

    with open(log_file, "a") as lf:
        if not continue_oauth:
            lf.write(f"reloading app (update={update})\n")
        else:
            lf.write("continuing app reload after oauth\n")

        if update or continue_oauth:
            if not app_row["repo_path"] or not os.path.isdir(os.path.join(app_row["repo_path"], ".git")):
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (
                        "No git repository found to update. If this is a builtin app, git-based updates are not possible.",
                        app_id,
                    ),
                )
                db.commit()
                return Response(content={"ok": True})

            repo_url = app_row["repo_url"] or ""
            pull_ok = False
            pull_err = None

            if not continue_oauth:
                lf.write("Attempting git pull without github oauth\n")
                lf.flush()
                pull_ok, pull_err = await asyncio.to_thread(
                    git_pull,
                    app_row["repo_path"],
                    app_name,
                    log_file=log_file,
                    repo_url=repo_url,
                )

            if not pull_ok and "github.com" in repo_url:
                lf.write("Attempting git pull with github oauth\n")
                lf.flush()
                return_to = f"//{config.zone_domain}/reload_app/{app_id}?continue_oauth_update=1"
                try:
                    token = await get_oauth_token("github", ["repo"], return_to=return_to)
                except ServiceNotAvailable as e:
                    lf.write(f"Secrets service unavailable: {e.message}\n")
                    db.execute(
                        "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                        (e.message, app_id),
                    )
                    db.commit()
                    if continue_oauth:
                        return Redirect(path=f"/app_detail/{app_id}")
                    return Response(content={"ok": True})
                except OAuthAuthorizationRequired as e:
                    lf.write("No token available; redirecting to oauth flow\n")
                    return Redirect(path=e.authorize_url)
                lf.flush()
                pull_ok, pull_err = await asyncio.to_thread(
                    git_pull,
                    app_row["repo_path"],
                    app_name,
                    github_token=token,
                    log_file=log_file,
                    repo_url=repo_url,
                )

            if not pull_ok:
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (f"Git pull failed: {pull_err}", app_id),
                )
                db.commit()
                if continue_oauth:
                    return Redirect(path=f"/app_detail/{app_id}")
                return Response(content={"ok": True})

    await asyncio.to_thread(stop_app_process, app_row)
    db.execute(
        "UPDATE apps SET status = 'building', container_id = NULL, error_message = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    threading.Thread(
        target=reload_app_background,
        args=(app_id, app_row["repo_path"], config),
        daemon=True,
    ).start()

    return Response(content={"ok": True})


@post("/reload_app/{app_id:str}", status_code=200)
async def reload_app(request: Request[Any, Any, Any], app_id: str, user: dict[str, Any]) -> Any:
    return await _reload_app_impl(request, app_id)


@get("/reload_app/{app_id:str}")
async def reload_app_get(request: Request[Any, Any, Any], app_id: str, user: dict[str, Any]) -> Any:
    return await _reload_app_impl(request, app_id)


@post("/remove_app/{app_id:str}", status_code=202)
async def remove_app(
    app_id: str,
    data: Annotated[RemoveForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> Response[dict[str, Any]]:
    config = get_config()
    app_row, err = _resolve_app_or_error(app_id)
    if err is not None:
        return err
    assert app_row is not None
    db = get_db()
    keep_data = (data.keep_data or "") == "1"

    if not keep_data and not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
            return Response(
                content={
                    "error": "Archive backend is not healthy; refusing to "
                    "remove an archive-using app's data because the "
                    "S3-side bytes wouldn't actually be deleted.  "
                    "Either restore the archive mount and retry, or "
                    "use keep_data=1 to remove the app while leaving "
                    "its data in place."
                },
                status_code=503,
            )

    cursor = db.execute(
        "UPDATE apps SET status = 'removing', error_message = NULL WHERE app_id = ? AND status != 'removing'",
        (app_id,),
    )
    db.commit()

    if cursor.rowcount == 0:
        return Response(content={"ok": True, "already_removing": True}, status_code=202)

    try:
        threading.Thread(
            target=remove_app_background,
            args=(app_id, keep_data, config),
            daemon=True,
        ).start()
    except Exception as e:
        logger.exception("Could not spawn remove worker for %s", app_id)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
            (f"Could not start removal worker: {e}", app_id),
        )
        db.commit()
        return Response(content={"error": "Could not start removal worker; try again."}, status_code=503)

    return Response(content={"ok": True}, status_code=202)


def _rename_app_storage_dirs(config: Config, old_name: str, new_name: str) -> str | None:
    rename_parents = [
        os.path.join(config.persistent_data_dir, "app_data"),
        os.path.join(config.temporary_data_dir, "app_temp_data"),
    ]
    for parent in rename_parents:
        if not os.path.isdir(parent):
            return f"Storage parent {parent!r} is not a directory; refusing to rename so per-app data isn't orphaned."
    if os.path.isdir(config.app_archive_dir):
        rename_parents.append(config.app_archive_dir)
    renamed: list[tuple[str, str]] = []
    try:
        for parent in rename_parents:
            old_dir = os.path.join(parent, old_name)
            new_dir = os.path.join(parent, new_name)
            if os.path.exists(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
                renamed.append((old_dir, new_dir))
    except OSError as exc:
        for old_dir, new_dir in reversed(renamed):
            try:
                os.rename(new_dir, old_dir)
            except OSError as rollback_exc:
                logger.error(
                    "Rollback of partial rename %s -> %s failed: %s",
                    new_dir,
                    old_dir,
                    rollback_exc,
                )
        return str(exc)
    return None


@post("/rename_app/{app_id:str}", status_code=200)
async def rename_app(
    app_id: str,
    data: Annotated[RenameForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> Response[dict[str, Any]]:
    config = get_config()
    new_name = (data.name or "").strip()

    if not new_name:
        return Response(content={"error": "Name is required"}, status_code=400)
    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", new_name):
        return Response(
            content={"error": "Name must be lowercase alphanumeric (hyphens allowed, not at start/end)"},
            status_code=400,
        )
    if f"/{new_name}" in RESERVED_PATHS:
        return Response(
            content={"error": f"Name '{new_name}' conflicts with a reserved path"},
            status_code=400,
        )

    db = get_db()
    app_row, err = _resolve_app_or_error(app_id)
    if err is not None:
        return err
    assert app_row is not None
    if _is_removing(app_row):
        return Response(content={"error": "App is being removed"}, status_code=409)
    old_name = app_row["name"]

    if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
        if not archive_backend.is_archive_dir_healthy(config, db):
            return Response(
                content={
                    "error": "Archive backend is not healthy; refusing to rename "
                    "an archive-using app until the JuiceFS mount is live "
                    "again (see the dashboard's Archive backend panel)."
                },
                status_code=503,
            )

    if new_name == old_name:
        return Response(content={"ok": True, "name": new_name})

    conflict = db.execute("SELECT name FROM apps WHERE name = ?", (new_name,)).fetchone()
    if conflict:
        return Response(content={"error": f"Name already in use by '{conflict['name']}'"}, status_code=409)

    prior_status = app_row["status"]
    prior_container_id = app_row["container_id"]
    was_running = prior_status in ("running", "starting", "building")
    stop_app_process(app_row)
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    rename_error = await asyncio.to_thread(_rename_app_storage_dirs, config, old_name, new_name)
    if rename_error is not None:
        rollback_db_error: str | None = None
        try:
            db.execute(
                "UPDATE apps SET status = ?, container_id = ? WHERE app_id = ?",
                (prior_status, prior_container_id, app_id),
            )
            db.commit()
        except sqlite3.Error as db_exc:
            logger.error("Failed to restore status during rename rollback: %s", db_exc)
            rollback_db_error = str(db_exc)
        error_message = f"Failed to rename app data directories: {rename_error}"
        if rollback_db_error is not None:
            error_message += (
                f"; additionally, the DB-status rollback failed: "
                f"{rollback_db_error}.  Check the apps table; the row "
                f"for app_id={app_id!r} may be stuck at status='stopped'."
            )
        return Response(content={"error": error_message}, status_code=500)

    db.execute(
        "UPDATE apps SET name = ?, repo_path = REPLACE(repo_path, ?, ?) WHERE app_id = ?",
        (new_name, f"/{old_name}/", f"/{new_name}/", app_id),
    )
    db.execute(
        "UPDATE app_databases SET db_path = REPLACE(db_path, ?, ?) WHERE app_id = ?",
        (f"/{old_name}/", f"/{new_name}/", app_id),
    )
    db.commit()

    if was_running:
        try:
            start_app_process(app_id, db, config)
        except (RuntimeError, ValueError) as e:
            logger.warning("Failed to restart %s after rename: %s", new_name, e)
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                (str(e), app_id),
            )
            db.commit()

    return Response(content={"ok": True, "name": new_name, "app_id": app_id})


api_apps_routes = [
    clone_and_get_app_info,
    check_port,
    api_add_app,
    api_apps,
    app_status,
    app_logs,
    stop_app,
    reload_app,
    reload_app_get,
    remove_app,
    rename_app,
]
