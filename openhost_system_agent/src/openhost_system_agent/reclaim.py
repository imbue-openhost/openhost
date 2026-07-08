"""Reclaim ownership of the host's pixi tree.

The openhost service runs as the unprivileged ``host`` user via ``pixi run``,
and pixi tracks its env and per-user caches under paths owned by ``host``. Any
pixi (or pip) operation accidentally run as root leaves root-owned files there
that the host service can't later modify — its next ``pixi run`` fails with
EACCES ("Failed to update PyPI packages ... Permission denied") and the service
won't start.

This module chowns the pixi tree back to ``host`` so such a mistake self-heals
instead of bricking the host. It is a failsafe: the code paths that install
deps already run as ``host`` (see ``apply_after_checkout``); this reclaims any
residue left by older buggy versions or by a stray future root-run pixi call.
"""

from __future__ import annotations

import os
import subprocess

from openhost_system_agent.pixi import HOST_USER

#: Pixi trees owned by the host user. The first holds the pixi binary itself
#: (targeted by ``pixi self-update``); the second holds the project's resolved
#: environment (targeted by ``pixi install``). Both must stay host-owned.
_PIXI_PATHS = (
    "/home/host/.pixi",
    "/home/host/openhost/.pixi",
)


def reclaim_pixi_ownership() -> None:
    """chown the host's pixi trees back to the host user. Root-only, idempotent.

    Safe to call repeatedly and cheap when nothing is misowned. Missing paths
    are skipped (a fresh host may not have the project env yet). Raises if not
    run as root, since only root can chown files another user owns.
    """
    if os.geteuid() != 0:
        raise RuntimeError("reclaim_pixi_ownership must be run as root")

    for path in _PIXI_PATHS:
        if not os.path.exists(path):
            continue
        # -R to cover the whole tree; -h so symlinks are chowned in place
        # rather than followed. check=True: a failure here means the env may
        # still be broken, so surface it rather than silently continuing.
        subprocess.run(
            ["chown", "-Rh", f"{HOST_USER}:{HOST_USER}", path],
            check=True,
            timeout=120,
        )
