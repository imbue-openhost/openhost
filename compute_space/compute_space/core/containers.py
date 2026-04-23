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
_CACHE_CORRUPT_RE = re.compile(r"content digest sha256:[0-9a-f]+: not found")


def compute_data_mounts(
    manifest: AppManifest,
    app_name: str,
    data_dir: str,
    temp_data_dir: str,
) -> list[tuple[str, str, str | None]]:
    """Return the list of data-volume mounts implied by a manifest.

    Each tuple is ``(host_path, container_path, options)`` where
    ``options`` is the Docker mount options string (``"ro"``) or ``None``
    for a default read/write bind. The caller is expected to pass these
    into ``docker run -v``.

    Resolution rules (each category is independent — any combination of
    the three ``access_all_apps_*`` / ``access_vm_data*`` fields can be
    requested):

    * For app_data and app_temp_data, a broad (parent-directory) mount
      shadows the scoped (single-app) mount; only the broader mount is
      emitted since the scoped path would be a subpath of it anyway.
    * For vm_data, the manifest guarantees at most one of RO / RW is
      active: parsing rejects ``access_vm_data`` (RO) together with
      either ``access_vm_data_rw`` or the legacy ``access_all_data``
      shorthand (both of which imply RW). This function just emits
      whichever mount was requested.
    """
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    app_temp_dir = os.path.join(temp_data_dir, "app_temp_data", app_name)
    vm_data_dir = os.path.join(data_dir, "vm_data")
    c_app_data = f"{CONTAINER_ROOT}/app_data/{app_name}"
    c_app_temp = f"{CONTAINER_ROOT}/app_temp_data/{app_name}"
    c_vm_data = f"{CONTAINER_ROOT}/vm_data"

    mounts: list[tuple[str, str, str | None]] = []

    if manifest.wants_all_apps_data:
        mounts.append(
            (
                os.path.join(data_dir, "app_data"),
                f"{CONTAINER_ROOT}/app_data",
                None,
            )
        )
    elif manifest.wants_own_app_data:
        mounts.append((app_data_dir, c_app_data, None))

    if manifest.wants_all_apps_temp_data:
        mounts.append(
            (
                os.path.join(temp_data_dir, "app_temp_data"),
                f"{CONTAINER_ROOT}/app_temp_data",
                None,
            )
        )
    elif manifest.wants_own_app_temp_data:
        mounts.append((app_temp_dir, c_app_temp, None))

    if manifest.wants_vm_data_rw:
        mounts.append((vm_data_dir, c_vm_data, None))
    elif manifest.wants_vm_data_ro:
        mounts.append((vm_data_dir, c_vm_data, "ro"))

    return mounts


def _log_path(app_name: str, temp_data_dir: str) -> str:
    """Return the path to the build/deploy log file for an app (in temp data)."""
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def _append_log(app_name: str, temp_data_dir: str, text: str) -> None:
    """Append text to the app's log file."""
    log_file = _log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a") as f:
        f.write(text)


def _clear_log(app_name: str, temp_data_dir: str) -> None:
    """Clear the app's log file (on fresh deploy/reload)."""
    log_file = _log_path(app_name, temp_data_dir)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "w"):
        pass


