"""Configure swap space and install CRIU for app suspension.

Safe to run on already-provisioned hosts — every step is idempotent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file


class Migration0003SwapAndCriu(SystemMigration):
    version = 3

    def up(self) -> None:
        self._install_criu()
        self._configure_swap()
        self._set_swappiness()

    def _install_criu(self) -> None:
        self._add_ubuntu_archive_universe()
        run("apt-get", "update", "-qq")
        run("apt-get", "install", "-y", "-qq", "criu")

    def _add_ubuntu_archive_universe(self) -> None:
        # Some cloud mirrors (e.g. Hetzner) are partial and don't carry all
        # universe packages. Add the official Ubuntu archive as a supplementary
        # source so packages like criu are available regardless of the mirror.
        fallback = Path("/etc/apt/sources.list.d/ubuntu-archive-universe.sources")
        if fallback.exists():
            return
        result = subprocess.run(
            ["lsb_release", "-sc"], capture_output=True, text=True
        )
        codename = result.stdout.strip() if result.returncode == 0 else "noble"
        fallback.write_text(
            "# Added by OpenHost — supplements cloud mirror with full Ubuntu universe.\n"
            "Types: deb\n"
            "URIs: http://archive.ubuntu.com/ubuntu\n"
            f"Suites: {codename}\n"
            "Components: universe\n"
            "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        )

    def _configure_swap(self) -> None:
        swapfile = Path("/swapfile")
        if not swapfile.exists():
            run("fallocate", "-l", "4G", str(swapfile))
            swapfile.chmod(0o600)
            run("mkswap", str(swapfile))
            run("swapon", str(swapfile))

        # Persist in /etc/fstab
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
