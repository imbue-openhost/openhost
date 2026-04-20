"""ContainerRuntime protocol.

Defines the interface every container runtime implementation must satisfy.
The router always goes through this interface; runtime-specific details
(Docker CLI flags, Podman uidmap setup, etc.) live entirely inside each
implementation.
"""

from __future__ import annotations

from typing import Protocol

from compute_space.core.manifest import AppManifest
from compute_space.core.manifest import PortMapping


class ContainerRuntime(Protocol):
    """Interface the router uses to manage containers.

    Implementations are stateless from the caller's perspective — any state
    (subuid allocations, per-app networks, etc.) is persisted in the runtime
    itself or in the router's database, not on the runtime object.
    """

    name: str
    """Short identifier for the runtime (e.g. ``"docker"``, ``"podman"``).

    Used in log messages and error text so operators can tell which runtime
    produced a given failure.
    """

    def build_image(
        self,
        app_name: str,
        repo_path: str,
        dockerfile_rel_path: str,
        temp_data_dir: str | None = None,
    ) -> str:
        """Build the container image for an app.

        Returns the resulting image tag.  If ``temp_data_dir`` is provided,
        build output is streamed to the app's build log.

        Raises ``RuntimeError`` on build failure.  The error message should
        start with ``[BUILD_CACHE_CORRUPT]`` when the failure is specifically
        a corrupted local build cache (so callers can offer a "drop cache"
        remediation).
        """
        ...

    def run_container(
        self,
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
        ...

    def stop_container(self, container_id: str) -> None:
        """Stop and remove a container by ID or name.  Idempotent."""
        ...

    def remove_image(self, app_name: str) -> None:
        """Remove the image built for an app.  Idempotent."""
        ...

    def get_container_status(self, container_id: str) -> str:
        """Return ``"running"``, ``"exited"``, or ``"unknown"``."""
        ...

    def get_container_logs(
        self,
        container_id: str,
        tail: int = 10000,
    ) -> str:
        """Return recent stdout/stderr from a container.

        ANSI escape sequences are stripped.  Returns an empty string when
        logs are unavailable (e.g. container gone, runtime timeout).
        """
        ...

    def drop_build_cache(self) -> str:
        """Drop the runtime's local build cache.  Returns human-readable output.

        Raises ``RuntimeError`` on failure.
        """
        ...
