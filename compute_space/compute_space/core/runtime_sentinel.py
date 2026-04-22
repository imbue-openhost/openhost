"""Host-runtime sentinel file.

``ansible/tasks/podman.yml`` writes ``/etc/openhost/runtime`` with
``runtime=podman`` and a ``runtime_version`` integer.  The dashboard's
update flow reads this (via ``host_prep_status``) to warn the operator
when clicking Update would land on a host whose runtime_version doesn't
match what the router code expects.

Soft signal only — the authoritative runtime check is the live
``podman_available()`` probe in ``core.containers``.  The sentinel's
value is detecting host-side prep changes that the binary probe can't
(new sysctl, new sudoers rule, new kernel feature requirement, …).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

SENTINEL_PATH: Final[str] = "/etc/openhost/runtime"

# Bump ``EXPECTED_RUNTIME_VERSION`` (here) and the value written by
# ``ansible/tasks/podman.yml`` together whenever a host-side change
# ships that existing hosts must adopt before their next router start.
EXPECTED_RUNTIME: Final[str] = "podman"
EXPECTED_RUNTIME_VERSION: Final[int] = 1


@dataclass(frozen=True)
class HostPrepStatus:
    """Snapshot of sentinel state for the settings UI.

    When ``ok`` is False, ``reason`` is one of ``missing``,
    ``wrong_runtime``, ``wrong_version``, ``unreadable`` so the UI can
    branch on it; ``message`` is a human-readable explanation with
    remediation.
    """

    ok: bool
    reason: str
    message: str


def _parse_sentinel(contents: str) -> dict[str, str]:
    """Parse ``key=value`` lines; ignore blanks, ``#`` comments, and unknown keys."""
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
        # errors="replace" so a corrupted/binary file surfaces as
        # reason="wrong_runtime" rather than raising UnicodeDecodeError
        # (not an OSError, so our "never raises" contract would break).
        with open(path, encoding="utf-8", errors="replace") as f:
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
    """Return the current sentinel status.  Never raises."""
    return _read_sentinel(path)
