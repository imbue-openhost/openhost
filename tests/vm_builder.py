"""Build a local test VM image.

Boots Ubuntu 24.04 in QEMU, runs ansible/setup.yml against it, and saves the
resulting qcow2. Skips rebuild if no git-tracked file under ansible/ has
changed since the last successful build.

mac: requires `brew install qemu`. apple silicon also uses the edk2 firmware
that ships with brew qemu. linux: requires qemu, kvm, genisoimage.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIBLE_DIR = REPO_ROOT / "ansible"
CACHE_DIR = Path(__file__).resolve().parent / ".vm-cache"

UBUNTU_BASE_URL = "https://cloud-images.ubuntu.com/noble/current"
UBUNTU_IMG = {
    "amd64": "noble-server-cloudimg-amd64.img",
    "arm64": "noble-server-cloudimg-arm64.img",
}

DISK_SIZE = "20G"
RAM_MB = 4096
CPUS = 4
SSH_PORT = 2222
SSH_TIMEOUT_S = 600


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("arm64", "aarch64"):
        return "arm64"
    raise RuntimeError(f"unsupported arch: {m}")


def _require(cmd: str) -> str:
    p = shutil.which(cmd)
    if not p:
        raise RuntimeError(f"required command not found: {cmd!r}. on mac: brew install qemu ansible")
    return p


def _ansible_hash() -> str:
    """SHA-256 over the content of every git-tracked file under ansible/."""
    out = subprocess.check_output(["git", "-C", str(REPO_ROOT), "ls-files", "ansible"], text=True)
    h = hashlib.sha256()
    for rel in sorted(out.splitlines()):
        p = REPO_ROOT / rel
        if not p.is_file():
            continue
        h.update(rel.encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _download_base_image(arch: str) -> Path:
    dest = CACHE_DIR / f"base-{arch}.img"
    if dest.exists():
        return dest
    img_name = UBUNTU_IMG[arch]
    print(f"downloading {img_name} ...", file=sys.stderr)
    tmp = dest.with_suffix(".img.part")
    subprocess.check_call(["curl", "-L", "--fail", "-o", str(tmp), f"{UBUNTU_BASE_URL}/{img_name}"])
    tmp.rename(dest)
    return dest


def _ensure_ssh_key() -> Path:
    key = CACHE_DIR / "ssh_key"
    if not key.exists():
        subprocess.check_call(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-q"])
    return key


def _make_seed_iso(pubkey: str) -> Path:
    iso = CACHE_DIR / "seed.iso"
    seed_dir = CACHE_DIR / "seed"
    if seed_dir.exists():
        shutil.rmtree(seed_dir)
    seed_dir.mkdir(parents=True)

    (seed_dir / "user-data").write_text(
        "#cloud-config\n"
        "users:\n"
        "  - name: ubuntu\n"
        "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        "    shell: /bin/bash\n"
        "    ssh_authorized_keys:\n"
        f"      - {pubkey.strip()}\n"
        "ssh_pwauth: false\n"
    )
    (seed_dir / "meta-data").write_text("instance-id: openhost-test\nlocal-hostname: openhost-test\n")

    if iso.exists():
        iso.unlink()
    if sys.platform == "darwin":
        subprocess.check_call(
            [
                "hdiutil",
                "makehybrid",
                "-quiet",
                "-iso",
                "-joliet",
                "-default-volume-name",
                "CIDATA",
                "-o",
                str(iso),
                str(seed_dir),
            ]
        )
    else:
        mkisofs = shutil.which("genisoimage") or shutil.which("mkisofs")
        if not mkisofs:
            raise RuntimeError("install genisoimage (linux) to build the seed iso")
        subprocess.check_call(
            [
                mkisofs,
                "-output",
                str(iso),
                "-volid",
                "CIDATA",
                "-joliet",
                "-rock",
                str(seed_dir / "user-data"),
                str(seed_dir / "meta-data"),
            ]
        )
    return iso


def _make_working_disk(base: Path, dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    shutil.copy(base, dest)
    subprocess.check_call([_require("qemu-img"), "resize", str(dest), DISK_SIZE])


def _qemu_cmd(arch: str, disk: Path, seed: Path) -> list[str]:
    accel = "hvf" if sys.platform == "darwin" else "kvm"

    if arch == "arm64":
        binary = _require("qemu-system-aarch64")
        for prefix in ("/opt/homebrew/share/qemu", "/usr/local/share/qemu", "/usr/share/qemu"):
            code = Path(prefix) / "edk2-aarch64-code.fd"
            vars_template = Path(prefix) / "edk2-arm-vars.fd"
            if code.exists() and vars_template.exists():
                break
        else:
            raise RuntimeError("edk2 firmware not found; install brew qemu")
        vars_copy = CACHE_DIR / "edk2-arm-vars.fd"
        if not vars_copy.exists():
            shutil.copy(vars_template, vars_copy)
        machine = ["-M", "virt", "-cpu", "host"]
        firmware = [
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={code}",
            "-drive",
            f"if=pflash,format=raw,file={vars_copy}",
        ]
    else:
        binary = _require("qemu-system-x86_64")
        machine = ["-M", "q35", "-cpu", "host"]
        firmware = []

    return [
        binary,
        *machine,
        "-accel",
        accel,
        *firmware,
        "-m",
        str(RAM_MB),
        "-smp",
        str(CPUS),
        "-display",
        "none",
        "-serial",
        f"file:{CACHE_DIR / 'serial.log'}",
        "-drive",
        f"if=virtio,file={disk},format=qcow2",
        "-drive",
        f"if=virtio,file={seed},format=raw,readonly=on",
        "-netdev",
        f"user,id=n0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22",
        "-device",
        "virtio-net-pci,netdev=n0",
        "-rtc",
        "base=utc",
    ]


def _ssh_cmd(key: Path) -> list[str]:
    return [
        "ssh",
        "-i",
        str(key),
        "-p",
        str(SSH_PORT),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "LogLevel=ERROR",
        "ubuntu@127.0.0.1",
    ]


def _wait_ssh(key: Path, timeout: int = SSH_TIMEOUT_S) -> None:
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", SSH_PORT), timeout=2):
                pass
        except OSError as e:
            last_err = str(e)
            time.sleep(2)
            continue
        r = subprocess.run(
            _ssh_cmd(key) + ["true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if r.returncode == 0:
            return
        last_err = r.stderr.strip()
        time.sleep(3)
    raise TimeoutError(f"VM not SSH-reachable after {timeout}s; last: {last_err}")


def _run_ansible(key: Path) -> None:
    # Use a non-127.0.0.1 alias so ansible's synchronize module doesn't
    # short-circuit to local rsync (which would write to the Mac fs).
    inv = CACHE_DIR / "inventory.ini"
    inv.write_text(f"[vm]\nopenhost-test ansible_host=127.0.0.1 ansible_port={SSH_PORT}\n")
    env = os.environ.copy()
    env["ANSIBLE_HOST_KEY_CHECKING"] = "False"
    cmd = [
        _require("ansible-playbook"),
        str(ANSIBLE_DIR / "setup.yml"),
        "-i",
        str(inv),
        "-e",
        "domain=test.local",
        "-e",
        "initial_user=ubuntu",
        "-e",
        f"ansible_ssh_private_key_file={key}",
        "--ssh-extra-args=-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        "--skip-tags",
        "acme_key",
    ]
    subprocess.check_call(cmd, env=env)


def _shutdown(key: Path, qemu: subprocess.Popen) -> None:
    subprocess.run(
        _ssh_cmd(key) + ["sudo", "poweroff"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        qemu.wait(timeout=60)
    except subprocess.TimeoutExpired:
        qemu.terminate()
        qemu.wait(timeout=10)


def build_vm_image(force: bool = False) -> Path:
    """Build (or rebuild) the test VM image. Returns the qcow2 path.

    No-op if no git-tracked ansible/ file changed since the last build.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    built = CACHE_DIR / "built.qcow2"
    sidecar = CACHE_DIR / "built.qcow2.hash"
    h = _ansible_hash()

    if not force and built.exists() and sidecar.exists() and sidecar.read_text().strip() == h:
        return built

    arch = _arch()
    base = _download_base_image(arch)
    key = _ensure_ssh_key()
    pubkey = (CACHE_DIR / "ssh_key.pub").read_text()
    seed = _make_seed_iso(pubkey)

    work = CACHE_DIR / "build.qcow2"
    _make_working_disk(base, work)

    cmd = _qemu_cmd(arch, work, seed)
    print("booting build VM ...", file=sys.stderr)
    qemu = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    try:
        _wait_ssh(key)
        print("running ansible ...", file=sys.stderr)
        _run_ansible(key)
        print("shutting down ...", file=sys.stderr)
        _shutdown(key, qemu)
    except BaseException:
        qemu.terminate()
        with contextlib.suppress(Exception):
            qemu.wait(timeout=10)
        raise

    if built.exists():
        built.unlink()
    work.rename(built)
    sidecar.write_text(h)
    return built


