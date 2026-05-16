import os
import sqlite3
import threading

from compute_space.config import Config
from compute_space.core.apps import start_app_process
from compute_space.core.containers import CONTAINER_RUNTIME_MISSING_ERROR
from compute_space.core.containers import container_runtime_available
from compute_space.core.containers import is_container_running
from compute_space.core.default_apps import deploy_default_apps
from compute_space.core.logging import logger


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


def check_app_status(config: Config) -> None:
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
                            "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                            (
                                f"Cannot restart: repo path missing ({repo_path})",
                                row["app_id"],
                            ),
                        )
                        continue
                    db.execute(
                        "UPDATE apps SET status = 'starting' WHERE app_id = ?",
                        (row["app_id"],),
                    )
                    apps_to_restart.append(row["app_id"])
                else:
                    db.execute(
                        "UPDATE apps SET status = 'stopped' WHERE app_id = ?",
                        (row["app_id"],),
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


def _restart_apps_sequential(app_ids: list[str], config: Config) -> None:
    """Rebuild and restart apps one at a time in a background thread."""
    db = sqlite3.connect(config.db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    try:
        for app_id in app_ids:
            try:
                start_app_process(app_id, db, config)
                logger.info("Rebuilt and restarted app %s", app_id)
            except Exception as e:
                logger.exception("Failed to rebuild app %s", app_id)
                db.execute(
                    "UPDATE apps SET status = 'error', error_message = ? WHERE app_id = ?",
                    (str(e), app_id),
                )
                db.commit()
    finally:
        db.close()


def retry_pending_default_apps(config: Config) -> None:
    """Retry failed default-app installs on each boot."""
    db = sqlite3.connect(config.db_path)
    try:
        try:
            deploy_default_apps(config, db)
        except Exception as exc:
            logger.error("default_apps retry on startup raised: %s", exc)
    finally:
        db.close()
