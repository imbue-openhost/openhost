"""Apply entrypoint: run migrations at the current checkout, install deps,
then walk forward to the next tag and re-exec self.

At each tag: migrations → pixi install → find next tag → checkout → re-exec.
Migrations run before `pixi install`, so a migration that upgrades the
toolchain (e.g. pixi) takes effect before deps are installed.

The re-exec at each tag means the next step uses that tag's code, so the
migration set and apply logic can evolve between tags.

STABILITY CONTRACT: the previous tagged release's _reexec_apply calls this
file by path. The file path, argv interface, and stdout JSON shape must stay
compatible with the prior tag's caller. Once a new tag is cut, the contract
resets — the new tag's _reexec_apply is the new caller.
"""

from __future__ import annotations

import json
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


def main() -> None:
    project = str(Path(__file__).resolve().parent.parent.parent)

    # Reserve the real stdout for the JSON result and point this process's
    # stdout (inherited by every subprocess) at stderr, so migration and
    # install output can't corrupt the result the parent parses.
    result_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def emit(text: str) -> None:
        with os.fdopen(result_fd, "w") as out:
            out.write(text if text.endswith("\n") else text + "\n")

    applied: list[int] = []

    # Migrations run before install so a toolchain upgrade (e.g. pixi) takes
    # effect before deps are installed for this checkout.
    applied += apply_system_migrations()

    result = subprocess.run([PIXI_BIN, "install"], cwd=project, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        emit(json.dumps({"error": f"pixi install failed (exit {result.returncode}):\n{result.stderr}"}))
        sys.exit(1)

    # Walk to the next tag if one exists.
    tags = _get_sorted_tags(project)
    current = _current_tag(project)
    ref = current or "unknown"

    if current and tags:
        later = [t for t in tags if _version_key(t) > _version_key(current)]
        if later:
            next_tag = later[0]
            subprocess.run(["git", "checkout", next_tag], cwd=project, capture_output=True, check=True, timeout=60)
            subprocess.run(["git", "clean", "-fd"], cwd=project, capture_output=True, check=True, timeout=60)

            # No aggregate timeout: each step bounds its own work (pixi
            # install, git ops), so a host many tags behind can catch up
            # without a single cap killing the walk partway through.
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve())],
                cwd=project,
                capture_output=True,
                text=True,
            )
            if child.returncode != 0:
                emit(child.stdout or json.dumps({"error": child.stderr}))
                sys.exit(1)

            child_result = json.loads(child.stdout)
            applied += child_result.get("system_migrations_applied", [])
            ref = child_result["ref"]

    emit(json.dumps({"ref": ref, "system_migrations_applied": applied, "already_up_to_date": not applied}))


if __name__ == "__main__":
    main()
