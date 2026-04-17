from __future__ import annotations

import os
import secrets as secrets_mod
import shutil
import subprocess

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest


def provision_data(
    app_name: str,
    manifest: AppManifest,
    data_dir: str,
    temp_data_dir: str,
    my_openhost_redirect_domain: str,
    zone_domain: str,
    port: int,
) -> dict[str, str]:
    """Create data directories for an app based on manifest permissions.
    Returns a dict of environment variable name -> value.

    Apps only get filesystem access to directories they explicitly request
    via app_data and app_temp_data flags in [data]. SQLite entries
    implicitly enable app_data.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    env_vars = {}

    # Determine if permanent data dir is needed:
    # explicitly requested, has sqlite entries, or access_all_data
    needs_app_data = manifest.app_data or manifest.sqlite_dbs or manifest.access_all_data

    if needs_app_data:
        os.makedirs(app_data_dir, exist_ok=True)
        os.chmod(app_data_dir, 0o777)
        env_vars["OPENHOST_APP_DATA_DIR"] = app_data_dir

        sqlite_dir = os.path.join(app_data_dir, "sqlite")
        if manifest.sqlite_dbs:
            os.makedirs(sqlite_dir, exist_ok=True)
        for db_name in manifest.sqlite_dbs:
            db_path = os.path.join(sqlite_dir, f"{db_name}.db")
            # Don't create the file — let the app create it so its init logic
            # (e.g. "if not exists: create tables") triggers correctly.
            env_key = f"OPENHOST_SQLITE_{db_name}"
            env_vars[env_key] = db_path

    if manifest.app_temp_data or manifest.access_all_data:
        os.makedirs(app_temp_dir, exist_ok=True)
        env_vars["OPENHOST_APP_TEMP_DIR"] = app_temp_dir

    # Always create temp dir for internal use (repo clone, logs) even if
    # the app doesn't get access to it
    os.makedirs(app_temp_dir, exist_ok=True)

    env_vars["OPENHOST_APP_NAME"] = app_name

    # Generate app token for cross-app service calls
    env_vars["OPENHOST_APP_TOKEN"] = secrets_mod.token_urlsafe(32)

    # Apps run in Docker bridge-mode containers where 127.0.0.1 is the
    # container itself, not the host. Use host.docker.internal instead.
    env_vars["OPENHOST_ROUTER_URL"] = f"http://host.docker.internal:{port}"

    # Zone identity info so apps can build federated auth flows
    env_vars["OPENHOST_ZONE_DOMAIN"] = zone_domain

    env_vars["OPENHOST_MY_REDIRECT_DOMAIN"] = my_openhost_redirect_domain

    return env_vars


def _remove_dir(dir_path: str) -> None:
    """Remove a directory, falling back to docker for root-owned files.

    Docker containers run as root and may create root-owned files in
    the mounted data volume.  Try a normal rmtree first; if that fails
    due to permissions, fall back to ``docker run --rm`` to delete as root.
    """
    if not os.path.exists(dir_path):
        return
    try:
        shutil.rmtree(dir_path)
    except PermissionError:
        logger.info("Permission denied on rmtree, using docker to clean %s", dir_path)
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{dir_path}:/cleanup",
                    "alpine",
                    "rm",
                    "-rf",
                    "/cleanup",
                ],
                capture_output=True,
                timeout=30,
            )
            if os.path.exists(dir_path):
                shutil.rmtree(dir_path, ignore_errors=True)
        except Exception as e:
            logger.warning("Failed to clean data dir %s: %s", dir_path, e)


def deprovision_temp_data(app_name: str, temp_data_dir: str) -> None:
    """Remove the app's temporary data directory (app_temp_data/{name}).

    This includes the repo clone, build artifacts, runtime logs, and any
    files the app stored under OPENHOST_APP_TEMP_DIR.  Persistent data
    in app_data/{name} (SQLite databases) is not touched.
    """
    _remove_dir(os.path.join(temp_data_dir, "app_temp_data", app_name))


def deprovision_data(app_name: str, data_dir: str, temp_data_dir: str) -> None:
    """Remove all data for an app from both permanent and temp disks."""
    _remove_dir(os.path.join(data_dir, "app_data", app_name))
    deprovision_temp_data(app_name, temp_data_dir)
