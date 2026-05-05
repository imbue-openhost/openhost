"""Container lifecycle using rootless Podman.

Every container runs under the unprivileged ``host`` user in podman's
default rootless user namespace.  Host bind mounts use ``:idmap`` so
container-root writes land on disk owned by the ``host`` user.  Each
app sees only its own ``/data/...`` subdirectory unless it requests
``access_all_data``.

Security defaults: ``--cap-drop=ALL`` then re-add ``DEFAULT_CAPABILITIES``
(Docker's default set) plus anything the manifest requests from
``SAFE_CAPABILITIES``.  Devices restricted to ``SAFE_DEVICE_PATHS``.
``--security-opt=no-new-privileges=true``.

``OPENHOST_ROUTER_URL`` points at ``host.containers.internal`` (podman's
native host-gateway alias); ``host.docker.internal`` is also registered
via ``--add-host`` so existing Dockerfiles keep resolving.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping

CONTAINER_ROOT = "/data"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-9]|\x1b[=>]|\x0f|\r")

# Prefix on RuntimeError messages when the build failure is a corrupted
# local build cache.  The HTTP layer uses this to show a "drop cache"
# remediation.
BUILD_CACHE_CORRUPT_MARKER = "[BUILD_CACHE_CORRUPT]"

# Docker's default capability set.  Matches Docker's implicit behaviour
# so debian-packaged daemons (tor, postgres, redis, nginx, …) that
# expect container-root to CHOWN image-layer files, bind low ports,
# setuid, etc. work without per-app manifest changes.  Confined to
# the userns; no host effect.
DEFAULT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "CHOWN",
        "DAC_OVERRIDE",
        "FOWNER",
        "FSETID",
        "KILL",
        "NET_BIND_SERVICE",
        "SETFCAP",
        "SETGID",
        "SETPCAP",
        "SETUID",
        "SYS_CHROOT",
        "NET_RAW",
        "MKNOD",
        "AUDIT_WRITE",
    }
)

# Build-output fragments that indicate the local containers-storage
# cache is corrupt and can be fixed by pruning.  Narrow enough not to
# match normal build progress, registry pull failures, or missing-
# Dockerfile errors.
_BUILD_CACHE_CORRUPT_FRAGMENTS_UNCONDITIONAL = (
    "storage-driver errored",
    "layer not known",
)

# The "missing local layer blob" error from containers-storage.  The
# full "content digest" prefix rules out registry-side "not found"
# errors that happen to mention a sha256 digest.
_MISSING_LAYER_RE = re.compile(r"content digest sha256:[0-9a-f]+:\s*not found", re.IGNORECASE)


def _is_build_cache_corrupt_line(line: str) -> bool:
    if any(frag in line for frag in _BUILD_CACHE_CORRUPT_FRAGMENTS_UNCONDITIONAL):
        return True
    return bool(_MISSING_LAYER_RE.search(line))


CONTAINER_RUNTIME_MISSING_ERROR = (
    "podman runtime not available — run `ansible-playbook ansible/setup.yml` "
    "on this host to install and configure rootless podman."
)


def container_runtime_available() -> bool:
    """Return True if ``podman --version`` succeeds.

    Unexpected failures (timeout, OSError other than FileNotFoundError)
    are logged; FileNotFoundError is silent since the caller already
    surfaces CONTAINER_RUNTIME_MISSING_ERROR.
    """
    try:
        result = subprocess.run(
            ["podman", "--version"],
            capture_output=True,
            timeout=5,
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        logger.warning("podman --version timed out after 5s; treating podman as unavailable")
        return False
    except OSError as e:
        logger.warning("podman --version failed with OSError (%s); treating podman as unavailable", e)
        return False
    return result.returncode == 0


def _log_path(app_name: str, temp_data_dir: str) -> str:
    # Name preserved as docker.log for compatibility with existing log
    # tooling on deployed hosts.
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    log_file = _log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(text)


def _is_build_cache_corrupt(output: str) -> bool:
    return any(_is_build_cache_corrupt_line(line) for line in output.splitlines())


def _raise_if_build_cache_corrupt(output: str) -> None:
    """Raise a tagged RuntimeError if ``output`` matches a cache-corruption pattern."""
    if _is_build_cache_corrupt(output):
        raise RuntimeError(f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.")


def build_image(
    app_name: str,
    repo_path: str,
    dockerfile_rel_path: str,
    temp_data_dir: str | None = None,
) -> str:
    """Build the container image for an app.  Returns the image tag."""
    image_tag = f"openhost-{app_name}:latest"
    dockerfile_path = os.path.join(repo_path, dockerfile_rel_path)
    cmd = [
        "podman",
        "build",
        "-t",
        image_tag,
        "-f",
        dockerfile_path,
        repo_path,
    ]
    logger.info("Building container image: %s", " ".join(cmd))

    if temp_data_dir:
        _append_log(app_name, temp_data_dir, f"=== Building image: {image_tag} ===\n")

    if temp_data_dir:
        # Stream build output line-by-line so the dashboard can show progress.
        build_output = ""
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    build_output += line
                    _append_log(app_name, temp_data_dir, line)
                proc.wait(timeout=300)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("Build process %d did not exit within 5s of SIGKILL", proc.pid)
                raise
        if proc.returncode != 0:
            _raise_if_build_cache_corrupt(build_output)
            tail = build_output[-2000:] if len(build_output) > 2000 else build_output
            raise RuntimeError(f"Container build failed (exit code {proc.returncode}):\n{tail}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            _raise_if_build_cache_corrupt(combined)
            raise RuntimeError(f"Container build failed:\n{combined}")

    if temp_data_dir:
        _append_log(app_name, temp_data_dir, "=== Build complete ===\n\n")

    return image_tag


def _bind_mount_arg(host_path: str, container_path: str, *, read_only: bool = False) -> str:
    """Render a ``-v`` value with ``:idmap`` (or ``:ro,idmap`` for read-only)."""
    options = "ro,idmap" if read_only else "idmap"
    return f"{host_path}:{container_path}:{options}"


def run_container(
    app_name: str,
    image_tag: str,
    manifest: AppManifest,
    local_port: int,
    env_vars: dict[str, str],
    data_dir: str,
    temp_data_dir: str,
    archive_dir: str,
    port_mappings: list[PortMapping] | None = None,
) -> str:
    """Start a detached container for an app.  Returns the container ID."""
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    app_archive_dir = os.path.join(archive_dir, app_name)
    vm_data_dir = os.path.join(data_dir, "vm_data")
    container_name = f"openhost-{app_name}"

    has_app_data = manifest.app_data or manifest.sqlite_dbs or manifest.access_all_data
    has_app_temp = manifest.app_temp_data or manifest.access_all_data
    has_app_archive = manifest.app_archive or manifest.access_all_data
    has_vm_data = manifest.access_vm_data or manifest.access_all_data

    c_app_data = f"{CONTAINER_ROOT}/app_data/{app_name}"
    c_app_temp = f"{CONTAINER_ROOT}/app_temp_data/{app_name}"
    c_app_archive = f"{CONTAINER_ROOT}/app_archive/{app_name}"
    c_vm_data = f"{CONTAINER_ROOT}/vm_data"

    # Translate host paths in env vars to their in-container equivalents.
    container_env = {}
    for key, value in env_vars.items():
        if key.startswith("OPENHOST_SQLITE_"):
            rel_path = os.path.relpath(value, app_data_dir)
            container_env[key] = os.path.join(c_app_data, rel_path)
        elif key == "OPENHOST_APP_DATA_DIR":
            container_env[key] = c_app_data
        elif key == "OPENHOST_APP_TEMP_DIR":
            container_env[key] = c_app_temp
        elif key == "OPENHOST_APP_ARCHIVE_DIR":
            container_env[key] = c_app_archive
        else:
            container_env[key] = value

    cmd = [
        "podman",
        "run",
        "-d",
        "--name",
        container_name,
        "--hostname",
        container_name,
        "-p",
        f"127.0.0.1:{local_port}:{manifest.container_port}",
        f"--memory={manifest.memory_mb}m",
        f"--cpus={manifest.cpu_millicores / 1000.0}",
        "--restart=unless-stopped",
        # host.docker.internal kept for compatibility with existing apps.
        "--add-host=host.docker.internal:host-gateway",
        "--add-host=host.containers.internal:host-gateway",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges=true",
    ]
    for cap in sorted(DEFAULT_CAPABILITIES):
        cmd.extend(["--cap-add", cap])

    if manifest.access_all_data:
        # Opt-in full access to every app's data.
        cmd.extend(["-v", _bind_mount_arg(os.path.join(data_dir, "app_data"), f"{CONTAINER_ROOT}/app_data")])
        cmd.extend(
            [
                "-v",
                _bind_mount_arg(
                    os.path.join(temp_data_dir, "app_temp_data"),
                    f"{CONTAINER_ROOT}/app_temp_data",
                ),
            ]
        )
        # access_all_data is permissive — skip the archive mount when
        # the tier isn't configured rather than refusing to start.
        if os.path.isdir(archive_dir):
            cmd.extend(["-v", _bind_mount_arg(archive_dir, f"{CONTAINER_ROOT}/app_archive")])
        os.makedirs(vm_data_dir, exist_ok=True)
        cmd.extend(["-v", _bind_mount_arg(vm_data_dir, c_vm_data)])
    else:
        if has_app_data:
            cmd.extend(["-v", _bind_mount_arg(app_data_dir, c_app_data)])
        if has_app_temp:
            cmd.extend(["-v", _bind_mount_arg(app_temp_dir, c_app_temp)])
        if has_app_archive:
            cmd.extend(["-v", _bind_mount_arg(app_archive_dir, c_app_archive)])
        if has_vm_data:
            os.makedirs(vm_data_dir, exist_ok=True)
            cmd.extend(["-v", _bind_mount_arg(vm_data_dir, c_vm_data, read_only=True)])

    # Structured port mappings: bind TCP+UDP on 0.0.0.0.  host_port
    # values below UNPRIVILEGED_PORT_FLOOR are rejected at manifest
    # parse time so these binds always succeed.
    if port_mappings:
        for pm in port_mappings:
            cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/tcp"])
            cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/udp"])

    # Skip caps already in the baseline to keep the argv clean.
    for cap in manifest.capabilities:
        if cap not in DEFAULT_CAPABILITIES:
            cmd.extend(["--cap-add", cap])

    for device in manifest.devices:
        cmd.extend(["--device", device])

    for key, value in container_env.items():
        cmd.extend(["-e", f"{key}={value}"])

    if manifest.container_command:
        cmd.append(image_tag)
        cmd.extend(manifest.container_command.split())
    else:
        cmd.append(image_tag)

    logger.info("Running container: %s", " ".join(cmd))
    _append_log(app_name, temp_data_dir, f"=== Starting container: {container_name} ===\n")

    # Remove any stale container with the same name so podman run doesn't
    # fail with a name conflict.
    subprocess.run(
        ["podman", "rm", "-f", container_name],
        capture_output=True,
        timeout=30,
    )

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        _append_log(app_name, temp_data_dir, f"ERROR: {result.stderr}\n")
        raise RuntimeError(f"Container start failed:\n{result.stderr}")

    container_id = result.stdout.strip()
    _append_log(app_name, temp_data_dir, f"Container started: {container_id[:12]}\n\n")
    return container_id


def stop_container(container_id: str) -> None:
    """Stop and remove a container by ID or name.  Idempotent."""
    subprocess.run(["podman", "stop", container_id], capture_output=True, timeout=30)
    subprocess.run(["podman", "rm", "-f", container_id], capture_output=True, timeout=30)


def stop_app_process(app_row: sqlite3.Row) -> None:
    """Stop the running process for an app.  Does not update the database."""
    try:
        if app_row["container_id"]:
            stop_container(app_row["container_id"])
    except Exception as e:
        logger.warning("Error stopping app %s: %s", app_row["name"], e)


def remove_image(app_name: str) -> None:
    """Remove the image built for an app.  Idempotent."""
    image_tag = f"openhost-{app_name}:latest"
    subprocess.run(["podman", "rmi", image_tag], capture_output=True, timeout=30)


def drop_docker_build_cache() -> str:
    """Reclaim container-engine disk space via ``podman image prune``.

    Removes every unused image (including stopped-app images, which
    will be rebuilt on next deploy) plus dangling intermediate layers.
    Does not reclaim ``RUN --mount=type=cache`` build mounts.  The
    function and HTTP endpoint path retain "docker" in their names
    for external API compatibility.
    """
    cmd = ["podman", "image", "prune", "--all", "--force"]
    logger.info("Dropping container build cache: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "podman image prune failed")
    return output


def is_container_running(container_id: str) -> bool:
    """Return True iff podman reports the container's ``State.Status`` as ``running``.

    Any error (missing binary, timeout, nonzero exit, unknown container) maps
    to False.  Never raises; unexpected errors are logged at WARNING.
    """
    try:
        result = subprocess.run(
            ["podman", "inspect", "--format", "{{.State.Status}}", container_id],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        logger.warning("podman inspect timed out after 10s for %s", container_id)
        return False
    except OSError as e:
        logger.warning("podman inspect failed for %s with OSError: %s", container_id, e)
        return False
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "running"


def get_docker_logs(
    app_name: str,
    temp_data_dir: str,
    container_id: str | None = None,
    tail: int = 10000,
) -> str:
    """Full build log followed by the tail of podman container logs (ANSI-stripped)."""
    parts = []

    log_file = _log_path(app_name, temp_data_dir)
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                parts.append(f.read())
        except OSError as e:
            logger.warning("Could not read build log %s: %s", log_file, e)

    if container_id:
        try:
            result = subprocess.run(
                ["podman", "logs", "--tail", str(tail), container_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
            combined = (result.stdout + result.stderr).strip()
            if combined:
                combined = _ANSI_RE.sub("", combined)
                parts.append("=== Container logs ===\n" + combined)
        except subprocess.TimeoutExpired:
            logger.warning("podman logs timed out after 10s for %s", container_id)
        except OSError as e:
            logger.warning("podman logs failed for %s with OSError: %s", container_id, e)

    return "\n".join(parts) if parts else ""
