from __future__ import annotations

import os
import secrets as secrets_mod
import shutil
import stat
import subprocess

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest


def rmtree_with_sudo_fallback(path: str, *, raise_on_failure: bool = False) -> None:
    """Remove a directory tree, falling back to ``sudo -n rm -rf``.

    The fast path is ``shutil.rmtree`` with an onexc hook that chmods
    read-only entries (git-clone artefacts) and retries.  The sudo
    fallback relies on the NOPASSWD sudoers rule installed by ansible.

    With ``raise_on_failure=False`` both stages log and swallow errors
    so cleanup failures can't block the deprovision flow.  With
    ``raise_on_failure=True`` the error re-raises.
    """
    if not os.path.exists(path):
        return

    def _make_writable_and_retry(func, err_path, _exc):  # type: ignore[no-untyped-def]
        # Unlinking a file needs write perms on the parent directory, not
        # the file itself — so chmod both.  Git clones leave read-only
        # dirs as well as read-only files.
        os.chmod(err_path, stat.S_IRWXU)
        parent = os.path.dirname(err_path)
        if parent and parent != err_path:
            try:
                os.chmod(parent, stat.S_IRWXU)
            except OSError:
                pass
        func(err_path)

    try:
        shutil.rmtree(path, onexc=_make_writable_and_retry)
        return
    except OSError as rmtree_err:
        logger.warning(
            "rmtree failed on %s (%s), falling back to sudo rm -rf",
            path,
            rmtree_err,
        )

    try:
        subprocess.run(
            ["sudo", "-n", "rm", "-rf", path],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as sudo_err:
        # Surface captured stderr so the operator sees 'sudo: a password
        # is required' etc. without re-running the command by hand.
        stderr = (sudo_err.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.warning(
            "Failed to clean data dir %s via sudo (exit %d): %s",
            path,
            sudo_err.returncode,
            stderr or "<no stderr>",
        )
        if raise_on_failure:
            raise
    except (subprocess.TimeoutExpired, OSError) as sudo_err:
        logger.warning("Failed to clean data dir %s via sudo: %s", path, sudo_err)
        if raise_on_failure:
            raise


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
    # itself, not the host.  host.containers.internal (podman's host-gateway
    # alias) and the host.docker.internal back-compat alias are both
    # registered by run_container; we advertise the podman-native one.
    env_vars["OPENHOST_ROUTER_URL"] = f"http://host.containers.internal:{port}"

    # Zone identity info so apps can build federated auth flows
    env_vars["OPENHOST_ZONE_DOMAIN"] = zone_domain

    env_vars["OPENHOST_MY_REDIRECT_DOMAIN"] = my_openhost_redirect_domain

    return env_vars


def deprovision_temp_data(app_name: str, temp_data_dir: str) -> None:
    """Remove the app's temp dir (repo clone, build artefacts, logs,
    OPENHOST_APP_TEMP_DIR contents).  Persistent data is not touched.
    """
    rmtree_with_sudo_fallback(os.path.join(temp_data_dir, "app_temp_data", app_name))


def deprovision_data(app_name: str, data_dir: str, temp_data_dir: str) -> None:
    """Remove all data for an app from both permanent and temp disks."""
    rmtree_with_sudo_fallback(os.path.join(data_dir, "app_data", app_name))
    deprovision_temp_data(app_name, temp_data_dir)
