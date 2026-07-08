"""Download pinned, arch-specific binaries declared in ``pinned_binaries.toml``.

Some tools we depend on at runtime aren't packaged for every arch we run on, so
we fetch a pinned vendor release and verify its sha256 before use.  The pins
live in ``pinned_binaries.toml``; this module just reads it and installs from
it.  Caller: ``archive_backend`` (JuiceFS).  Provisioning-time binaries (e.g.
CoreDNS) are pinned in their own ansible task, not here."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import tarfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import attr

from compute_space.core.logging import logger

_MANIFEST_PATH = Path(__file__).parent / "pinned_binaries.toml"


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
            raise RuntimeError(f"No pinned {self.name} download for arch {arch!r} in {_MANIFEST_PATH.name}.")
        return asset


def host_arch() -> str:
    """Return the release-asset arch string ("amd64" / "arm64") for this host."""
    machine = os.uname().machine
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return "amd64"


def _parse_binary(name: str, spec: dict[str, Any]) -> PinnedBinary:
    assets = {arch: ArchAsset(url=a["url"], sha256=a["sha256"]) for arch, a in spec["arch"].items()}
    return PinnedBinary(name=name, version=spec["version"], archive_member=spec["archive_member"], assets=assets)


def _load_manifest() -> dict[str, PinnedBinary]:
    with open(_MANIFEST_PATH, "rb") as f:
        raw = tomllib.load(f)
    return {name: _parse_binary(name, spec) for name, spec in raw.items()}


_MANIFEST = _load_manifest()


def get_pinned_binary(name: str) -> PinnedBinary:
    binary = _MANIFEST.get(name)
    if binary is None:
        raise RuntimeError(f"No pinned binary named {name!r} in {_MANIFEST_PATH.name}.")
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
    os.chmod(dest_path, 0o750)
    logger.info("%s installed at %s", binary.name, dest_path)
