"""Configure swap space and install CRIU for app suspension.

Safe to run on already-provisioned hosts — every step is idempotent.
CRIU is not available via apt on Ubuntu 24.04, so it is built from source.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file

_CRIU_VERSION = "4.1"
_CRIU_BUILD_DEPS = (
    "build-essential",
    "libprotobuf-dev",
    "libprotobuf-c-dev",
    "protobuf-c-compiler",
    "protobuf-compiler",
    "pkg-config",
    "libnl-3-dev",
    "libcap-dev",
    "python3-yaml",
    "libbsd-dev",
)


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
        run("apt-get", "update", "-qq")
        run("apt-get", "install", "-y", "-qq", *_CRIU_BUILD_DEPS)
        src_url = (
            f"https://github.com/checkpoint-restore/criu"
            f"/archive/refs/tags/v{_CRIU_VERSION}.tar.gz"
        )
        build_dir = Path(f"/tmp/criu-{_CRIU_VERSION}")
        try:
            run("curl", "-fsSL", "-o", "/tmp/criu.tar.gz", src_url)
            run("tar", "-xf", "/tmp/criu.tar.gz", "-C", "/tmp")
            run("make", "-C", str(build_dir), "criu/criu")
            built = build_dir / "criu" / "criu"
            built.rename(dest)
            dest.chmod(0o755)
        finally:
            subprocess.run(
                ["rm", "-rf", "/tmp/criu.tar.gz", str(build_dir)], check=False
            )

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
