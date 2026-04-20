from __future__ import annotations

import os
import secrets as secrets_mod
import shutil

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

    Under rootless podman with idmapped bind mounts, directories don't need
    world-writable (0o777) permissions: the kernel rewrites uid/gid on
    access so container-root writes land on disk owned by the ``host`` user.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    env_vars = {}

    # Determine if permanent data dir is needed:
    # explicitly requested, has sqlite entries, or access_all_data
    needs_app_data = manifest.app_data or manifest.sqlite_dbs or manifest.access_all_data

    if needs_app_data:
        os.makedirs(app_data_dir, exist_ok=True)
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

    # Apps run in bridge-mode containers where 127.0.0.1 is the container
    # itself, not the host.  podman run is invoked with
    # --add-host=host.docker.internal:host-gateway (alongside
    # host.containers.internal) so this URL resolves to the host's
    # interface regardless of the runtime.  The Docker-style alias is
    # preserved so manifests written for the old runtime need no change.
    env_vars["OPENHOST_ROUTER_URL"] = f"http://host.docker.internal:{port}"

    # Zone identity info so apps can build federated auth flows
    env_vars["OPENHOST_ZONE_DOMAIN"] = zone_domain

    env_vars["OPENHOST_MY_REDIRECT_DOMAIN"] = my_openhost_redirect_domain

    return env_vars


def _remove_dir(dir_path: str) -> None:
    """Remove a directory tree.

    With rootless podman + idmapped mounts, every file under an app's data
    directory is owned by the ``host`` user on disk, so a plain
    ``shutil.rmtree`` succeeds without sudo or a privileged-container
    fallback.  Errors are logged and swallowed — a failed cleanup should
    not block app removal from the database.
    """
    if not os.path.exists(dir_path):
        return
    try:
        shutil.rmtree(dir_path)
    except OSError as e:
        # An unexpected permission error here means something other than
        # the router created a file we can't remove (or the idmapped mount
        # was misconfigured).  Surface it so the operator can investigate
        # but don't crash the calling path.
        logger.warning("Failed to remove data dir %s: %s", dir_path, e)


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
