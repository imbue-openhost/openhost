"""Container-runtime test helpers.

Lives in a dedicated module (not conftest.py) so integration tests that
need to tear down containers and images they spawned can import a single,
runtime-agnostic entry point.  The current implementation shells out to
podman; callers should not assume that and should only rely on the
``container_cleanup`` contract: force-remove the named container and
its built image, swallowing failures.
"""

from __future__ import annotations

import subprocess


def container_cleanup(container_name: str, app_name: str) -> None:
    """Force-remove a test container and the image built for ``app_name``.

    Failures (already-gone, runtime unavailable, timeout) are intentionally
    swallowed — callers use this in teardown paths where the point is to
    leave the host clean, not to assert on the removal itself.
    """
    subprocess.run(
        ["podman", "rm", "-f", container_name],
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        ["podman", "rmi", "-f", f"openhost-{app_name}:latest"],
        capture_output=True,
        timeout=10,
    )
