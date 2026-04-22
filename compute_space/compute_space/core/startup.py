import os
import sqlite3
import threading

from quart import Quart

from compute_space.config import Config
from compute_space.core import identity
from compute_space.core.apps import start_app_process
from compute_space.core.containers import PODMAN_MISSING_ERROR
from compute_space.core.containers import get_container_status
from compute_space.core.containers import podman_available
from compute_space.core.logging import logger
from compute_space.core.storage import start_storage_guard
from compute_space.db import init_db


def _mark_running_apps_podman_missing(config: Config) -> int:
    """Flip every running/starting/building app to ``status='error'`` with
    ``PODMAN_MISSING_ERROR`` as the remediation message, and clear
    ``container_id`` since any stored ID is no longer meaningful.

    Returns the number of rows updated.  Called from
    ``_check_app_status`` when podman isn't available on the host:
    attempting a rebuild would crash the router and take the dashboard
    down, so instead we surface a per-app error that points the
    operator at the ansible remediation and leave the dashboard up.
    """
    db = sqlite3.connect(config.db_path)
    try:
        cursor = db.execute(
            "UPDATE apps SET status = 'error', error_message = ?, container_id = NULL "
            "WHERE status IN ('running', 'starting', 'building')",
            (PODMAN_MISSING_ERROR,),
        )
        db.commit()
        return cursor.rowcount
    finally:
        db.close()


def _check_app_status(config: Config) -> None:
    """On startup, verify apps marked 'running' are still alive.

    Apps that need rebuilding are restarted sequentially in a single
    background thread to avoid concurrent image builds against the same
    containers-storage instance.

    If podman is not available on this host (the self-update transition
    case, before ansible has been re-run), running apps are marked as
    ``status='error'`` with a clear remediation message and no rebuild
    is attempted.  The dashboard still boots so the operator can see
    what happened.
    """
    if not podman_available():
        affected = _mark_running_apps_podman_missing(config)
        if affected:
            logger.error(
                "podman runtime missing; marked %d running/starting apps as error. %s",
                affected,
                PODMAN_MISSING_ERROR,
            )
        else:
            logger.warning("podman runtime missing; no running apps to mark.")
        return

    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    apps_to_restart: list[str] = []
    try:
        rows = db.execute("SELECT * FROM apps WHERE status = 'running'").fetchall()
        for row in rows:
            alive = False
            if row["container_id"]:
                status = get_container_status(row["container_id"])
                alive = status == "running"

            if not alive:
                if row["container_id"]:
                    repo_path = row["repo_path"]
                    if not repo_path or not os.path.isdir(repo_path):
                        db.execute(
                            "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                            (
                                f"Cannot restart: repo path missing ({repo_path})",
                                row["name"],
                            ),
                        )
                        continue
                    db.execute(
                        "UPDATE apps SET status = 'starting' WHERE name = ?",
                        (row["name"],),
                    )
                    apps_to_restart.append(row["name"])
                else:
                    db.execute(
                        "UPDATE apps SET status = 'stopped' WHERE name = ?",
                        (row["name"],),
                    )
        db.commit()
    finally:
        db.close()

    if apps_to_restart:
        threading.Thread(
            target=_restart_apps_sequential,
            args=(apps_to_restart, config),
            daemon=True,
        ).start()


def _restart_apps_sequential(app_names: list[str], config: Config) -> None:
    """Rebuild and restart apps one at a time in a background thread."""
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        for app_name in app_names:
            try:
                start_app_process(app_name, db, config)
                logger.info("Rebuilt and restarted app %s", app_name)
            except Exception as e:
                logger.exception("Failed to rebuild app %s", app_name)
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE name = ?",
                    (str(e), app_name),
                )
                db.commit()
    finally:
        db.close()


def init_app(app: Quart) -> None:
    """Initialize DB and app state. Call after data directories are ready."""
    config = app.openhost_config  # type: ignore[attr-defined]
    init_db(app)
    _check_app_status(config)
    identity.load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
