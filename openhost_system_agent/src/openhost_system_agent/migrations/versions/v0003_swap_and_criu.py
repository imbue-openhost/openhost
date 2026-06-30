"""Configure swap space, install CRIU, and grant checkpoint capability.

Safe to run on already-provisioned hosts — every step is idempotent.
CRIU is not available via apt on Ubuntu 24.04, so it is built from source.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from openhost_system_agent.migrations.base import SystemMigration
from openhost_system_agent.migrations.helpers import run
from openhost_system_agent.migrations.helpers import write_file

_CRIU_VERSION = "4.1"
_DROPIN_DIR = "/etc/systemd/system/openhost.service.d"
_DROPIN_PATH = f"{_DROPIN_DIR}/10-checkpoint-restore.conf"
_CRIU_BUILD_DEPS = (
    "build-essential",
    "libprotobuf-dev",
    "libprotobuf-c-dev",
    "protobuf-c-compiler",
    "protobuf-compiler",
    "python3-protobuf",
    "pkg-config",
    "libnl-3-dev",
    "libcap-dev",
    "libaio-dev",
    "libgnutls28-dev",
    "libnet-dev",
    "uuid-dev",
    "libbsd-dev",
    "python3-yaml",
)


class Migration0003SwapAndCriu(SystemMigration):
    version = 3

    def up(self) -> None:
        self._install_criu()
        self._symlink_criu_to_bin()
        self._configure_swap()
        self._set_swappiness()
        self._add_checkpoint_restore_cap()
        self._configure_host_containers_conf()
        self._install_checkpoint_helpers()
        self._configure_root_containers_conf()

    def _install_criu(self) -> None:
        dest = Path("/usr/local/sbin/criu")
        if dest.exists():
            return
        os.environ["DEBIAN_FRONTEND"] = "noninteractive"
        run("apt-get", "update", "-qq")
        # runc (the real opencontainers runc from apt) is required for CRIU
        # checkpoint support.  The conda-forge-bundled runc that ships with
        # podman is actually crun under a different name and lacks CRIU support.
        # apt runc goes to /usr/sbin/runc, which is already in podman's default
        # "runc" runtime search paths (containers.conf), so no extra config needed.
        run("apt-get", "install", "-y", "-qq", "runc")
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

    def _add_checkpoint_restore_cap(self) -> None:
        dropin = Path(_DROPIN_PATH)
        if dropin.exists():
            return
        Path(_DROPIN_DIR).mkdir(parents=True, exist_ok=True)
        write_file(
            _DROPIN_PATH,
            "[Service]\n"
            "# Allow rootless podman checkpoint/restore (CRIU).\n"
            "AmbientCapabilities=CAP_CHECKPOINT_RESTORE\n",
            mode=0o644,
        )
        run("systemctl", "daemon-reload")
        run("systemctl", "restart", "openhost")

    def _symlink_criu_to_bin(self) -> None:
        wrapper = Path("/usr/local/bin/criu")
        real = Path("/usr/local/sbin/criu")
        if wrapper.exists() or not real.exists():
            return
        # Podman's CRCheckForCriu runs `criu check` without --unprivileged,
        # which always exits 1 for non-root.  This wrapper injects the flag so
        # podman initialises supportsCheckpoint = true.  The real CRIU binary
        # handles dump/restore natively once CAP_CHECKPOINT_RESTORE is present.
        wrapper.write_text(
            "#!/bin/sh\n"
            "CRIU=/usr/local/sbin/criu\n"
            'if [ "$(id -u)" != "0" ] && [ "${1:-}" = "check" ]; then\n'
            '    exec "$CRIU" check --unprivileged "$@"\n'
            "fi\n"
            'exec "$CRIU" "$@"\n'
        )
        wrapper.chmod(0o755)

    def _install_checkpoint_helpers(self) -> None:
        # Rootless podman blocks checkpoint at the Go level regardless of
        # capabilities.  To checkpoint without root podman touching the host
        # user's container storage (which would leave root-owned files and
        # corrupt subsequent rootless podman operations), we call runc and
        # CRIU directly for checkpoint.  Restore still requires root podman
        # (rootless restore is also blocked), so we fix ownership afterwards.
        _PODMAN = "/home/host/openhost/.pixi/envs/default/bin/podman"
        _CONMON = "/home/host/openhost/.pixi/envs/default/bin/conmon"
        _ROOT = "/home/host/.local/share/containers/storage"

        # The runc wrapper injects --root pointing at the host user's runc
        # state dir so root podman (used for restore) can find rootless
        # containers.  Root podman's --runtime flag is ignored for checkpoint
        # but IS used for restore's runc create/restore calls.
        runc_wrapper = Path("/usr/local/sbin/openhost-runc")
        write_file(
            str(runc_wrapper),
            "#!/bin/sh\n"
            "HOST_UID=$(getent passwd host | cut -d: -f3)\n"
            'exec /usr/sbin/runc --root "/run/user/${HOST_UID}/runc" "$@"\n',
            mode=0o755,
        )

        # Checkpoint: call runc directly so root never writes into the host
        # user's runroot (/run/user/<uid>/containers).  We get the container
        # ID via rootless podman (as the host user), checkpoint via runc, then
        # package the result in podman's --ignore-rootfs archive format.
        checkpoint_script = Path("/usr/local/bin/openhost-checkpoint")
        write_file(
            str(checkpoint_script),
            "#!/bin/sh\n"
            "# Usage: openhost-checkpoint CONTAINER_NAME CHECKPOINT_PATH\n"
            "set -e\n"
            '[ $# -eq 2 ] || { echo "Usage: $0 CONTAINER_NAME CHECKPOINT_PATH" >&2; exit 1; }\n'
            "HOST_UID=$(getent passwd host | cut -d: -f3)\n"
            "RUNC_ROOT=\"/run/user/${HOST_UID}/runc\"\n"
            f"STORAGE_CTR={_ROOT}/overlay-containers\n"
            "CTR_ID=$(runuser -u host -- \\\n"
            f"    env XDG_RUNTIME_DIR=\"/run/user/${{HOST_UID}}\" \\\n"
            f"        {_PODMAN} inspect --format '{{{{.ID}}}}' \"$1\" 2>/dev/null) || true\n"
            '[ -n "${CTR_ID}" ] || { echo "Container \'$1\' not found" >&2; exit 1; }\n'
            'BUNDLE="${STORAGE_CTR}/${CTR_ID}/userdata"\n'
            "/usr/sbin/runc --root \"${RUNC_ROOT}\" \\\n"
            '    checkpoint \\\n'
            '    --image-path "${BUNDLE}/checkpoint" \\\n'
            '    --work-path "${BUNDLE}" \\\n'
            '    "${CTR_ID}"\n'
            'tar -czf "$2" -C "${BUNDLE}" checkpoint config.dump spec.dump artifacts\n'
            'chown -R host:host "${BUNDLE}/checkpoint" "${BUNDLE}/dump.log"\n'
            'chown host:host "$2"\n',
            mode=0o755,
        )

        # Restore: root podman is unavoidable here (rootless restore is also
        # blocked).  We fix all storage ownership afterwards so rootless podman
        # can continue managing the restored container.
        restore_script = Path("/usr/local/bin/openhost-restore")
        write_file(
            str(restore_script),
            "#!/bin/sh\n"
            "# Usage: openhost-restore CHECKPOINT_PATH\n"
            "set -e\n"
            '[ $# -eq 1 ] || { echo "Usage: $0 CHECKPOINT_PATH" >&2; exit 1; }\n'
            "HOST_UID=$(getent passwd host | cut -d: -f3)\n"
            f'{_PODMAN} \\\n'
            f'    --runtime /usr/local/sbin/openhost-runc \\\n'
            f'    --conmon {_CONMON} \\\n'
            f'    --root {_ROOT} \\\n'
            f'    --runroot "/run/user/${{HOST_UID}}/containers" \\\n'
            '    container restore \\\n'
            '    --import "$1"\n'
            # Restore writes root-owned files into the host user's storage and
            # runroot.  chown them back so rootless podman can manage the
            # restored container.
            f'find {_ROOT} -not -user host -exec chown host:host {{}} + 2>/dev/null || true\n'
            'find "/run/user/${HOST_UID}/containers" -not -user host -exec chown host:host {} + 2>/dev/null || true\n',
            mode=0o755,
        )

        sudoers = Path("/etc/sudoers.d/openhost-checkpoint")
        if not sudoers.exists():
            write_file(
                str(sudoers),
                "# Allow the openhost service user to checkpoint/restore containers\n"
                "# via privileged helpers that access the rootless container storage.\n"
                "host ALL=(root) NOPASSWD: /usr/local/bin/openhost-checkpoint\n"
                "host ALL=(root) NOPASSWD: /usr/local/bin/openhost-restore\n",
                mode=0o440,
            )

    def _configure_host_containers_conf(self) -> None:
        conf_dir = Path("/home/host/.config/containers")
        conf_path = conf_dir / "containers.conf"
        if conf_path.exists():
            return
        conf_dir.mkdir(parents=True, exist_ok=True)
        # The conda-forge podman bundle ships a "runc" binary that is actually
        # crun under a different name and has no CRIU checkpoint support.
        # Override the default runtime to point "runc" at the real
        # opencontainers runc installed by apt (/usr/sbin/runc).
        write_file(
            str(conf_path),
            '[engine]\n'
            'runtime = "runc"\n'
            '\n'
            '[engine.runtimes]\n'
            'runc = [\n'
            '    "/usr/sbin/runc",\n'
            '    "/usr/bin/runc",\n'
            '    "/usr/local/bin/runc",\n'
            ']\n',
            mode=0o644,
        )
        run("chown", "-R", "host:host", str(conf_dir))

    def _configure_root_containers_conf(self) -> None:
        conf_dir = Path("/root/.config/containers")
        conf_path = conf_dir / "containers.conf"
        conf_dir.mkdir(parents=True, exist_ok=True)
        # podman --runtime flag is ignored for `checkpoint` — it resolves the
        # runtime from the container's stored config ("runc"), then looks up
        # "runc" in root's containers.conf.  Default root config maps "runc" to
        # /usr/bin/runc (no --root flag), so it can't find rootless container
        # state.  We override root's "runc" to point at our wrapper, which
        # injects --root /run/user/<uid>/runc before every runc call.
        write_file(
            str(conf_path),
            '[engine]\n'
            'runtime = "runc"\n'
            '\n'
            '[engine.runtimes]\n'
            'runc = [\n'
            '    "/usr/local/sbin/openhost-runc",\n'
            ']\n',
            mode=0o644,
        )
