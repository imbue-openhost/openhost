import asyncio
import os
import sqlite3
import threading

from quart import Quart

from compute_space.config import Config
from compute_space.core import identity
from compute_space.core.apps import start_app_process
from compute_space.core.containers import get_container_status
from compute_space.core.logging import logger
from compute_space.core.storage import start_storage_guard
from compute_space.db import get_session_maker
from compute_space.db import init_db


def _check_app_status(config: Config) -> None:
    """On startup, verify apps marked 'running' are still alive.

    Apps that need rebuilding are restarted sequentially in a single
    background thread to avoid concurrent Docker builds that can corrupt
    BuildKit's content store.
    """
    db = sqlite3.connect(config.db_path)
    db.row_factory = sqlite3.Row
    apps_to_restart: list[str] = []
    try:
        rows = db.execute("SELECT * FROM apps WHERE status = 'running'").fetchall()
        for row in rows:
            alive = False
            if row["docker_container_id"]:
                status = get_container_status(row["docker_container_id"])
                alive = status == "running"

            if not alive:
                if row["docker_container_id"]:
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
    asyncio.run(_restart_apps_sequential_async(app_names, config))


async def _restart_apps_sequential_async(app_names: list[str], config: Config) -> None:
    from sqlalchemy import update  # noqa: PLC0415

    from compute_space.db.models import App  # noqa: PLC0415

    async with get_session_maker()() as session:
        for app_name in app_names:
            try:
                await start_app_process(app_name, session, config)
                logger.info("Rebuilt and restarted app %s", app_name)
            except Exception as e:
                logger.exception("Failed to rebuild app %s", app_name)
                await session.execute(
                    update(App).where(App.name == app_name).values(status="error", error_message=str(e))
                )
                await session.commit()


def init_app(app: Quart) -> None:
    """Initialize DB and app state. Call after data directories are ready."""
    config = app.openhost_config  # type: ignore[attr-defined]
    init_db(app)
    _check_app_status(config)
    identity.load_identity_keys(config.persistent_data_dir)
    start_storage_guard(config)
