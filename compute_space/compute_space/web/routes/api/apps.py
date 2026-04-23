import asyncio
import dataclasses
import os
import re
import shutil
import stat
import subprocess
import threading

from quart import Blueprint
from quart import jsonify
from quart import redirect
from quart import request
from quart import url_for
from quart.typing import ResponseReturnValue
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from compute_space.config import get_config
from compute_space.core.apps import RESERVED_PATHS
from compute_space.core.apps import app_log_path
from compute_space.core.apps import clone_with_github_fallback
from compute_space.core.apps import git_pull
from compute_space.core.apps import insert_and_deploy
from compute_space.core.apps import reload_app_background
from compute_space.core.apps import start_app_process
from compute_space.core.apps import validate_manifest
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
from compute_space.db import get_engine
from compute_space.db import get_session
from compute_space.db.models import App
from compute_space.db.models import AppDatabase
from compute_space.db.models import AppPortMapping
from compute_space.db.models import AppToken
from compute_space.db.models import Permission
from compute_space.db.models import ServiceProvider
from compute_space.web.middleware import login_required


def _rmtree_force(path: str) -> None:
    """Remove a directory tree, handling files not owned by the current user.

    Git clones (or Docker) may leave files owned by a different uid.
    We first try a normal rmtree with chmod retry; if that fails (EPERM
    because we don't own the files) we fall back to sudo rm -rf.
    """

    def _make_writable_and_retry(func, err_path, _exc):  # type: ignore[no-untyped-def]
        os.chmod(err_path, stat.S_IRWXU)
        func(err_path)

    try:
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    except PermissionError:
        logger.warning("rmtree failed on {}, falling back to sudo rm -rf", path)
        subprocess.run(["sudo", "-n", "rm", "-rf", path], check=True, timeout=30)


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
    session = get_session()
    validation_error = await validate_manifest(manifest, session)
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
async def check_port() -> ResponseReturnValue:
    """Check if a host port is available. Returns {port, available, used_by}."""
    port_str = request.args.get("port", "")
    try:
        port = int(port_str)
    except (ValueError, TypeError):
        return jsonify({"error": "port must be an integer"}), 400
    if port < 1 or port > 65535:
        return jsonify({"error": "port must be 1-65535"}), 400

    session = get_session()
    available, used_by = await check_port_available(port, session)
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

    if clone_dir is None:
        raise RuntimeError("clone_dir unexpectedly None after successful clone")
    if manifest is None:
        try:
            manifest = parse_manifest(clone_dir)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    if app_name is None:
        app_name = manifest.name

    session = get_session()
    validation_error = await validate_manifest(manifest, session, app_name=app_name)
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
        app_name = await insert_and_deploy(
            manifest,
            final_dir,
            config,
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
async def api_apps() -> ResponseReturnValue:
    session = get_session()
    rows = (await session.execute(select(App.name, App.status, App.error_message).order_by(App.name))).all()
    apps: dict[str, dict[str, str | None]] = {
        row.name: {"status": row.status, "error_message": row.error_message} for row in rows
    }
    return jsonify(apps)


@api_apps_bp.route("/api/app_status/<app_name>")
@login_required
async def app_status(app_name: str) -> ResponseReturnValue:
    session = get_session()
    app_row = (await session.execute(select(App.status, App.error_message).where(App.name == app_name))).first()
    if app_row is None:
        return jsonify({"error": "not found"}), 404
    error_msg = app_row.error_message
    error_kind = None
    if error_msg and "[CACHE_CORRUPT]" in error_msg:
        error_kind = "cache_corrupt"
        error_msg = "Docker build cache is corrupted."
    return jsonify({"status": app_row.status, "error": error_msg, "error_kind": error_kind})


@api_apps_bp.route("/app_logs/<app_name>")
@login_required
async def app_logs(app_name: str) -> ResponseReturnValue:
    config = get_config()
    session = get_session()
    container_id = (
        await session.execute(select(App.docker_container_id).where(App.name == app_name))
    ).scalar_one_or_none()
    if container_id is None and not (await session.execute(select(App.name).where(App.name == app_name))).first():
        return "App not found", 404
    logs = get_docker_logs(app_name, config.temporary_data_dir, container_id)
    return logs, 200, {"Content-Type": "text/plain; charset=utf-8"}


@api_apps_bp.route("/stop_app/<app_name>", methods=["POST"])
@login_required
async def stop_app(app_name: str) -> ResponseReturnValue:
    session = get_session()
    app_row = (await session.execute(select(App).where(App.name == app_name))).scalar_one_or_none()
    if app_row is None:
        return jsonify({"error": "App not found"}), 404

    stop_app_process(app_row)
    stop_container(f"openhost-{app_name}")
    await session.execute(update(App).where(App.name == app_name).values(status="stopped", docker_container_id=None))
    await session.commit()
    return jsonify({"ok": True})


@api_apps_bp.route("/reload_app/<app_name>", methods=["GET", "POST"])
@login_required
async def reload_app(app_name: str) -> ResponseReturnValue:
    config = get_config()
    session = get_session()
    app_row = (await session.execute(select(App).where(App.name == app_name))).scalar_one_or_none()
    if app_row is None:
        return jsonify({"error": "App not found"}), 404

    form = await request.form if request.method == "POST" else {}
    update_flag = form.get("update") == "1"
    continue_oauth = request.args.get("continue_oauth_update") == "1"

    log_file = app_log_path(app_name, config)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not continue_oauth:
        open(log_file, "w").close()  # truncate

    with open(log_file, "a") as lf:
        if not continue_oauth:
            lf.write(f"reloading app (update={update_flag})\n")
        else:
            lf.write("continuing app reload after oauth\n")

        if update_flag or continue_oauth:
            if not app_row.repo_path or not os.path.isdir(os.path.join(app_row.repo_path, ".git")):
                await session.execute(
                    update(App)
                    .where(App.name == app_name)
                    .values(
                        status="error",
                        error_message="No git repository found to update. If this is a builtin app, git-based updates are not possible.",
                    )
                )
                await session.commit()
                return jsonify({"ok": True})

            repo_url = app_row.repo_url or ""
            pull_ok = False
            pull_err = None

            if not continue_oauth:
                lf.write("Attempting git pull without github oauth\n")
                lf.flush()
                pull_ok, pull_err = await asyncio.to_thread(
                    git_pull,
                    app_row.repo_path,
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
                    await session.execute(
                        update(App).where(App.name == app_name).values(status="error", error_message=e.message)
                    )
                    await session.commit()
                    if continue_oauth:
                        return redirect(url_for("apps.app_detail", app_name=app_name))
                    return jsonify({"ok": True})
                except OAuthAuthorizationRequired as e:
                    lf.write("No token available; redirecting to oauth flow\n")
                    return redirect(e.authorize_url)
                lf.flush()
                pull_ok, pull_err = await asyncio.to_thread(
                    git_pull,
                    app_row.repo_path,
                    app_name,
                    github_token=token,
                    log_file=log_file,
                    repo_url=repo_url,
                )

            if not pull_ok:
                await session.execute(
                    update(App)
                    .where(App.name == app_name)
                    .values(status="error", error_message=f"Git pull failed: {pull_err}")
                )
                await session.commit()
                if continue_oauth:
                    return redirect(url_for("apps.app_detail", app_name=app_name))
                return jsonify({"ok": True})

    repo_path = app_row.repo_path
    await asyncio.to_thread(stop_app_process, app_row)
    await session.execute(
        update(App).where(App.name == app_name).values(status="building", docker_container_id=None, error_message=None)
    )
    await session.commit()

    threading.Thread(
        target=reload_app_background,
        args=(app_name, repo_path, config),
        daemon=True,
    ).start()

    return jsonify({"ok": True})


@api_apps_bp.route("/remove_app/<app_name>", methods=["POST"])
@login_required
async def remove_app(app_name: str) -> ResponseReturnValue:
    config = get_config()
    session = get_session()
    app_row = (await session.execute(select(App).where(App.name == app_name))).scalar_one_or_none()
    if app_row is None:
        return jsonify({"error": "App not found"}), 404

    form = await request.form
    keep_data = form.get("keep_data") == "1"

    await asyncio.to_thread(stop_app_process, app_row)
    await asyncio.to_thread(remove_image, app_row.name)

    try:
        if keep_data:
            await asyncio.to_thread(deprovision_temp_data, app_name, config.temporary_data_dir)
        else:
            await asyncio.to_thread(deprovision_data, app_name, config.persistent_data_dir, config.temporary_data_dir)
    except Exception as e:
        logger.warning("Failed to deprovision data for %s: %s", app_name, e)

    await session.execute(delete(App).where(App.name == app_name))
    await session.execute(delete(AppDatabase).where(AppDatabase.app_name == app_name))
    await session.commit()

    return jsonify({"ok": True})


async def _rename_app_in_db(engine: AsyncEngine, app_name: str, new_name: str) -> None:
    """Rename an app and cascade the rename across FK-referencing child tables.

    FKs on children are declared ``ON DELETE CASCADE`` but not ``ON UPDATE CASCADE``,
    so we disable the FK check for the duration of the rename and update each table
    explicitly. The PRAGMA must land outside a transaction — SQLite silently
    ignores ``PRAGMA foreign_keys`` inside an active txn — so we use a dedicated
    connection in AUTOCOMMIT isolation.
    """
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        await conn.execute(
            update(App)
            .where(App.name == app_name)
            .values(name=new_name, repo_path=func.replace(App.repo_path, f"/{app_name}/", f"/{new_name}/"))
        )
        await conn.execute(update(AppDatabase).where(AppDatabase.app_name == app_name).values(app_name=new_name))
        await conn.execute(update(AppToken).where(AppToken.app_name == app_name).values(app_name=new_name))
        await conn.execute(
            update(ServiceProvider).where(ServiceProvider.app_name == app_name).values(app_name=new_name)
        )
        await conn.execute(update(Permission).where(Permission.consumer_app == app_name).values(consumer_app=new_name))
        await conn.execute(update(AppPortMapping).where(AppPortMapping.app_name == app_name).values(app_name=new_name))
        await conn.execute(
            update(AppDatabase)
            .where(AppDatabase.app_name == new_name)
            .values(db_path=func.replace(AppDatabase.db_path, f"/{app_name}/", f"/{new_name}/"))
        )
        await conn.execute(text("PRAGMA foreign_keys=ON"))


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

    session = get_session()
    app_row = (await session.execute(select(App).where(App.name == app_name))).scalar_one_or_none()
    if app_row is None:
        return jsonify({"error": "App not found"}), 404

    if new_name == app_name:
        return jsonify({"ok": True, "name": new_name})

    conflict = (await session.execute(select(App.name).where(App.name == new_name))).scalar_one_or_none()
    if conflict is not None:
        return jsonify({"error": f"Name already in use by '{conflict}'"}), 409

    was_running = app_row.status in ("running", "starting", "building")
    stop_app_process(app_row)
    await session.execute(update(App).where(App.name == app_name).values(status="stopped", docker_container_id=None))
    await session.commit()

    for parent in [
        os.path.join(config.persistent_data_dir, "app_data"),
        os.path.join(config.temporary_data_dir, "app_temp_data"),
    ]:
        old_dir = os.path.join(parent, app_name)
        new_dir = os.path.join(parent, new_name)
        if os.path.exists(old_dir) and not os.path.exists(new_dir):
            os.rename(old_dir, new_dir)

    await _rename_app_in_db(get_engine(), app_name, new_name)

    if was_running:
        await start_app_process(new_name, session, config)

    return jsonify({"ok": True, "name": new_name})
