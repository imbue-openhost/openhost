"""Host-runtime sentinel file.

``tasks/podman.yml`` writes ``/etc/openhost/runtime`` with ``runtime=podman``
plus a ``runtime_version`` integer.  The router reads the same file on
startup (and the dashboard's "check for updates" endpoint consults it)
to confirm the host has been provisioned for the version of the router
about to run.

Contract:

- If the file is missing, or the runtime/version don't match what the
  router code declares, ``check_runtime_sentinel()`` raises
  ``RuntimeSentinelMismatch`` with a human-readable error.  ``main()``
  in ``web/start.py`` prints the error and exits non-zero, leaving the
  previous (Docker-era, or otherwise out-of-sync) router running under
  systemd until an operator runs ``ansible-playbook ansible/setup.yml``
  (or a targeted playbook that re-runs ``tasks/podman.yml``).

- The dashboard's update flow calls ``host_prep_status()`` before
  offering an Update button; if the host isn't prepared it surfaces a
  banner explaining what to do rather than letting the user brick
  themselves by clicking Update.

Why a file, not probing podman directly: an admin might have podman
installed but not have run the rest of the ansible prep (lingering,
port floor sysctl, idmap FS check).  The sentinel is the single
source of truth that says "this host has been fully prepared for
router runtime version N," not "podman happens to be on the PATH."
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# Canonical location managed by ansible.  Deliberately under /etc/ so
# it survives package / venv upgrades and can only be written by an
# operator with sudo (matching the trust boundary: the router itself
# should never be writing this).
SENTINEL_PATH: Final[str] = "/etc/openhost/runtime"

# What the currently-running router code expects.  Bump the version
# whenever a host-side change ships that existing hosts need to adopt
# before their next router restart (new package, new sysctl, new
# sudoers rule, etc.) and update ``tasks/podman.yml`` to write the
# new value.
EXPECTED_RUNTIME: Final[str] = "podman"
EXPECTED_RUNTIME_VERSION: Final[int] = 1


class RuntimeSentinelMismatch(RuntimeError):
    """Raised when the sentinel is missing or doesn't match expectations.

    The message is deliberately verbose: it is printed directly to the
    operator via stderr on router startup and surfaced to the user via
    the settings UI, and in both places the only useful thing is a
    clear remediation.
    """


@dataclass(frozen=True)
class HostPrepStatus:
    """Snapshot of sentinel state for the settings UI."""

    ok: bool
    # When ``ok`` is False, ``reason`` is a short machine-readable tag
    # (``missing``, ``wrong_runtime``, ``wrong_version``, ``unreadable``)
    # so the dashboard can branch on it; ``message`` is the verbose
    # human-readable explanation with remediation.
    reason: str
    message: str


def _parse_sentinel(contents: str) -> dict[str, str]:
    """Parse the sentinel's key=value format.

    The format is intentionally trivial so ansible can template it and
    the router can parse it without pulling in a TOML/YAML dependency
    this early in startup.  Blank lines and ``#``-prefixed comments
    are ignored.
    """
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            # Ignore unrecognised lines rather than fail — future
            # ansible versions may write extra metadata the current
            # router code doesn't know about.  Forward compatibility
            # matters here because the sentinel is specifically about
            # host/router version skew.
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _read_sentinel(path: str) -> HostPrepStatus:
    """Read and validate the sentinel; return a HostPrepStatus."""
    if not os.path.exists(path):
        return HostPrepStatus(
            ok=False,
            reason="missing",
            message=(
                f"Host runtime sentinel {path} is missing.  This build of the "
                f"router requires rootless podman; the host must be prepared "
                f"by running `ansible-playbook ansible/setup.yml` (or at "
                f"minimum `ansible/tasks/podman.yml`) before upgrading."
            ),
        )
    try:
        with open(path) as f:
            contents = f.read()
    except OSError as e:
        return HostPrepStatus(
            ok=False,
            reason="unreadable",
            message=f"Could not read host runtime sentinel {path}: {e}",
        )

    values = _parse_sentinel(contents)
    runtime = values.get("runtime", "")
    version_str = values.get("runtime_version", "")

    if runtime != EXPECTED_RUNTIME:
        return HostPrepStatus(
            ok=False,
            reason="wrong_runtime",
            message=(
                f"Host runtime sentinel {path} reports runtime={runtime!r}, "
                f"but this build requires runtime={EXPECTED_RUNTIME!r}.  Run "
                f"`ansible-playbook ansible/setup.yml` (or "
                f"`ansible/tasks/podman.yml`) on this host to migrate."
            ),
        )

    try:
        version = int(version_str)
    except ValueError:
        return HostPrepStatus(
            ok=False,
            reason="wrong_version",
            message=(
                f"Host runtime sentinel {path} has non-integer "
                f"runtime_version={version_str!r}; re-run the ansible "
                f"playbook to rewrite it."
            ),
        )

    if version != EXPECTED_RUNTIME_VERSION:
        return HostPrepStatus(
            ok=False,
            reason="wrong_version",
            message=(
                f"Host runtime sentinel {path} reports runtime_version="
                f"{version}, but this build requires runtime_version="
                f"{EXPECTED_RUNTIME_VERSION}.  Re-run the ansible playbook "
                f"to bring the host up to date before restarting the router."
            ),
        )

    return HostPrepStatus(ok=True, reason="", message="host runtime sentinel OK")


def host_prep_status(path: str = SENTINEL_PATH) -> HostPrepStatus:
    """Dashboard-facing: return the current sentinel status.

    Never raises; always returns a HostPrepStatus the UI can branch on.
    Safe to call on every update-check request.
    """
    return _read_sentinel(path)


def check_runtime_sentinel(path: str = SENTINEL_PATH) -> None:
    """Startup-facing: raise if the sentinel is missing or mismatched.

    Called from ``web/start.py`` before any DB migration or
    ``_check_app_status`` runs, so a mismatched host doesn't result in
    half-migrated state or every app flipping to ``error`` with a
    cryptic "podman not found" message.
    """
    status = _read_sentinel(path)
    if not status.ok:
        raise RuntimeSentinelMismatch(status.message)
