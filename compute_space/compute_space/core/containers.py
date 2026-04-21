"""Container lifecycle using rootless Podman.

Every container runs under the unprivileged ``host`` Linux user with a
per-app user-namespace mapping (``--uidmap``/``--gidmap``), so container-root
is an unprivileged subuid on the host.  Host bind mounts use idmapped mounts
(``:idmap``) so files written by container-root land on disk owned by the
``host`` user, which lets the router manage them without ``sudo`` or
chmod 0o777 on the data directory.

Security defaults applied to every container:

- ``--cap-drop=ALL`` plus ``--cap-add`` for each capability listed in the
  manifest.  The manifest validator restricts capabilities to a
  rootless-safe allowlist (``SAFE_CAPABILITIES``), so anything reaching
  here is safe to grant inside the user namespace.
- ``--device`` is only added for entries in ``SAFE_DEVICE_PATHS``; the
  manifest parser rejects everything else (``/dev/mem``, ``/dev/kmem``,
  raw block devices, etc.) before deploy.
- ``--security-opt=no-new-privileges=true``.
- ``--uidmap`` / ``--gidmap`` give every app its own 65536-UID window,
  disjoint from every other app's.

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
    # Content-store corruption: a manifest references a layer digest that
    # isn't present on disk.  Pruning the build cache almost always fixes it.
    "content digest sha256:",
    # Podman / containers-storage surfacing the same failure via different
    # wording depending on the storage driver.
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


def build_log_path(app_name: str, temp_data_dir: str) -> str:
    """Return the path to the build/deploy log file for an app.

    Single source of truth for where *build* logs land — every caller
    (router build streaming, dashboard log view, app_log_path helper)
    funnels through this function rather than recomputing the path.
    Runtime container logs are not written to this file; they're fetched
    live via ``podman logs`` from ``get_app_logs``.
    """
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "build.log")


# Historical filename from the Docker-era layout.  ``get_app_logs`` falls
# back to reading this when ``build.log`` doesn't exist yet so existing
# deployments' log files stay visible through one deploy cycle; after
# the next rebuild everything switches to ``build.log``.
_LEGACY_BUILD_LOG_NAME = "docker.log"


def _legacy_build_log_path(app_name: str, temp_data_dir: str) -> str:
    return os.path.join(temp_data_dir, "app_temp_data", app_name, _LEGACY_BUILD_LOG_NAME)


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    log_file = build_log_path(app_name, temp_data_dir)
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


def _container_paths(app_name: str) -> tuple[str, str, str]:
    """Return the (app_data, app_temp_data, vm_data) paths inside a container.

    Single source of truth for where an app's data directories appear
    under ``CONTAINER_ROOT`` — consumed by both the env-var translator
    and the bind-mount assembler so a path-format change can't leave
    them desynchronised.
    """
    return (
        f"{CONTAINER_ROOT}/app_data/{app_name}",
        f"{CONTAINER_ROOT}/app_temp_data/{app_name}",
        f"{CONTAINER_ROOT}/vm_data",
    )


def _translate_env_for_container(
    env_vars: dict[str, str],
    app_name: str,
    app_data_dir: str,
) -> dict[str, str]:
    """Rewrite host paths in OpenHost env vars to their in-container equivalents.

    The router hands out paths as they are on the host (e.g.
    ``/opt/openhost/persistent_data/app_data/foo``); inside the container
    they must point to the idmapped mount target (``/data/app_data/foo``).
    """
    c_app_data, c_app_temp, _ = _container_paths(app_name)
    translated: dict[str, str] = {}
    for key, value in env_vars.items():
        if key.startswith("OPENHOST_SQLITE_"):
            rel_path = os.path.relpath(value, app_data_dir)
            translated[key] = os.path.join(c_app_data, rel_path)
        elif key == "OPENHOST_APP_DATA_DIR":
            translated[key] = c_app_data
        elif key == "OPENHOST_APP_TEMP_DIR":
            translated[key] = c_app_temp
        else:
            translated[key] = value
    return translated


def _base_run_args(
    *,
    container_name: str,
    local_port: int,
    manifest: AppManifest,
    uid_map_base: int,
) -> list[str]:
    """The constant, security-shaped arguments every app container receives."""
    return [
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


def _volume_args(
    manifest: AppManifest,
    app_name: str,
    data_dir: str,
    temp_data_dir: str,
) -> list[str]:
    """Build the ``-v`` arguments for an app's idmapped bind mounts.

    Returns them in pairs (``-v``, ``src:dst:opts``) so the caller can
    ``cmd.extend`` the result directly.  Mutates ``vm_data_dir`` on disk
    (via ``makedirs``) only for manifests that actually mount it.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    vm_data_dir = os.path.join(data_dir, "vm_data")
    c_app_data, c_app_temp, c_vm_data = _container_paths(app_name)

    args: list[str] = []
    if manifest.access_all_data:
        # Full access: mount parent dirs so the app sees all apps' data.
        args += ["-v", _bind_mount_arg(os.path.join(data_dir, "app_data"), f"{CONTAINER_ROOT}/app_data")]
        args += [
            "-v",
            _bind_mount_arg(
                os.path.join(temp_data_dir, "app_temp_data"),
                f"{CONTAINER_ROOT}/app_temp_data",
            ),
        ]
        os.makedirs(vm_data_dir, exist_ok=True)
        args += ["-v", _bind_mount_arg(vm_data_dir, c_vm_data)]
        return args

    # Per-app subdirs, mounted only when the manifest requests them.
    has_app_data = manifest.app_data or manifest.sqlite_dbs
    if has_app_data:
        args += ["-v", _bind_mount_arg(app_data_dir, c_app_data)]
    if manifest.app_temp_data:
        args += ["-v", _bind_mount_arg(app_temp_dir, c_app_temp)]
    if manifest.access_vm_data:
        os.makedirs(vm_data_dir, exist_ok=True)
        args += ["-v", _bind_mount_arg(vm_data_dir, c_vm_data, read_only=True)]
    return args


