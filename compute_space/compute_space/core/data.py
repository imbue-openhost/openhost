from __future__ import annotations

import os
import secrets as secrets_mod
import shutil
import stat
import subprocess

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest


def rmtree_with_sudo_fallback(path: str, *, raise_on_failure: bool = False) -> None:
    """Remove a directory tree, falling back to ``sudo -n rm -rf`` for
    files the router can't chmod itself.

    The fast path is ``shutil.rmtree`` with an onexc hook that retries
    after chmodding entries read-only git clones might have left behind.
    The sudo fallback is reserved for the rare case where the router's
    unprivileged UID can't delete a file at all — it works because the
    ansible bootstrap installs a NOPASSWD sudoers rule for the ``host``
    user.

    If ``raise_on_failure`` is False (the default for data-deprovision
    code paths), both stages log and swallow their errors so a cleanup
    failure can't block removal of the app row from the database.  If
    True, the failure re-raises (used by the web route's code-sync
    helper, where a failed rmtree is a hard error).
    """
    if not os.path.exists(path):
        return

    def _make_writable_and_retry(func, err_path, _exc):  # type: ignore[no-untyped-def]
        os.chmod(err_path, stat.S_IRWXU)
        func(err_path)

    try:
        shutil.rmtree(path, onexc=_make_writable_and_retry)
        return
    except OSError as rmtree_err:
        # Intentionally catch the full OSError family (ENOENT races,
        # EBUSY, ESTALE on NFS, EROFS, etc.) not just PermissionError,
        # so the raise_on_failure=False contract really is total.  The
        # sudo fallback is unlikely to fix non-permission errors but
        # trying it is cheap and the logging path is the same.
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
        # capture_output=True hides stderr from the default string repr;
        # surface it explicitly so operators see things like
        # 'sudo: a password is required' without running the command by hand.
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
    """Remove an app's data directory during deprovision.  Cleanup
    failures are logged but never re-raised so they can't block the
    higher-level deprovision flow from removing the app row from the
    database."""
    rmtree_with_sudo_fallback(dir_path, raise_on_failure=False)


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
