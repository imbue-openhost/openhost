import asyncio
import os
import re
import shutil
import sqlite3
import threading

import attr
from quart import Blueprint
from quart import jsonify
from quart import redirect
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue

from compute_space.config import Config
from compute_space.config import get_config
from compute_space.core import archive_backend
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
from compute_space.web.middleware import login_required

# Router-level permissions an app may request via ``[permissions]`` in
# its ``openhost.toml`` and the owner may grant at install time.
# Adding entries here is the manifest contract; the runtime token
# issuance + API gating that *enforce* them lands in a follow-up PR.
KNOWN_ROUTER_PERMISSIONS: frozenset[str] = frozenset({"deploy_apps"})


def _is_removing(app_row: sqlite3.Row | None) -> bool:
    """True if the row is being torn down by remove_app_background.

    Mutating routes (stop, reload, rename) refuse to touch a removing
    row with 409. /remove_app itself uses an atomic UPDATE...WHERE
    status != 'removing' instead of this helper to avoid a TOCTOU race
    on concurrent removal requests.
    """
    return app_row is not None and app_row["status"] == "removing"


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

    if manifest is None:
        raise RuntimeError("manifest unexpectedly None after successful clone")
    db = get_db()
    validation_error = validate_manifest(manifest, db)
    info = attr.asdict(manifest)
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
    grant_permissions_v2 = form.get("grant_permissions_v2", "").lower() in ("1", "true", "yes")

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

    if clone_dir is None:
        raise RuntimeError("clone_dir unexpectedly None after successful clone")
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

    # 400 when the operator hasn't configured S3 (action: visit the System
    # page); 503 when configured-but-unhealthy (action: retry transient).
    if manifest.app_archive:
        backend_state = archive_backend.read_state(db)
        if backend_state.backend != "s3":
            shutil.rmtree(clone_dir, ignore_errors=True)
            return jsonify(
                {
                    "error": "This app uses the app_archive data tier, but "
                    "S3 archive storage has not been configured on "
                    "this zone.  Visit the System page to configure an "
                    "S3 backend before deploying this app."
                }
            ), 400
        if not archive_backend.is_archive_dir_healthy(config, db):
            shutil.rmtree(clone_dir, ignore_errors=True)
            return jsonify(
                {
                    "error": "Archive backend is not healthy; refusing to deploy "
                    "an archive-using app until the JuiceFS mount is live "
                    "again (see the dashboard's Archive backend panel)."
                }
            ), 503

    final_dir = move_clone_to_app_temp_dir(clone_dir, app_name, config)

    if grant_permissions_raw is None:
        logger.warning("add_app called without grant_permissions field")
        grant_permissions: set[str] = set()
    else:
        grant_permissions = {k.strip() for k in grant_permissions_raw.split(",") if k.strip()}

    # Privileged router permissions are per-perm form fields shaped
    # ``grant_router_permission.<name>=1``.  We accumulate the set of
    # granted names and validate against ``KNOWN_ROUTER_PERMISSIONS`` so a
    # client typo or a future-version client doesn't accidentally grant
    # something we don't know how to enforce.
    granted_router_permissions: set[str] = set()
    for key in form:
        if not key.startswith("grant_router_permission."):
            continue
        perm_name = key.removeprefix("grant_router_permission.")
        if perm_name not in KNOWN_ROUTER_PERMISSIONS:
            return jsonify({"error": f"Unknown router permission: {perm_name!r}"}), 400
        if (form.get(key) or "").strip().lower() not in ("1", "true", "yes"):
            continue
        granted_router_permissions.add(perm_name)

    # Refuse grants the manifest doesn't request — the consent UI never
    # offers them, but a hand-rolled API client could try.  Fail closed.
    if "deploy_apps" in granted_router_permissions and not manifest.deploy_apps_permission:
        shutil.rmtree(final_dir, ignore_errors=True)
        return jsonify(
            {"error": "deploy_apps router permission was granted but the manifest does not request it"}
        ), 400

    deploy_apps_granted = "deploy_apps" in granted_router_permissions

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
            grant_permissions_v2=grant_permissions_v2,
            app_name=app_name,
            repo_url=repo_url,
            port_overrides=port_overrides,
            deploy_apps_granted=deploy_apps_granted,
        )
    except (RuntimeError, ValueError) as e:
        # ValueError covers uid_map pool exhaustion (see compute_uid_map_base)
        # and other manifest-validation errors raised at insert time; both
        # map to a 400 rather than a 500.
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
    # error_message may carry either the current BUILD_CACHE_CORRUPT_MARKER
    # or the legacy ``[CACHE_CORRUPT]`` marker; both trigger the same
    # 'drop cache and rebuild' remediation in the UI.
    if error_msg and (BUILD_CACHE_CORRUPT_MARKER in error_msg or "[CACHE_CORRUPT]" in error_msg):
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
    if _is_removing(app_row):
        return jsonify({"error": "App is being removed"}), 409

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
    if _is_removing(app_row):
        return jsonify({"error": "App is being removed"}), 409

    if not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_requires_archive(app_row["manifest_raw"] or ""):
            return jsonify(
                {
                    "error": "Archive backend is not healthy; refusing to "
                    "reload an app that requires app_archive until "
                    "the operator-configured archive mount is live "
                    "again (see the dashboard's Archive backend panel)."
                }
            ), 503

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
    """Flip the row to ``status='removing'`` and run teardown in a thread.

    Returns 202 immediately. The dashboard's /api/apps poll picks up
    the new status and renders 'Removing…' until the row is deleted,
    so reloading the page or opening a second tab still shows the
    in-flight state.
    """
    config = get_config()
    db = get_db()
    app_row = db.execute("SELECT * FROM apps WHERE name = ?", (app_name,)).fetchone()
    if not app_row:
        return jsonify({"error": "App not found"}), 404

    form = await request.form
    keep_data = form.get("keep_data") == "1"

    # Refuse destructive remove on an unhealthy archive: rmtree of an
    # empty mountpoint would leave S3 bytes orphaned while the DB row
    # disappears. Must run before the atomic-claim UPDATE.
    if not keep_data and not archive_backend.is_archive_dir_healthy(config, db):
        if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
            return jsonify(
                {
                    "error": "Archive backend is not healthy; refusing to "
                    "remove an archive-using app's data because the "
                    "S3-side bytes wouldn't actually be deleted.  "
                    "Either restore the archive mount and retry, or "
                    "use keep_data=1 to remove the app while leaving "
                    "its data in place."
                }
            ), 503

    # Atomic claim: ``WHERE status != 'removing'`` makes concurrent
    # POSTs safe — only the first one gets rowcount=1 and spawns a
    # worker; later ones short-circuit to the already_removing branch.
    cursor = db.execute(
        "UPDATE apps SET status = 'removing', error_message = NULL WHERE name = ? AND status != 'removing'",
        (app_name,),
    )
    db.commit()

    if cursor.rowcount == 0:
        return jsonify({"ok": True, "already_removing": True}), 202

    try:
        threading.Thread(
            target=remove_app_background,
            args=(app_name, keep_data, config),
            daemon=True,
        ).start()
    except Exception as e:
        # Thread spawn failed (resource exhaustion). Roll the row back
        # to 'error' so a retry can re-claim it via the atomic UPDATE.
        logger.exception("Could not spawn remove worker for %s", app_name)
        db.execute(
            "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
            (f"Could not start removal worker: {e}", app_name),
        )
        db.commit()
        return jsonify({"error": "Could not start removal worker; try again."}), 503

    return jsonify({"ok": True}), 202


