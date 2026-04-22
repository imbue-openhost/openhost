"""``openhost doctor`` -- check prerequisites and common misconfigurations.

Checks:
  - Python >= 3.12
  - uv available
  - Rootless podman accessible
  - Required ports not in use
  - Router code present
"""

import json
import os
import shutil
import socket
import subprocess
import sys

from compute_space import COMPUTE_SPACE_PACKAGE_DIR
from compute_space.core.runtime_sentinel import SENTINEL_PATH
from compute_space.core.runtime_sentinel import host_prep_status


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


def _check_podman() -> _Check:
    """Verify podman is installed AND configured to run rootless.

    The router relies on rootless mode for its security model (idmapped
    bind mounts, per-container user namespaces), so reporting "available"
    for a rootful-only installation would be misleading.  We parse
    ``podman info`` JSON and explicitly assert the rootless flag.
    """
    try:
        r = subprocess.run(
            ["podman", "info", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return _Check("Podman available", False, "podman not found on PATH")
    except subprocess.TimeoutExpired:
        return _Check("Podman available", False, "podman info timed out")
    except OSError as e:
        # EPERM on the binary, fd exhaustion, etc.  Match the OSError
        # handling in compute_space.core.containers.podman_available so
        # `openhost doctor` never crashes with a bare traceback.
        return _Check("Podman available", False, f"podman info failed with OSError: {e}")

    if r.returncode != 0:
        return _Check("Podman available", False, "podman info failed")

    try:
        info = json.loads(r.stdout)
    except json.JSONDecodeError:
        return _Check("Podman available", False, "podman info returned non-JSON output")

    # A future podman version could conceivably emit a JSON array or
    # scalar instead of an object.  Guard against `.get()` on non-dicts
    # so an unexpected format surfaces as a clean failure rather than
    # crashing `openhost doctor` with AttributeError.
    if not isinstance(info, dict):
        return _Check(
            "Podman available",
            False,
            f"podman info returned unexpected JSON type {type(info).__name__}",
        )

    host = info.get("host") or {}
    if not isinstance(host, dict):
        host = {}
    security = host.get("security") or {}
    if not isinstance(security, dict):
        security = {}
    rootless = security.get("rootless")
    if rootless is True:
        return _Check("Podman available", True, "rootless mode")
    if rootless is False:
        return _Check("Podman available", False, "podman is running rootful; rootless required")
    return _Check("Podman available", False, "could not determine rootless status from podman info")


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
    main_py = COMPUTE_SPACE_PACKAGE_DIR / "compute_space" / "__main__.py"
    if main_py.is_file():
        return _Check("Router code", True, str(COMPUTE_SPACE_PACKAGE_DIR))
    return _Check("Router code", False, f"__main__.py not found in {COMPUTE_SPACE_PACKAGE_DIR}")


def _check_runtime_sentinel() -> _Check | None:
    """Check the host-runtime sentinel if present.

    Returns None (no check reported) when the sentinel doesn't exist
    — dev laptops and ``openhost up --dev`` runs legitimately don't
    have ``/etc/openhost/runtime``, so omitting the check there keeps
    `openhost doctor` output clean.  On ansible-provisioned servers
    the sentinel exists and the check surfaces version-skew between
    the host provisioning and the router code (the exact scenario the
    sentinel was designed for).
    """
    if not os.path.exists(SENTINEL_PATH):
        return None
    status = host_prep_status()
    if status.ok:
        return _Check("Host runtime sentinel", True, status.message)
    return _Check("Host runtime sentinel", False, status.message)


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
        _check_podman(),
        _check_port(8080),
        _check_router_code(),
    ]
    # Sentinel check only fires on ansible-provisioned servers where
    # /etc/openhost/runtime exists; dev laptops legitimately don't have
    # it and shouldn't see a noisy WARN line every time they run
    # ``openhost doctor``.
    sentinel_check = _check_runtime_sentinel()
    if sentinel_check is not None:
        checks.append(sentinel_check)
    ok = _print_checks("Checks", checks)
    print()
    if ok:
        print("All checks passed.")
    else:
        print("Some checks failed. Fix the issues above before running 'openhost up'.")
    return ok
