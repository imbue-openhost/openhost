"""Host-runtime sentinel file.

``tasks/podman.yml`` writes ``/etc/openhost/runtime`` with ``runtime=podman``
plus a ``runtime_version`` integer.  The dashboard's "check for
updates" endpoint consults this file (alongside a live
``podman --version`` probe, see ``core.containers.podman_available``)
to decide whether to warn the operator that clicking Update would
produce a router whose runtime prerequisites aren't satisfied.

The sentinel is used as a soft signal only, not a hard startup gate:
the live podman probe in ``core.containers.podman_available`` is the
authoritative check.  ``host_prep_status()`` is suitable for the
settings-UI banner and for the ``/api/settings/update_repo_state``
preflight, where the goal is to warn or refuse before committing to
an upgrade — not to prevent the router from booting.

The sentinel's primary value is signalling runtime_version skew for
host-side changes that wouldn't otherwise be detectable by probing
the binary: a new sysctl, a new sudoers rule, a new kernel feature
prerequisite, or an allowlist change that the router code expects
the host to already honour.
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
    the router can parse it without pulling in a TOML/YAML dependency.
    Blank lines and ``#``-prefixed comments are ignored.  Unknown keys
    are ignored (forward compatibility: a future ansible version may
    add fields the current router doesn't know about).
    """
    values: dict[str, str] = {}
    for raw_line in contents.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
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
                f"router expects the host to have been provisioned by "
                f"`ansible-playbook ansible/setup.yml`."
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
                f"`ansible-playbook ansible/setup.yml` on this host to migrate."
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
