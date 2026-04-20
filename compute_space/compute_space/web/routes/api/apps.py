import asyncio
import dataclasses
import os
import re
import shutil
import stat
import threading

from quart import Blueprint
from quart import jsonify
from quart import redirect
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.apps import app_log_path
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import git_pull
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import reload_app_background
from compute_space.core.apps import start_app_process
from compute_space.core.apps import validate_manifest
from compute_space.core.containers import BUILD_CACHE_CORRUPT_MARKER
from compute_space.core.containers import get_docker_logs
from compute_space.core.containers import remove_image
from compute_space.core.containers import stop_app_process
from compute_space.core.containers import stop_container
from compute_space.core.data import deprovision_data
from compute_space.core.data import deprovision_temp_data
from compute_space.core.logging import logger
from compute_space.core.manifest import parse_manifest
from compute_space.core.ports import check_port_available
from compute_space.core.services import OAuthAuthorizationRequired
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services import get_oauth_token
from compute_space.db import get_db
from compute_space.web.middleware import login_required


def _rmtree_force(path: str) -> None:
    """Remove a directory tree, making entries writable if needed.

    Git clones may checkout read-only files which block ``shutil.rmtree``;
    the onexc hook makes them writable and retries.  Under rootless podman
    with idmapped mounts, container-written files end up owned by the host
    ``host`` user already, so no privileged fallback (sudo / throwaway
    container) is necessary.
    """

    def _make_writable_and_retry(func, err_path, _exc):  # type: ignore[no-untyped-def]
        os.chmod(err_path, stat.S_IRWXU)
        func(err_path)

    shutil.rmtree(path, onexc=_make_writable_and_retry)


api_apps_bp = Blueprint("api_apps", __name__)


@api_apps_bp.route("/api/clone_and_get_app_info", methods=["POST"])
@login_required
async def clone_and_get_app_info() -> ResponseReturnValue:
    """Clone a repo and return its manifest info + temp clone dir."""
    form = await request.form
    repo_url = form.get("repo_url", "").strip()
    if not repo_url:
        return jsonify({"error": "No repository URL provided"}), 400

    config = get_config()
    add_app_url = f"//{config.zone_domain}{url_for('apps.add_app')}?repo={repo_url}"
    manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to=add_app_url)

    if authorize_url:
        return jsonify({"authorize_url": authorize_url}), 401

    if error:
        return jsonify({"error": error}), 400

    assert manifest is not None
    db = get_db()
    validation_error = validate_manifest(manifest, db)
    info = dataclasses.asdict(manifest)
    info.pop("raw_toml", None)
    return jsonify(
        {
            "manifest": info,
            "clone_dir": clone_dir,
            "app_name": manifest.name,
            **({"validation_error": validation_error} if validation_error else {}),
        }
    )


@api_apps_bp.route("/api/check_port")
@login_required
def check_port() -> ResponseReturnValue:
    """Check if a host port is available. Returns {port, available, used_by}."""
    port_str = request.args.get("port", "")
    try:
        port = int(port_str)
    except (ValueError, TypeError):
        return jsonify({"error": "port must be an integer"}), 400
    if port < 1 or port > 65535:
        return jsonify({"error": "port must be 1-65535"}), 400

    db = get_db()
    available, used_by = check_port_available(port, db)
    result: dict[str, object] = {"port": port, "available": available, "used_by": used_by}
    return jsonify(result)


