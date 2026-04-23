import asyncio
import os
import threading

from quart import Quart
from sqlalchemy import select
from sqlalchemy import update

from compute_space.config import Config
from compute_space.core import identity
from compute_space.core.apps import start_app_process
from compute_space.core.containers import get_container_status
from compute_space.core.logging import logger
from compute_space.core.storage import start_storage_guard
from compute_space.db import get_session_maker
from compute_space.db import init_db
from compute_space.db.models import App


def _check_app_status(config: Config) -> None:
    """On startup, verify apps marked 'running' are still alive.

    Apps that need rebuilding are restarted sequentially in a single
    background thread to avoid concurrent Docker builds that can corrupt
    BuildKit's content store.
    """
    apps_to_restart = asyncio.run(_check_app_status_async())
    if apps_to_restart:
        threading.Thread(
            target=_restart_apps_sequential,
            args=(apps_to_restart, config),
            daemon=True,
        ).start()


async def _check_app_status_async() -> list[str]:
    apps_to_restart: list[str] = []
    async with get_session_maker()() as session:
        rows = (
            await session.execute(
                select(App.name, App.docker_container_id, App.repo_path).where(App.status == "running")
            )
        ).all()
        for row in rows:
            alive = False
            if row.docker_container_id:
                status = get_container_status(row.docker_container_id)
                alive = status == "running"

            if not alive:
                if row.docker_container_id:
                    repo_path = row.repo_path
                    if not repo_path or not os.path.isdir(repo_path):
                        await session.execute(
                            update(App)
                            .where(App.name == row.name)
                            .values(
                                status="error",
                                error_message=f"Cannot restart: repo path missing ({repo_path})",
                            )
                        )
                        continue
                    await session.execute(update(App).where(App.name == row.name).values(status="starting"))
                    apps_to_restart.append(row.name)
                else:
                    await session.execute(update(App).where(App.name == row.name).values(status="stopped"))
        await session.commit()
    return apps_to_restart


def _restart_apps_sequential(app_names: list[str], config: Config) -> None:
    """Rebuild and restart apps one at a time in a background thread."""
    asyncio.run(_restart_apps_sequential_async(app_names, config))


async def _restart_apps_sequential_async(app_names: list[str], config: Config) -> None:
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
