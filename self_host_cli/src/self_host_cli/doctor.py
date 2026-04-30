"""``openhost doctor`` -- check prerequisites and common misconfigurations.

Checks:
  - Python >= 3.12
  - uv available
  - Rootless container runtime accessible (currently podman)
  - Required ports not in use
  - Router code present
"""

import json
import shutil
import socket
import subprocess
import sys

from compute_space import COMPUTE_SPACE_PACKAGE_DIR


class _Check:
    """Result of a single doctor check."""

    def __init__(self, name: str, ok: bool, detail: str = ""):
        self.name = name
        self.ok = ok
        self.detail = detail


def _check_python() -> _Check:
    v = sys.version_info
    ok = v >= (3, 12)
    detail = f"Python {v.major}.{v.minor}.{v.micro}"
    if not ok:
        detail += " (need >= 3.12)"
    return _Check("Python >= 3.12", ok, detail)


def _check_uv() -> _Check:
    path = shutil.which("uv")
    if path:
        return _Check("uv installed", True, path)
    return _Check("uv installed", False, "uv not found on PATH")


def _check_container_runtime() -> _Check:
    """Verify a usable, rootless container runtime is available.

    Currently only podman is supported; the check label is "Container
    runtime" so a future Docker/containerd/etc. backend can share the
    same probe contract without rewording user-facing output.  The
    router relies on rootless mode for its security model (idmapped
    bind mounts, per-container user namespaces), so reporting
    "available" for a rootful-only installation would be misleading.
    We parse ``podman info`` JSON and explicitly assert the rootless
    flag.
    """
    name = "Container runtime available"
    try:
        r = subprocess.run(
            ["podman", "info", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return _Check(name, False, "podman not found on PATH")
    except subprocess.TimeoutExpired:
        return _Check(name, False, "podman info timed out")

    if r.returncode != 0:
        return _Check(name, False, "podman info failed")

    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        return _Check(name, False, "podman info returned non-JSON output")

    rootless = info.get("host", {}).get("security", {}).get("rootless")
    if rootless is True:
        return _Check(name, True, "podman, rootless mode")
    if rootless is False:
        return _Check(name, False, "podman is running rootful; rootless required")
    return _Check(name, False, "could not determine rootless status from podman info")


def _check_port(port: int) -> _Check:
    name = f"Port {port} available"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(("127.0.0.1", port))
            if result == 0:
                return _Check(name, False, "port already in use")
            return _Check(name, True)
    except OSError as e:
        return _Check(name, False, str(e))


def _check_router_code() -> _Check:
    main_py = COMPUTE_SPACE_PACKAGE_DIR / "src" / "compute_space" / "__main__.py"
    if main_py.is_file():
        return _Check("Router code", True, str(COMPUTE_SPACE_PACKAGE_DIR))
    return _Check("Router code", False, f"__main__.py not found in {COMPUTE_SPACE_PACKAGE_DIR}")


def _print_checks(label: str, checks: list[_Check]) -> bool:
    """Print check results and return True if all passed."""
    print(f"{label}:")
    all_ok = True
    for c in checks:
        icon = "ok" if c.ok else "FAIL"
        detail = f"  ({c.detail})" if c.detail else ""
        print(f"  [{icon:>4}]  {c.name}{detail}")
        if not c.ok:
            all_ok = False
    return all_ok


def run_doctor() -> bool:
    """Run all checks, print results, return True if all passed."""
    checks: list[_Check] = [
        _check_python(),
        _check_uv(),
        _check_container_runtime(),
        _check_port(8080),
        _check_router_code(),
    ]
    ok = _print_checks("Checks", checks)
    print()
    if ok:
        print("All checks passed.")
    else:
        print("Some checks failed. Fix the issues above before running 'openhost up'.")
    return ok
