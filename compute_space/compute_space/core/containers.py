"""Container lifecycle using rootless Podman.

Every container runs under the unprivileged ``host`` Linux user in
podman's default rootless user namespace (the one /etc/subuid allocates
at host-user-creation time).  Host bind mounts use idmapped mounts
(``:idmap``) so files written by container-root land on disk owned by
the ``host`` user, which lets the router manage them without ``sudo``
or chmod 0o777 on the data directory.

Cross-app isolation comes from podman's per-container mount / network /
pid / ipc namespaces plus the fact that each app only has its own
``/data/...`` bind-mount subdirectory (unless ``access_all_data`` is
set).  Two apps can't see each other's process tree, network state or
files even though they share the rootless UID namespace.

Security defaults applied to every container:

- ``--cap-drop=ALL`` plus ``--cap-add`` for each capability listed in
  the manifest.  The manifest validator restricts capabilities to a
  rootless-safe allowlist (``SAFE_CAPABILITIES``), so anything reaching
  here is safe to grant.
- ``--device`` is only added for entries in ``SAFE_DEVICE_PATHS``; the
  manifest parser rejects everything else (``/dev/mem``, ``/dev/kmem``,
  raw block devices, etc.) before deploy.
- ``--security-opt=no-new-privileges=true``.

App-facing contract: images are built from ``Dockerfile``, bind mounts
appear at ``/data/app_data/<app>`` and the like, and the
``OPENHOST_ROUTER_URL`` env var resolves via ``host.docker.internal``
(kept for compatibility) or its podman-native equivalent
``host.containers.internal``.  Both are registered as ``--add-host``
entries pointing at the host gateway so either works.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping

# Container mount root — all data dirs live under this prefix so the
# container filesystem stays clean (no data dirs mixed with /bin, /etc, etc).
CONTAINER_ROOT = "/data"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-9]|\x1b[=>]|\x0f|\r")

# Marker we prefix RuntimeError messages with when a build failure is
# specifically a corrupted local build cache.  The HTTP API uses this to
# surface a "drop build cache" remediation to the user.
BUILD_CACHE_CORRUPT_MARKER = "[BUILD_CACHE_CORRUPT]"

# Fragments from podman build output that indicate the local storage/cache
# is in a state that can be fixed by pruning and retrying.  Matching any of
# these triggers the BUILD_CACHE_CORRUPT_MARKER path.
_BUILD_CACHE_CORRUPT_FRAGMENTS = (
    # Content-store corruption: a manifest references a layer digest that
    # isn't present on disk.  Pruning the build cache almost always fixes it.
    "content digest sha256:",
    # Podman / containers-storage surfacing the same failure via different
    # wording depending on the storage driver.
    "storage-driver errored",
    "layer not known",
)


def _log_path(app_name: str, temp_data_dir: str) -> str:
    """Return the path to the build/deploy log file for an app (in temp data)."""
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    log_file = _log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(text)


def _is_build_cache_corrupt(output: str) -> bool:
    return any(frag in output for frag in _BUILD_CACHE_CORRUPT_FRAGMENTS)


def _raise_if_build_cache_corrupt(output: str) -> None:
    """Raise a RuntimeError tagged with BUILD_CACHE_CORRUPT_MARKER if the
    given build output matches any of the cache-corruption fragments.

    Shared between the streaming and non-streaming build paths so the
    error string stays identical in both places.
    """
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
        # Stream build output line-by-line so the dashboard can show progress
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        build_output = ""
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                build_output += line
                _append_log(app_name, temp_data_dir, line)
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        if proc.returncode != 0:
            _raise_if_build_cache_corrupt(build_output)
            # Include the tail of build output so the error is visible in
            # the main router log (the full output is already in the app log).
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
    """Render a ``-v`` argument value with the security options OpenHost wants.

    Every bind mount uses ``:idmap`` so container-root writes land on disk
    owned by the host ``host`` user (translated by the idmapped mount) rather
    than by the mapped subuid.  Read-only mounts combine ``:idmap`` with
    ``:ro``.
    """
    options = "idmap"
    if read_only:
        options = "ro,idmap"
    return f"{host_path}:{container_path}:{options}"


def run_container(
    app_name: str,
    image_tag: str,
    manifest: AppManifest,
    local_port: int,
    env_vars: dict[str, str],
    data_dir: str,
    temp_data_dir: str,
    port_mappings: list[PortMapping] | None = None,
) -> str:
    """Start a detached container for an app.  Returns the container ID.

    Containers run in podman's default rootless user namespace (the one
    /etc/subuid allocates to the ``host`` user at user-creation time).
    Cross-app isolation comes from mount/network/pid namespaces plus the
    fact that each app only has its own ``/data/...`` bind mounts; the
    filesystem-side ownership is handled by ``:idmap`` on every volume,
    which translates container-root writes to the host ``host`` user on
    disk so the router can manage files without sudo or chmod 0o777.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    vm_data_dir = os.path.join(data_dir, "vm_data")
    container_name = f"openhost-{app_name}"

    # Check which data access the app has (sqlite implies app_data)
    has_app_data = manifest.app_data or manifest.sqlite_dbs or manifest.access_all_data
    has_app_temp = manifest.app_temp_data or manifest.access_all_data
    has_vm_data = manifest.access_vm_data or manifest.access_all_data

    # Container paths follow the logical structure under CONTAINER_ROOT
    c_app_data = f"{CONTAINER_ROOT}/app_data/{app_name}"
    c_app_temp = f"{CONTAINER_ROOT}/app_temp_data/{app_name}"
    c_vm_data = f"{CONTAINER_ROOT}/vm_data"

    # Translate host paths to container paths in env vars
    container_env = {}
    for key, value in env_vars.items():
        if key.startswith("OPENHOST_SQLITE_"):
            rel_path = os.path.relpath(value, app_data_dir)
            container_env[key] = os.path.join(c_app_data, rel_path)
        elif key == "OPENHOST_APP_DATA_DIR":
            container_env[key] = c_app_data
        elif key == "OPENHOST_APP_TEMP_DIR":
            container_env[key] = c_app_temp
        else:
            container_env[key] = value

    cmd = [
        "podman",
        "run",
        "-d",
        "--name",
        container_name,
        "-p",
        f"127.0.0.1:{local_port}:{manifest.container_port}",
        f"--memory={manifest.memory_mb}m",
        f"--cpus={manifest.cpu_millicores / 1000.0}",
        "--restart=unless-stopped",
        # Make every app reachable via both the historical Docker-specific
        # alias and Podman's native one.  The OPENHOST_ROUTER_URL env var
        # passed to apps points at host.docker.internal so manifests written
        # for the old runtime keep working unchanged.
        "--add-host=host.docker.internal:host-gateway",
        "--add-host=host.containers.internal:host-gateway",
        # Start from zero capabilities and add back only what the manifest
        # explicitly requests.  The manifest validator rejects caps that
        # require host privilege (SYS_ADMIN, SYS_MODULE, SYS_PTRACE, ...)
        # so anything reaching here is safe to grant inside the user ns.
        "--cap-drop=ALL",
        # A compromised process can't gain privileges via setuid binaries.
        "--security-opt=no-new-privileges=true",
    ]

    # Mount data volumes following the logical structure from docs/data.md.
    # Every bind mount uses :idmap so host-side ownership stays sane.
    if manifest.access_all_data:
        # Full access: mount parent dirs so the app sees all apps' data.
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
        os.makedirs(vm_data_dir, exist_ok=True)
        cmd.extend(["-v", _bind_mount_arg(vm_data_dir, c_vm_data)])
    else:
        if has_app_data:
            cmd.extend(["-v", _bind_mount_arg(app_data_dir, c_app_data)])
        if has_app_temp:
            cmd.extend(["-v", _bind_mount_arg(app_temp_dir, c_app_temp)])
        if has_vm_data:
            os.makedirs(vm_data_dir, exist_ok=True)
            cmd.extend(["-v", _bind_mount_arg(vm_data_dir, c_vm_data, read_only=True)])

    # Structured port mappings: bind TCP+UDP on 0.0.0.0.  The manifest
    # validator rejects host_port values below the unprivileged port floor
    # (see ansible/tasks/podman.yml) so these binds always succeed.
    if port_mappings:
        for pm in port_mappings:
            cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/tcp"])
            cmd.extend(["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/udp"])

    for cap in manifest.capabilities:
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

    # Remove any stale container with the same name (e.g. from a previous run
    # or crash) so podman run doesn't fail with a name conflict.
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
    """Drop the container engine's build cache.  Returns human-readable output.

    Uses ``podman image prune --all --force``, which reclaims every
    dangling and unused image layer.  That covers the large majority of
    build-cache disk use on a rebuild-heavy host.  (Newer podman versions
    expose a ``--build-cache`` flag specifically for the persistent
    ``--mount=type=cache`` cache; we deliberately avoid it so the command
    also works on the Podman 4.9 shipped with Ubuntu 24.04 LTS.)
    """
    cmd = ["podman", "image", "prune", "--all", "--force"]
    logger.info("Dropping container build cache: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "podman image prune failed")
    return output


def get_container_status(container_id: str) -> str:
    """Return ``"running"``, ``"exited"``, or ``"unknown"``."""
    result = subprocess.run(
        ["podman", "inspect", "--format", "{{.State.Status}}", container_id],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip()


def get_docker_logs(
    app_name: str,
    temp_data_dir: str,
    container_id: str | None = None,
    tail: int = 10000,
) -> str:
    """Combined build log + recent container logs for an app.

    Returns the full build log file contents followed by the tail of the
    runtime container's stdout/stderr, with ANSI escapes stripped.
    """
    parts = []

    # Build/deploy log (full, no truncation)
    log_file = _log_path(app_name, temp_data_dir)
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                parts.append(f.read())
        except OSError:
            pass

    # Live container logs
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
        except (subprocess.TimeoutExpired, OSError):
            pass

    return "\n".join(parts) if parts else ""
