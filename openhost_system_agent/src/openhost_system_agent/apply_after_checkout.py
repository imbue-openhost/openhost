"""Apply entrypoint: run this checkout's migrations, install deps, then
tail-call into the next tag.

At each tag: migrations → pixi install → checkout next tag → os.execv self.
Using execv (not a child subprocess) keeps the walk a single process no
matter how many tags the host is behind, and each step still runs that
tag's own code.

Migrations run before `pixi install`, so a migration that upgrades the
toolchain (e.g. pixi) takes effect before deps are installed.

The caller (update.py at the host's starting tag) reads results back from
the migration log and git after this exits, so there is no structured
output contract — success is exit 0, failure is a non-zero exit with the
error on stderr.

STABILITY CONTRACT: the prior tag's update.py invokes this file by path and
depends only on its exit code. Keep the path and that contract stable. Once
a new tag is cut, the contract resets to that tag's update.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from openhost_system_agent.migrations.runner import apply_system_migrations

PIXI_BIN = "/home/host/.pixi/bin/pixi"

# A release tag is "v" followed by dot-separated integers (v1, v1.2, v1.2.3).
# Other v-prefixed tags (e.g. v1.2.0-rc1) are ignored so version parsing
# can't crash on them.
_RELEASE_TAG = re.compile(r"v\d+(?:\.\d+)*")


def _version_key(tag_name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag_name.lstrip("v").split("."))


def _get_sorted_tags(project: str) -> list[str]:
    result = subprocess.run(["git", "tag", "-l", "v*"], cwd=project, capture_output=True, text=True, timeout=60)
    tags = [t for t in result.stdout.strip().split("\n") if _RELEASE_TAG.fullmatch(t)]
    return sorted(tags, key=_version_key)


def _current_tag(project: str) -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=60,
    )
    tag = result.stdout.strip()
    return tag if result.returncode == 0 and _RELEASE_TAG.fullmatch(tag) else None


def _next_tag(project: str) -> str | None:
    current = _current_tag(project)
    if current is None:
        return None
    later = [t for t in _get_sorted_tags(project) if _version_key(t) > _version_key(current)]
    return later[0] if later else None


def main() -> None:
    project = str(Path(__file__).resolve().parent.parent.parent)

    # Migrations run before install so a toolchain upgrade (e.g. pixi) takes
    # effect before deps are installed for this checkout.
    apply_system_migrations()

    result = subprocess.run([PIXI_BIN, "install"], cwd=project, timeout=300)
    if result.returncode != 0:
        print(f"pixi install failed (exit {result.returncode})", file=sys.stderr)
        sys.exit(1)

    nxt = _next_tag(project)
    if nxt:
        subprocess.run(["git", "checkout", nxt], cwd=project, check=True, timeout=60)
        subprocess.run(["git", "clean", "-fd"], cwd=project, check=True, timeout=60)
        # Tail-call into the next tag's code, replacing this process so the
        # walk stays a single process regardless of how many tags we're behind.
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())])


if __name__ == "__main__":
    main()