@api_apps_bp.route("/api/add_app", methods=["POST"])
@login_required
async def api_add_app() -> ResponseReturnValue:
    """Install an app. Optionally takes a clone_dir from a prior clone_and_get_app_info call."""
    config = get_config()
    form = await request.form
    repo_url = form.get("repo_url", "").strip()
    app_name = form.get("app_name", "").strip() or None
    clone_dir = form.get("clone_dir", "").strip() or None
    grant_permissions_raw = form.get("grant_permissions")

    if not repo_url:
        return jsonify({"error": "No repository URL provided"}), 400

    # Clone if no existing clone_dir provided
    manifest = None
    if not clone_dir or not os.path.isdir(clone_dir):
        manifest, clone_dir, error, authorize_url = await clone_with_github_fallback(repo_url, return_to="/")
        if authorize_url:
            return jsonify(
                {
                    "error": "GitHub authorization required",
                    "authorize_url": authorize_url,
                }
            ), 401
        if error:
            return jsonify({"error": error}), 400

    assert clone_dir is not None
    if manifest is None:
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if app_name is None:
        app_name = manifest.name

    db = get_db()
    validation_error = validate_manifest(manifest, db, app_name=app_name)
    if validation_error:
        shutil.rmtree(clone_dir, ignore_errors=True)
        return jsonify({"error": validation_error}), 400

    final_dir = os.path.join(config.temporary_data_dir, "app_temp_data", app_name, "repo")
    if os.path.exists(final_dir):
        _rmtree_force(final_dir)
    os.makedirs(os.path.dirname(final_dir), exist_ok=True)
    shutil.move(clone_dir, final_dir)

    if grant_permissions_raw is None:
        logger.warning("add_app called without grant_permissions field")
        grant_permissions: set[str] = set()
    else:
        grant_permissions = {k.strip() for k in grant_permissions_raw.split(",") if k.strip()}

    # Parse port overrides from individual form fields: port_override.<label>=<host_port>
    port_overrides: dict[str, int] | None = None
    for key in form:
        if key.startswith("port_override."):
            label = key.removeprefix("port_override.")
            try:
                port_overrides = port_overrides or {}
                port_overrides[label] = int(form[key])
            except ValueError:
                return jsonify({"error": f"Invalid port override value for '{label}': {form[key]}"}), 400

    try:
        app_name = insert_and_deploy(
            manifest,
            final_dir,
            config,
            db,
            grant_permissions=grant_permissions,
            app_name=app_name,
            repo_url=repo_url,
            port_overrides=port_overrides,
        )
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400

    return jsonify({"ok": True, "app_name": app_name, "status": "building"})


@api_apps_bp.route("/api/apps")
@login_required
def api_apps() -> ResponseReturnValue:
    db = get_db()
    rows = db.execute("SELECT * FROM apps ORDER BY name").fetchall()
    apps: dict[str, dict[str, str | None]] = {}
    for row in rows:
        apps[row["name"]] = {
            "status": row["status"],
            "error_message": row["error_message"],
        }
    return jsonify(apps)


@api_apps_bp.route("/api/app_status/<app_name>")
@login_required
def app_status(app_name: str) -> ResponseReturnValue:
    db = get_db()
    app_row = db.execute("SELECT status, error_message FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return jsonify({"error": "not found"}), 404
    error_msg = app_row["error_message"]
    error_kind = None
    if error_msg and BUILD_CACHE_CORRUPT_MARKER in error_msg:
        error_kind = "build_cache_corrupt"
        error_msg = "Container build cache is corrupted."
    return jsonify({"status": app_row["status"], "error": error_msg, "error_kind": error_kind})


@api_apps_bp.route("/app_logs/<app_name>")
@login_required
def app_logs(app_name: str) -> ResponseReturnValue:
    config = get_config()
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return "App not found", 404
    logs = get_docker_logs(app_name, config.temporary_data_dir, app_row["container_id"])
    return logs, 200, {"Content-Type": "text/plain; charset=utf-8"}


@api_apps_bp.route("/stop_app/<app_name>", methods=["POST"])
@login_required
def stop_app(app_name: str) -> ResponseReturnValue:
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return jsonify({"error": "App not found"}), 404

    stop_app_process(app_row)
    stop_container(f"openhost-{app_name}")
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE name = ?",
        (app_name,),
    )
    db.commit()
    return jsonify({"ok": True})


@api_apps_bp.route("/reload_app/<app_name>", methods=["GET", "POST"])
@login_required
async def reload_app(app_name: str) -> ResponseReturnValue:
    config = get_config()
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return jsonify({"error": "App not found"}), 404

    form = await request.form if request.method == "POST" else {}
    update = form.get("update") == "1"
    continue_oauth = request.args.get("continue_oauth_update") == "1"

    log_file = app_log_path(app_name, config)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not continue_oauth:
        open(log_file, "w").close()  # truncate

    with open(log_file, "a") as lf:
        if not continue_oauth:
            lf.write(f"reloading app (update={update})\n")
        else:
            lf.write("continuing app reload after oauth\n")

        if update or continue_oauth:
            if not app_row["repo_path"] or not os.path.isdir(os.path.join(app_row["repo_path"], ".git")):
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                    (
                        "No git repository found to update. If this is a builtin app, git-based updates are not possible.",
                        app_name,
                    ),
                )
                db.commit()
                return jsonify({"ok": True})

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
                return_to = f"//{config.zone_domain}{url_for('api_apps.reload_app', app_name=app_name, continue_oauth_update='1')}"
                try:
                    token = await get_oauth_token("github", ["repo"], return_to=return_to)
                except ServiceNotAvailable as e:
                    lf.write(f"Secrets service unavailable: {e.message}\n")
                    db.execute(
                        "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                        (e.message, app_name),
                    )
                    db.commit()
                    if continue_oauth:
                        return redirect(url_for("apps.app_detail", app_name=app_name))
                    return jsonify({"ok": True})
                except OAuthAuthorizationRequired as e:
                    lf.write("No token available; redirecting to oauth flow\n")
                    return redirect(e.authorize_url)
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
                    "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                    (f"Git pull failed: {pull_err}", app_name),
                )
                db.commit()
                if continue_oauth:
                    return redirect(url_for("apps.app_detail", app_name=app_name))
                return jsonify({"ok": True})

    await asyncio.to_thread(stop_app_process, app_row)
    db.execute(
        "UPDATE apps SET status = 'building', container_id = NULL, error_message = NULL WHERE name = ?",
        (app_name,),
    )
    db.commit()

    threading.Thread(
        target=reload_app_background,
        args=(app_name, app_row["repo_path"], config),
        daemon=True,
    ).start()

    return jsonify({"ok": True})


