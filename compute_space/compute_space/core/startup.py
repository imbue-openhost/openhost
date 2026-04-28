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
    worker again to finish what it started. The worker is idempotent:
    stop/remove/deprovision swallow already-clean errors, and the
    worker early-returns if it finds the row already gone (which can
    happen if a concurrent request finished the removal first).

    All errors here are swallowed: this runs during ``init_app`` boot
    and a DB hiccup (locked file, missing column on a partially-migrated
    DB, disk I/O error) must NOT prevent the server from starting. The
    consequence of crashing init_app would be a server that won't boot
    at all, which is much worse than the alternative.

    Two failure modes get bespoke handling rather than a plain swallow:

    * Top-level DB failure (the SELECT itself raises): we log and
      return, leaving every ``removing`` row untouched. They stay
      visible on the dashboard but are not actionable until the next
      restart re-runs this function.
    * Per-row thread-spawn failure (e.g. resource exhaustion): we log
      and flip just that row to ``status='error'`` so the operator
      can immediately retry from the dashboard. Leaving it in
      ``removing`` would trip the route's atomic-claim guard and
      block all retries until the next restart.
    """
    try:
        db = sqlite3.connect(config.db_path)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute("SELECT name, removing_keep_data FROM apps WHERE status = 'removing'").fetchall()
        finally:
            db.close()
    except Exception:
        logger.exception("Could not query for pending removals on startup; skipping recovery")
        return

    for row in rows:
        try:
            # Default to ``keep_data=False`` if the column is somehow
            # NULL. The route always sets the column before flipping
            # status, so NULL here is anomalous (hand-mucked DB, partial
            # write, etc.). We log a warning so the operator notices,
            # then choose False because the user only reaches removal
            # through an explicit confirmation and "Keep Data" is the
            # opt-in branch — defaulting to True would silently leave
            # files behind that the user wanted gone.
            if row["removing_keep_data"] is None:
                logger.warning(
                    "Resuming removal of %s with keep_data=False because removing_keep_data is NULL "
                    "(unexpected — the /remove_app route always sets this column)",
                    row["name"],
                )
                keep_data = False
            else:
                keep_data = bool(row["removing_keep_data"])
            logger.info("Resuming pending removal of %s (keep_data=%s)", row["name"], keep_data)
            threading.Thread(
                target=remove_app_background,
                args=(row["name"], keep_data, config),
                daemon=True,
            ).start()
        except Exception:
            # Don't let a per-row failure (e.g. RuntimeError "can't
            # start new thread" under resource exhaustion) crash
            # startup. Flip the row to ``status='error'`` so the
            # operator can retry from the dashboard — leaving it in
            # 'removing' is a trap, because the route's atomic-claim
            # guard (WHERE status != 'removing') would refuse every
            # retry until the next server restart re-ran this
            # function. Mirrors the recovery path in the /remove_app
            # route handler.
            logger.exception("Could not spawn removal worker for %s; flipping to error", row["name"])
            try:
                err_db = sqlite3.connect(config.db_path)
                try:
                    err_db.execute(
                        "UPDATE apps SET status = 'error', error_message = ?, "
                        "removing_keep_data = NULL WHERE name = ?",
                        (
                            "Could not start removal worker on startup; retry from the dashboard.",
                            row["name"],
                        ),
                    )
                    err_db.commit()
                finally:
                    err_db.close()
            except sqlite3.Error:
                logger.exception(
                    "Could not flip %s to 'error' after spawn failure; row remains in 'removing'",
                    row["name"],
                )


def init_app(app: Quart) -> None:
    """Initialize DB and app state. Call after data directories are ready."""
    config = app.openhost_config  # type: ignore[attr-defined]
    init_db(app)
    _check_app_status(config)
    _resume_pending_removals(config)
    identity.load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