def build_image(
    app_name: str,
    repo_path: str,
    dockerfile_rel_path: str,
    temp_data_dir: str | None = None,
) -> str:
    """Build a Docker image from the Dockerfile. Returns image tag."""
    image_tag = f"openhost-{app_name}:latest"
    dockerfile_path = os.path.join(repo_path, dockerfile_rel_path)
    cmd = [
        "docker",
        "build",
        "--progress=plain",
        "-t",
        image_tag,
        "-f",
        dockerfile_path,
        repo_path,
    ]
    logger.info("Building Docker image: %s", " ".join(cmd))

    if temp_data_dir:
        _append_log(app_name, temp_data_dir, f"=== Building image: {image_tag} ===\n")

    if temp_data_dir:
        # Stream build output line-by-line so the frontend can show progress
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
            if _CACHE_CORRUPT_RE.search(build_output):
                raise RuntimeError("[CACHE_CORRUPT] Docker build cache is corrupted.")
            # Include the tail of build output so the error is visible in
            # the main router log (the full output is already in the app log).
            tail = build_output[-2000:] if len(build_output) > 2000 else build_output
            raise RuntimeError(f"Docker build failed (exit code {proc.returncode}):\n{tail}")
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            if _CACHE_CORRUPT_RE.search(result.stderr):
                raise RuntimeError("[CACHE_CORRUPT] Docker build cache is corrupted.")
            raise RuntimeError(f"Docker build failed:\n{result.stderr}")

    if temp_data_dir:
        _append_log(app_name, temp_data_dir, "=== Build complete ===\n\n")

    return image_tag


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
    """Run a Docker container. Returns the container ID."""
    app_data_dir = os.path.join(data_dir, "app_data", app_name)
    vm_data_dir = os.path.join(data_dir, "vm_data")
    container_name = f"openhost-{app_name}"

    # Container paths follow the logical structure under CONTAINER_ROOT.
    # Only the ones used for env-var translation below are computed
    # here; ``compute_data_mounts`` owns the mount-side path layout.
    c_app_data = f"{CONTAINER_ROOT}/app_data/{app_name}"
    c_app_temp = f"{CONTAINER_ROOT}/app_temp_data/{app_name}"

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
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "-p",
        f"127.0.0.1:{local_port}:{manifest.container_port}",
        f"--memory={manifest.memory_mb}m",
        f"--cpus={manifest.cpu_millicores / 1000.0}",
        "--restart=unless-stopped",
        "--add-host=host.docker.internal:host-gateway",
    ]

    # Mount data volumes following the logical structure from docs/data.md.
    # See ``compute_data_mounts`` for the permission-resolution rules.
    # The vm_data dir is shared among apps and may not exist yet on a
    # fresh install, so create it on demand if the manifest asks for it.
    # Detecting that by host path is more robust than by container path,
    # since the container-path scheme is defined inside
    # ``compute_data_mounts`` and could drift.
    mounts = compute_data_mounts(manifest, app_name, data_dir, temp_data_dir)
    if any(host == vm_data_dir for host, _container, _opts in mounts):
        os.makedirs(vm_data_dir, exist_ok=True)
    for host_path, container_path, options in mounts:
        spec = f"{host_path}:{container_path}"
        if options:
            spec += f":{options}"
        cmd.extend(["-v", spec])

    # Structured port mappings: bind TCP+UDP on 0.0.0.0
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
    # or crash) so docker run doesn't fail with a name conflict.
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        timeout=30,
    )

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        _append_log(app_name, temp_data_dir, f"ERROR: {result.stderr}\n")
        raise RuntimeError(f"Docker run failed:\n{result.stderr}")

    container_id = result.stdout.strip()
    _append_log(app_name, temp_data_dir, f"Container started: {container_id[:12]}\n\n")
    return container_id


def stop_container(container_id: str) -> None:
    """Stop and remove a Docker container."""
    subprocess.run(["docker", "stop", container_id], capture_output=True, timeout=30)
    subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=30)


def stop_app_process(app_row: sqlite3.Row) -> None:
    """Stop the running process for an app. Does not update DB."""
    try:
        if app_row["docker_container_id"]:
            stop_container(app_row["docker_container_id"])
    except Exception as e:
        logger.warning("Error stopping app %s: %s", app_row["name"], e)


def remove_image(app_name: str) -> None:
    """Remove the Docker image for an app."""
    image_tag = f"openhost-{app_name}:latest"
    subprocess.run(["docker", "rmi", image_tag], capture_output=True, timeout=30)


def drop_docker_build_cache() -> str:
    """Drop Docker build cache and return command output."""
    cmd = ["docker", "builder", "prune", "--all", "--force"]
    logger.info("Dropping Docker build cache: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(output or "Docker builder prune failed")
    return output


def get_container_status(container_id: str) -> str:
    """Returns 'running', 'exited', or 'unknown'."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container_id],
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
    """Get combined build log + container runtime logs.

    Returns the build/deploy log file contents followed by recent
    docker container logs (if the container is running).
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
                ["docker", "logs", "--tail", str(tail), container_id],
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