@dataclass
class RunningVM:
    ssh_port: int
    ssh_key: Path
    overlay: Path
    _qemu: subprocess.Popen

    def ssh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(_ssh_cmd(self.ssh_key) + list(args), check=check)


@contextlib.contextmanager
def boot_vm_image():
    """Boot the built image in an ephemeral COW overlay; yield a RunningVM."""
    image = build_vm_image()
    arch = _arch()
    key = _ensure_ssh_key()
    overlay = CACHE_DIR / "running.qcow2"
    if overlay.exists():
        overlay.unlink()
    subprocess.check_call(
        [
            _require("qemu-img"),
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(image.resolve()),
            str(overlay),
        ]
    )
    seed = CACHE_DIR / "seed.iso"
    qemu = subprocess.Popen(_qemu_cmd(arch, overlay, seed), stdin=subprocess.DEVNULL)
    try:
        _wait_ssh(key)
        yield RunningVM(ssh_port=SSH_PORT, ssh_key=key, overlay=overlay, _qemu=qemu)
    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                _ssh_cmd(key) + ["sudo", "poweroff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        try:
            qemu.wait(timeout=30)
        except subprocess.TimeoutExpired:
            qemu.terminate()
            qemu.wait(timeout=10)


if __name__ == "__main__":
    print(build_vm_image(force="--force" in sys.argv))
