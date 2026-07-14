"""Download pinned, arch-specific binaries compute_space installs at RUNTIME.

Some tools we depend on at runtime aren't packaged for every arch we run on, so
we fetch a pinned vendor release and verify its sha256 before use.  The pins are
declared as data in ``_MANIFEST`` below -- one entry per program, with the
download URL + sha256 for each arch.  Adding a pinned tool or arch is a data-only
change here.  Caller: ``archive_backend`` (JuiceFS).  Provisioning-time binaries
(e.g. CoreDNS) are pinned in their own ansible task, not here."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import urllib.error
import urllib.request

import attr

from compute_space.core.logging import logger


@attr.s(auto_attribs=True, frozen=True)
class ArchAsset:
    url: str
    sha256: str


@attr.s(auto_attribs=True, frozen=True)
class PinnedBinary:
    name: str
    version: str
    archive_member: str
    # arch string ("amd64" / "arm64") -> where to get it.  A genuine same-typed
    # mapping, so a dict is the right shape here.
    assets: dict[str, ArchAsset]

    def asset_for(self, arch: str) -> ArchAsset:
        asset = self.assets.get(arch)
        if asset is None:
            raise RuntimeError(f"No pinned {self.name} download for arch {arch!r}.")
        return asset


# Pinned runtime binaries, keyed by name.  URLs point at the vendor's GitHub
# release; bump both the version and the per-arch sha256 together.
_MANIFEST: dict[str, PinnedBinary] = {
    "juicefs": PinnedBinary(
        name="juicefs",
        version="1.3.1",
        archive_member="juicefs",  # file to extract from the tarball
        assets={
            "amd64": ArchAsset(
                url="https://github.com/juicedata/juicefs/releases/download/v1.3.1/juicefs-1.3.1-linux-amd64.tar.gz",
                sha256="eb67a7be5d174b420cb3734d441971b3a462ab522b78ad2a6ed993e7deddcd44",
            ),
            "arm64": ArchAsset(
                url="https://github.com/juicedata/juicefs/releases/download/v1.3.1/juicefs-1.3.1-linux-arm64.tar.gz",
                sha256="c29bff8f609366011cee03b9abcc76c11a06308b2c314364b8c340a2bfbc6c48",
            ),
        },
    ),
}


def host_arch() -> str:
    """Return the release-asset arch string ("amd64" / "arm64") for this host."""
    machine = os.uname().machine
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


def get_pinned_binary(name: str) -> PinnedBinary:
    binary = _MANIFEST.get(name)
    if binary is None:
        raise RuntimeError(f"No pinned binary named {name!r} declared in pinned_binary.py.")
    return binary


def install_pinned_binary(binary: PinnedBinary, dest_path: str) -> None:
    """Download + verify sha256 + extract ``binary`` to ``dest_path``.  Idempotent."""
    if os.path.isfile(dest_path) and os.access(dest_path, os.X_OK):
        return
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    arch = host_arch()
    asset = binary.asset_for(arch)
    logger.info("Downloading %s %s for %s", binary.name, binary.version, arch)
    try:
        with urllib.request.urlopen(asset.url, timeout=120) as resp:
            tarball_bytes = resp.read()
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"Failed to download {binary.name}: {exc}") from exc

    actual_sha = hashlib.sha256(tarball_bytes).hexdigest()
    if actual_sha != asset.sha256:
        raise RuntimeError(
            f"{binary.name} tarball sha256 mismatch (expected {asset.sha256}, got {actual_sha}).  Refusing to install."
        )

    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        member = next((m for m in tar.getmembers() if m.name == binary.archive_member), None)
        if member is None:
            raise RuntimeError(f"{binary.name} tarball missing the {binary.archive_member!r} entry")
        f = tar.extractfile(member)
        if f is None:
            raise RuntimeError(f"{binary.name} tarball entry {binary.archive_member!r} was unreadable")
        with f, open(dest_path, "wb") as out:
            shutil.copyfileobj(f, out)
    os.chmod(dest_path, 0o700)
    logger.info("%s installed at %s", binary.name, dest_path)
