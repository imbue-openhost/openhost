"""Apply entrypoint: run migrations at the current checkout, then continue
walking forward through tags.

At each tag: pre_pixi_install migrations → pixi install → post_pixi_install
migrations → find next tag → checkout → re-exec self.

The re-exec at each tag means the next step uses that tag's code and its
own copy of migration_tags.json, so the format can evolve between tags.

STABILITY CONTRACT: the previous tagged release's _reexec_apply calls
this file by path. The file path, argv interface, and stdout JSON shape
must stay compatible with the prior tag's caller. Once a new tag is cut,
the contract resets — the new tag's _reexec_apply is the new caller.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from openhost_system_agent.migrations.runner import apply_system_migrations

PIXI_BIN = "/home/host/.pixi/bin/pixi"


def _version_key(tag_name: str) -> tuple[int, ...]:
    return tuple(int(x) for x in tag_name.lstrip("v").split("."))


def _get_sorted_tags(project: str) -> list[str]:
    result = subprocess.run(["git", "tag", "-l", "v*"], cwd=project, capture_output=True, text=True)
    tags = [t for t in result.stdout.strip().split("\n") if t]
    return sorted(tags, key=_version_key)


def _current_tag(project: str) -> str | None:
    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=project,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    project = str(Path(__file__).resolve().parent.parent.parent)
    applied: list[int] = []

    # Run migrations and install deps at this checkout.
    applied += apply_system_migrations(phase="pre_pixi_install")

    result = subprocess.run([PIXI_BIN, "install"], cwd=project, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(json.dumps({"error": f"pixi install failed (exit {result.returncode}):\n{result.stderr}"}))
        sys.exit(1)

    applied += apply_system_migrations(phase="post_pixi_install")

    # Walk to the next tag if one exists.
    tags = _get_sorted_tags(project)
    current = _current_tag(project)
    ref = current or "unknown"

    if current and tags:
        later = [t for t in tags if _version_key(t) > _version_key(current)]
        if later:
            next_tag = later[0]
            subprocess.run(["git", "checkout", next_tag], cwd=project, capture_output=True, check=True)
            subprocess.run(["git", "clean", "-fd"], cwd=project, capture_output=True, check=True)

            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve())],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if child.returncode != 0:
                print(child.stdout or json.dumps({"error": child.stderr}))
                sys.exit(1)

            child_result = json.loads(child.stdout)
            applied += child_result.get("system_migrations_applied", [])
            ref = child_result["ref"]

    print(json.dumps({"ref": ref, "system_migrations_applied": applied, "already_up_to_date": not applied}))


if __name__ == "__main__":
    main()
