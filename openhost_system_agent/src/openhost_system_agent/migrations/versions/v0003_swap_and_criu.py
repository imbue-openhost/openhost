"""Configure swap space and install CRIU for app suspension.

Safe to run on already-provisioned hosts — every step is idempotent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file

# criu is not available via apt on Ubuntu 24.04; install the static binary
# from GitHub releases instead.  Bump this when upgrading CRIU.
_CRIU_VERSION = "4.1"
_CRIU_ARCH_MAP = {"x86_64": "x86_64", "aarch64": "aarch64"}


class Migration0003SwapAndCriu(SystemMigration):
    version = 3

    def up(self) -> None:
        self._install_criu()
        self._configure_swap()
        self._set_swappiness()

    def _install_criu(self) -> None:
        dest = Path("/usr/local/sbin/criu")
        if dest.exists():
            return
        arch = subprocess.run(
            ["uname", "-m"], capture_output=True, text=True
        ).stdout.strip()
        criu_arch = _CRIU_ARCH_MAP.get(arch, arch)
        url = (
            f"https://github.com/checkpoint-restore/criu/releases/download/"
            f"v{_CRIU_VERSION}/criu-static-{criu_arch}"
        )
        run("curl", "-fsSL", "-o", str(dest), url)
        dest.chmod(0o755)

    def _configure_swap(self) -> None:
        swapfile = Path("/swapfile")
        if not swapfile.exists():
            run("fallocate", "-l", "4G", str(swapfile))
            swapfile.chmod(0o600)
            run("mkswap", str(swapfile))
            run("swapon", str(swapfile))

        fstab = Path("/etc/fstab")
        entry = "/swapfile none swap sw 0 0"
        text = fstab.read_text()
        if entry not in text:
            fstab.write_text(text.rstrip("\n") + f"\n{entry}\n")

    def _set_swappiness(self) -> None:
        write_file(
            "/etc/sysctl.d/90-openhost-swap.conf",
            "# Managed by OpenHost; do not edit by hand.\n"
            "vm.swappiness = 10\n",
            mode=0o644,
        )
        run("sysctl", "-p", "/etc/sysctl.d/90-openhost-swap.conf")
