"""Container lifecycle using rootless Podman.

Every container runs under the unprivileged ``host`` Linux user with a
per-app user-namespace mapping (``--uidmap``/``--gidmap``), so container-root
is an unprivileged subuid on the host.  Host bind mounts use idmapped mounts
(``:idmap``) so files written by container-root land on disk owned by the
``host`` user, which lets the router manage them without ``sudo`` or
chmod 0o777 on the data directory.

Security defaults applied to every container:

- ``--cap-drop=ALL`` then ``--cap-add`` for caps listed in the manifest
  (replaces the Docker behaviour of *adding* to the default set).  The
  manifest validator rejects host-privileged caps (``SYS_ADMIN`` etc.) at
  deploy time so this list is always safe to apply.
- ``--security-opt=no-new-privileges=true``.
- ``--userns`` / ``--uidmap`` / ``--gidmap`` give every app its own
  65536-UID window, disjoint from every other app's.

App-facing contract unchanged: images are still built from ``Dockerfile``,
bind mounts still appear at ``/data/app_data/<app>`` etc., and the
``OPENHOST_ROUTER_URL`` env var still points at ``host.docker.internal``
(Podman recognises this alias via ``--add-host``).
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

# Width of a per-app user-namespace mapping (the standard subuid window size).
# Every app gets a disjoint block of this many UIDs inside the host's
# subuid range, so UID 0 inside the container is a subuid unique to that
# app and UID N is that subuid + N (up to 65535).
UID_MAP_WIDTH = 65536

# First subuid/subgid allocated to per-app mappings.  Must match the range
# allocated to the ``host`` user by ansible (see ansible/tasks/podman.yml).
UID_MAP_BASE_START = 10_000_000

# Size of the subuid/subgid range allocated to the ``host`` user.  Must
# match the ``host:10000000:10000000`` entry ansible writes to /etc/subuid
# and /etc/subgid.  The router refuses to allocate a uid_map window that
# would spill past this range — an exhausted pool is an operator problem
# (too many apps, or many creates/deletes of apps with SQLite autoincrement
# never reusing ids), and must surface as a clear error rather than a
# malformed ``--uidmap`` that podman would reject later with a cryptic
# message.
UID_MAP_RANGE_SIZE = 10_000_000

# Cap on app_id values that the deterministic formula can accept without
# overflowing the allocated range.  app_id goes into uid_map_base as
# UID_MAP_BASE_START + app_id * UID_MAP_WIDTH, and the window ends at
# uid_map_base + UID_MAP_WIDTH.  Solving for that to stay within
# (UID_MAP_BASE_START + UID_MAP_RANGE_SIZE) gives this bound.
_MAX_APP_ID_FOR_UID_MAP = (UID_MAP_RANGE_SIZE // UID_MAP_WIDTH) - 1

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\([AB0-9]|\x1b[=>]|\x0f|\r")

# Marker we prefix RuntimeError messages with when a build failure is
# specifically a corrupted local build cache.  The HTTP API uses this to
# surface a "drop build cache" remediation to the user.
BUILD_CACHE_CORRUPT_MARKER = "[BUILD_CACHE_CORRUPT]"

# Fragments from podman build output that indicate the local storage/cache
# is in a state that can be fixed by pruning and retrying.  Matching any of
# these triggers the BUILD_CACHE_CORRUPT_MARKER path.
_BUILD_CACHE_CORRUPT_FRAGMENTS = (
    # Generic content-store corruption (inherited from Docker/BuildKit era
    # and occasionally still surfaced by podman when layering over broken
    # storage).
    "content digest sha256:",
    # Podman/containers-storage specific recovery hints.
    "storage-driver errored",
    "layer not known",
)


def compute_uid_map_base(app_id: int) -> int:
    """Deterministic subuid base for an app id.

    Each app gets its own disjoint 65536-UID window in the host's subuid
    range, so two apps never share container-root and can't read each
    other's files even if one escapes its mount namespace.

    Raises ``ValueError`` if ``app_id`` is negative or if the resulting
    window would fall outside the subuid range allocated to the host user.
    Exhaustion is an operator-level problem (too many total apps created,
    even counting deleted ones — SQLite's ``AUTOINCREMENT`` never reuses
    ids) and surfaces here rather than being passed through to podman.
    """
    if app_id < 0:
        raise ValueError(f"app_id must be non-negative, got {app_id}")
    if app_id > _MAX_APP_ID_FOR_UID_MAP:
        raise ValueError(
            f"app_id {app_id} exceeds the per-host subuid pool "
            f"(supports up to {_MAX_APP_ID_FOR_UID_MAP + 1} total apps over "
            f"the lifetime of this server).  Expand host's /etc/subuid + "
            f"/etc/subgid allocation and adjust UID_MAP_RANGE_SIZE to match."
        )
    return UID_MAP_BASE_START + app_id * UID_MAP_WIDTH


def _log_path(app_name: str, temp_data_dir: str) -> str:
    """Return the path to the build/deploy log file for an app."""
    # Historical filename kept so existing deployments keep their log path.
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    log_file = _log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(text)


def _is_build_cache_corrupt(output: str) -> bool:
    return any(frag in output for frag in _BUILD_CACHE_CORRUPT_FRAGMENTS)


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
            if _is_build_cache_corrupt(build_output):
                raise RuntimeError(f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.")
            # Include the tail of build output so the error is visible in
            # the main router log (the full output is already in the app log).
            tail = build_output[-2000:] if len(build_output) > 2000 else build_output
            raise RuntimeError(f"Container build failed (exit code {proc.returncode}):\n{tail}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            combined = result.stdout + result.stderr
            if _is_build_cache_corrupt(combined):
                raise RuntimeError(f"{BUILD_CACHE_CORRUPT_MARKER} Container build cache is corrupted.")
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
    uid_map_base: int,
    port_mappings: list[PortMapping] | None = None,
) -> str:
    """Start a detached container for an app.  Returns the container ID.

    ``uid_map_base`` is the starting host subuid for this app's 65536-UID
    user-namespace mapping.  It must be allocated from the host's
    ``/etc/subuid`` range (see ansible/tasks/podman.yml).
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
        # Per-app user namespace: container UID 0 -> host subuid uid_map_base.
        # Two apps always get disjoint 65536-UID windows.
        f"--uidmap=0:{uid_map_base}:{UID_MAP_WIDTH}",
        f"--gidmap=0:{uid_map_base}:{UID_MAP_WIDTH}",
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

    Named ``drop_docker_build_cache`` for HTTP API stability — the endpoint
    it backs is ``/api/drop-docker-cache`` and we'd rather keep the JSON API
    stable than rename every caller.  The actual operation is
    ``podman system prune --build``.
    """
    cmd = ["podman", "system", "prune", "-f", "--build"]
    logger.info("Dropping container build cache: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "podman system prune failed")
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

    Named ``get_docker_logs`` for callsite stability with the historical
    function name.  The returned text still represents the app's build log
    followed by live container runtime logs.
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
