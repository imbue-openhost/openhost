"""Thin, runtime-neutral API for container lifecycle.

Every function here delegates to the ``ContainerRuntime`` selected by router
config.  Callers should import these functions (not runtime classes) so the
choice of runtime stays a single decision made in one place.

The implementation moved to ``compute_space.core.runtimes`` in the container
runtime abstraction refactor.  The module-level functions below are kept as
a stable import surface so we can add more runtimes (notably rootless Podman)
without touching every call site.
"""

from __future__ import annotations

import os
import sqlite3

from compute_space.core.logging import logger
from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping
from compute_space.core.runtimes import get_runtime

# Prefix used on RuntimeError messages raised by any runtime when the failure
# is specifically a corrupted local build cache.  The HTTP API uses this
# marker to surface a "drop cache" remediation to the user.
BUILD_CACHE_CORRUPT_MARKER = "[BUILD_CACHE_CORRUPT]"

# Container mount root — exposed so callers that need to construct container
# paths (e.g. env-var templating outside the runtime) can share the constant.
CONTAINER_ROOT = "/data"


def _build_log_path(app_name: str, temp_data_dir: str) -> str:
    """Path to the build/deploy log file for an app."""
    # Kept in sync with the runtime implementations, which write to this file
    # during image build and container start-up.
    return os.path.join(temp_data_dir, "app_temp_data", app_name, "docker.log")


def build_image(
    app_name: str,
    repo_path: str,
    dockerfile_rel_path: str,
    temp_data_dir: str | None = None,
) -> str:
    """Build the image for an app.  Returns the resulting image tag."""
    return get_runtime().build_image(
        app_name,
        repo_path,
        dockerfile_rel_path,
        temp_data_dir=temp_data_dir,
    )


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
    """Start a detached container for an app.  Returns the container ID."""
    return get_runtime().run_container(
        app_name,
        image_tag,
        manifest,
        local_port,
        env_vars,
        data_dir,
        temp_data_dir,
        port_mappings=port_mappings,
    )


def stop_container(container_id: str) -> None:
    """Stop and remove a container by ID or name.  Idempotent."""
    get_runtime().stop_container(container_id)


def stop_app_process(app_row: sqlite3.Row) -> None:
    """Stop the running process for an app.  Does not update the database."""
    try:
        if app_row["docker_container_id"]:
            stop_container(app_row["docker_container_id"])
    except Exception as e:
        logger.warning("Error stopping app %s: %s", app_row["name"], e)


def remove_image(app_name: str) -> None:
    """Remove the image built for an app.  Idempotent."""
    get_runtime().remove_image(app_name)


def drop_docker_build_cache() -> str:
    """Drop the container runtime's build cache.  Returns human-readable output.

    Historically named for Docker; kept as the public function name so existing
    callers and tests continue to work.  Delegates to whichever runtime is
    active.
    """
    return get_runtime().drop_build_cache()


def get_container_status(container_id: str) -> str:
    """Return ``"running"``, ``"exited"``, or ``"unknown"``."""
    return get_runtime().get_container_status(container_id)


def get_docker_logs(
    app_name: str,
    temp_data_dir: str,
    container_id: str | None = None,
    tail: int = 10000,
) -> str:
    """Combined build log + recent container logs for an app.

    The build log is read from disk (it was streamed there during
    ``build_image``); the container logs are fetched from the active runtime.
    """
    parts = []

    # Build/deploy log (full, no truncation)
    log_file = _build_log_path(app_name, temp_data_dir)
    if os.path.exists(log_file):
        try:
            with open(log_file) as f:
                parts.append(f.read())
        except OSError:
            pass

    # Live container logs
    if container_id:
        container_logs = get_runtime().get_container_logs(container_id, tail=tail)
        if container_logs:
            parts.append("=== Container logs ===\n" + container_logs)

    return "\n".join(parts) if parts else ""
