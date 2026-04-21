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

    Routine removals succeed with ``shutil.rmtree``; for entries owned by
    a UID the router can't chmod (rare but possible when files ended up
    under an unexpected owner), we fall back to ``sudo -n rm -rf`` via
    the NOPASSWD sudoers rule ansible installs.

    Errors from both paths are swallowed (with a warning) so a cleanup
    failure can't block removal of the app row from the database or
    leave the storage guard stuck.
    """
    if not os.path.exists(dir_path):
        return
    try:
        shutil.rmtree(dir_path)
        return
    except OSError as rmtree_err:
        logger.warning(
            "rmtree failed on %s (%s); falling back to sudo rm -rf",
            dir_path,
            rmtree_err,
        )

    try:
        subprocess.run(
            ["sudo", "-n", "rm", "-rf", dir_path],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as sudo_err:
        # capture_output=True hides stderr from the default string repr;
        # surface it explicitly so operators see things like
        # 'sudo: a password is required' without running the command by hand.
        stderr = (sudo_err.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.warning(
            "Failed to clean data dir %s via sudo (exit %d): %s",
            dir_path,
            sudo_err.returncode,
            stderr or "<no stderr>",
        )
    except (subprocess.TimeoutExpired, OSError) as sudo_err:
        logger.warning("Failed to clean data dir %s via sudo: %s", dir_path, sudo_err)


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