def _rename_app_storage_dirs(config: Config, old_name: str, new_name: str) -> str | None:
    """Rename per-app subdirs across the three storage tiers, with rollback on partial failure.

    Returns ``None`` on success or an error message on failure. Sync helper
    so the blocking renames (which can be slow on JuiceFS) stay off the
    event loop.
    """
    rename_parents = [
        os.path.join(config.persistent_data_dir, "app_data"),
        os.path.join(config.temporary_data_dir, "app_temp_data"),
    ]
    for parent in rename_parents:
        if not os.path.isdir(parent):
            return f"Storage parent {parent!r} is not a directory; refusing to rename so per-app data isn't orphaned."
    # The archive parent only exists when the operator has configured S3 +
    # JuiceFS is mounted; skip it cleanly otherwise (apps with app_archive=true
    # are blocked from install on disabled zones, so this is harmless).
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
    if _is_removing(app_row):
        return jsonify({"error": "App is being removed"}), 409

    # Refuse rename on an unhealthy archive ONLY if this app actually
    # uses the archive — otherwise the archive subdir would be silently
    # skipped while other tiers are renamed, orphaning the archive
    # contents under the old name.  Apps that don't use the archive
    # tier are unaffected and can rename freely on any backend state.
    if archive_backend.manifest_uses_archive(app_row["manifest_raw"] or ""):
        if not archive_backend.is_archive_dir_healthy(config, db):
            return jsonify(
                {
                    "error": "Archive backend is not healthy; refusing to rename "
                    "an archive-using app until the JuiceFS mount is live "
                    "again (see the dashboard's Archive backend panel)."
                }
            ), 503

    if new_name == app_name:
        return jsonify({"ok": True, "name": new_name})

    conflict = db.execute("SELECT name FROM apps WHERE name = ?", (new_name,)).fetchone()
    if conflict:
        return jsonify({"error": f"Name already in use by '{conflict['name']}'"}), 409

    prior_status = app_row["status"]
    prior_container_id = app_row["container_id"]
    was_running = prior_status in ("running", "starting", "building")
    stop_app_process(app_row)
    db.execute(
        "UPDATE apps SET status = 'stopped', container_id = NULL WHERE name = ?",
        (app_name,),
    )
    db.commit()

    # Off-loop because JuiceFS renames can take hundreds of ms.
    rename_error = await asyncio.to_thread(
        _rename_app_storage_dirs,
        config,
        app_name,
        new_name,
    )
    if rename_error is not None:
        rollback_db_error: str | None = None
        try:
            db.execute(
                "UPDATE apps SET status = ?, container_id = ? WHERE name = ?",
                (prior_status, prior_container_id, app_name),
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
                f"for {app_name!r} may be stuck at status='stopped'."
            )
        return jsonify({"error": error_message}), 500

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
        # Persist failures instead of letting them 500 out: the rename
        # has already succeeded, and the dashboard needs a visible error
        # on the app rather than a generic server error.
        try:
            start_app_process(new_name, db, config)
        except (RuntimeError, ValueError) as e:
            logger.warning("Failed to restart %s after rename: %s", new_name, e)
            db.execute(
                "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                (str(e), new_name),
            )
            db.commit()

    return jsonify({"ok": True, "name": new_name})
