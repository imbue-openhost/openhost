import os
import sqlite3
import threading

from quart import Quart

from compute_space.config import Config
from compute_space.core import identity
from compute_space.core.apps import remove_app_background
from compute_space.core.apps import start_app_process
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.containers import container_runtime_available
from compute_space.core.containers import is_container_running
from compute_space.core.logging import logger
from compute_space.core.storage import start_storage_guard
from compute_space.db import init_db


def _mark_running_apps_container_runtime_missing(config: Config) -> int:
    """Flip every running/starting/building app to ``status='error'`` with
    ``CONTAINER_RUNTIME_MISSING_ERROR`` and clear ``container_id``.  Returns rowcount.
    """
    db = sqlite3.connect(config.db_path)
    try:
        cursor = db.execute(
            "UPDATE apps SET status = 'error', error_message = ?, container_id = NULL "
            "WHERE status IN ('running', 'starting', 'building')",
            (CONTAINER_RUNTIME_MISSING_ERROR,),
        )
        db.commit()
        return cursor.rowcount
    finally:
        db.close()


def _check_app_status(config: Config) -> None:
    """On startup, verify apps marked 'running' are still alive.

    Apps that need rebuilding are restarted sequentially in a single
    background thread to avoid concurrent image builds against the same
    containers-storage instance.  When podman isn't available, every
    running/starting/building app is flipped to 'error' with a
    remediation message and no rebuild is attempted — the dashboard
    stays reachable so the operator can see what happened.
    """
    if not container_runtime_available():
        affected = _mark_running_apps_container_runtime_missing(config)
        if affected:
            logger.error(
                "podman runtime missing; marked %d running/starting apps as error. %s",
                affected,
                CONTAINER_RUNTIME_MISSING_ERROR,
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
                alive = is_container_running(row["container_id"])

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


def _resume_pending_removals(config: Config) -> None:
    """Resume any app removal that was in flight when the server stopped.

    A row in ``status='removing'`` means a previous request flipped the
    status, persisted ``removing_keep_data``, and either crashed or was
    interrupted before the row was deleted. The keep-data choice was
    captured at request time, so we just spawn the same background
    worker again to finish what it started. The worker is idempotent
    (stop/remove/deprovision swallow already-clean errors, and the
    final DELETE is a no-op if the row is gone).
    """
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT name, removing_keep_data FROM apps WHERE status = 'removing'").fetchall()
    finally:
        db.close()

    for row in rows:
        # Default to ``keep_data=False`` if the column is somehow NULL —
        # this can only happen with a hand-mucked DB; the route always
        # sets the column when it flips status. False is the safer
        # default because the user only reaches removal through an
        # explicit confirmation, and "Keep Data" is the opt-in branch.
        keep_data = bool(row["removing_keep_data"]) if row["removing_keep_data"] is not None else False
        logger.info("Resuming pending removal of %s (keep_data=%s)", row["name"], keep_data)
        threading.Thread(
            target=remove_app_background,
            args=(row["name"], keep_data, config),
            daemon=True,
        ).start()


def init_app(app: Quart) -> None:
    """Initialize DB and app state. Call after data directories are ready."""
    config = app.openhost_config  # type: ignore[attr-defined]
    init_db(app)
    _check_app_status(config)
    _resume_pending_removals(config)
    identity.load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
