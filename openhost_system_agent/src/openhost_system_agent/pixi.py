"""Pinned pixi version and the helper that enforces it on a host."""

from __future__ import annotations

import subprocess

PIXI_VERSION = "0.70.2"
PIXI_BIN = "/home/host/.pixi/bin/pixi"


def ensure_pixi_version() -> None:
    """Pin the host's pixi to PIXI_VERSION. Idempotent."""
    try:
        result = subprocess.run(
            [PIXI_BIN, "self-update", "--version", PIXI_VERSION],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"pixi self-update to {PIXI_VERSION} timed out after 120s") from e
    if result.returncode != 0:
        raise RuntimeError(f"pixi self-update to {PIXI_VERSION} failed (exit {result.returncode}):\n{result.stderr}")