@api_apps_bp.route("/remove_app/<app_name>", methods=["POST"])
@login_required
async def remove_app(app_name: str) -> ResponseReturnValue:
    config = get_config()
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return jsonify({"error": "App not found"}), 404

    form = await request.form
    keep_data = form.get("keep_data") == "1"

    await asyncio.to_thread(stop_app_process, app_row)
    await asyncio.to_thread(remove_image, app_row["name"])

    try:
        if keep_data:
            await asyncio.to_thread(deprovision_temp_data, app_name, config.temporary_data_dir)
        else:
            await asyncio.to_thread(deprovision_data, app_name, config.persistent_data_dir, config.temporary_data_dir)
    except Exception as e:
        logger.warning("Failed to deprovision data for %s: %s", app_name, e)

    db.execute("DELETE FROM apps WHERE name = ?", (app_name,))
    db.execute("DELETE FROM app_databases WHERE app_name = ?", (app_name,))
    db.commit()

    return jsonify({"ok": True})


@api_apps_bp.route("/rename_app/<app_name>", methods=["POST"])
@login_required
async def rename_app(app_name: str) -> ResponseReturnValue:
    """Rename an app (changes its unique ID and subdomain)."""
    config = get_config()
    form = await request.form
    new_name = form.get("name", "").strip()

    if not new_name:
        return jsonify({"error": "Name is required"}), 400

    if not re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", new_name):
        return jsonify({"error": "Name must be lowercase alphanumeric (hyphens allowed, not at start/end)"}), 400

    if f"/{new_name}" in RESERVED_PATHS:
        return jsonify({"error": f"Name '{new_name}' conflicts with a reserved path"}), 400

    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return jsonify({"error": "App not found"}), 404

    if new_name == app_name:
        return jsonify({"ok": True, "name": new_name})

    conflict = db.execute("SELECT name FROM apps WHERE name = ?", (new_name,)).fetchone()
    if conflict:
        return jsonify({"error": f"Name already in use by '{conflict['name']}'"}), 409

    was_running = app_row["status"] in ("running", "starting", "building")
    stop_app_process(app_row)
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE name = ?",
        (app_name,),
    )
    db.commit()

    for parent in [
        os.path.join(config.persistent_data_dir, "app_data"),
        os.path.join(config.temporary_data_dir, "app_temp_data"),
    ]:
        old_dir = os.path.join(parent, app_name)
        new_dir = os.path.join(parent, new_name)
        if os.path.exists(old_dir) and not os.path.exists(new_dir):
            os.rename(old_dir, new_dir)

    db.execute("PRAGMA foreign_keys=OFF")
    db.execute(
        "UPDATE apps SET name = ?, repo_path = REPLACE(repo_path, ?, ?) WHERE name = ?",
        (new_name, f"/{app_name}/", f"/{new_name}/", app_name),
    )
    db.execute("UPDATE app_databases SET app_name = ? WHERE app_name = ?", (new_name, app_name))
    db.execute(
        "UPDATE app_tokens SET app_name = ? WHERE app_name = ?",
        (new_name, app_name),
    )
    db.execute(
        "UPDATE service_providers SET app_name = ? WHERE app_name = ?",
        (new_name, app_name),
    )
    db.execute(
        "UPDATE permissions SET consumer_app = ? WHERE consumer_app = ?",
        (new_name, app_name),
    )
    db.execute(
        "UPDATE app_port_mappings SET app_name = ? WHERE app_name = ?",
        (new_name, app_name),
    )
    db.execute(
        "UPDATE app_databases SET db_path = REPLACE(db_path, ?, ?) WHERE app_name = ?",
        (f"/{app_name}/", f"/{new_name}/", new_name),
    )
    db.commit()
    db.execute("PRAGMA foreign_keys=ON")

    if was_running:
        start_app_process(new_name, db, config)

    return jsonify({"ok": True, "name": new_name})