def _port_mapping_args(port_mappings: list[PortMapping] | None) -> list[str]:
    """Render TCP+UDP publish flags for every entry in ``port_mappings``.

    The manifest validator rejects ``host_port`` values below the
    unprivileged port floor, so every resulting bind is guaranteed to
    succeed against the kernel's ``net.ipv4.ip_unprivileged_port_start``.
    """
    if not port_mappings:
        return []
    args: list[str] = []
    for pm in port_mappings:
        args += ["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/tcp"]
        args += ["-p", f"0.0.0.0:{pm.host_port}:{pm.container_port}/udp"]
    return args


def _container_spec_args(
    manifest: AppManifest,
    container_env: dict[str, str],
    image_tag: str,
) -> list[str]:
    """Final per-app flags (caps, devices, env, image + optional command)."""
    args: list[str] = []
    for cap in manifest.capabilities:
        args += ["--cap-add", cap]
    for device in manifest.devices:
        args += ["--device", device]
    for key, value in container_env.items():
        args += ["-e", f"{key}={value}"]
    args.append(image_tag)
    if manifest.container_command:
        args.extend(manifest.container_command.split())
    return args


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

    Argv assembly is split into small helpers (_base_run_args,
    _volume_args, _port_mapping_args, _container_spec_args) so each slice
    of concern — security flags, bind mounts, port publishes, and
    per-app spec — can be reasoned about and tested independently.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    container_name = f"openhost-{app_name}"
    container_env = _translate_env_for_container(env_vars, app_name, app_data_dir)

    cmd = _base_run_args(
        container_name=container_name,
        local_port=local_port,
        manifest=manifest,
        uid_map_base=uid_map_base,
    )
    cmd += _volume_args(manifest, app_name, data_dir, temp_data_dir)
    cmd += _port_mapping_args(port_mappings)
    cmd += _container_spec_args(manifest, container_env, image_tag)

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


def drop_build_cache() -> str:
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


def get_app_logs(
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

    # Build/deploy log (full, no truncation).  Read from the current
    # filename first; fall back to the legacy filename so already-running
    # deployments keep surfacing their log until the next rebuild writes
    # build.log.
    for candidate in (
        build_log_path(app_name, temp_data_dir),
        _legacy_build_log_path(app_name, temp_data_dir),
    ):
        if os.path.exists(candidate):
            try:
                with open(candidate) as f:
                    parts.append(f.read())
            except OSError:
                pass
            break

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
