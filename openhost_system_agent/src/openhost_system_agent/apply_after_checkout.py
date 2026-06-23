"""Apply entrypoint invoked by the old code after git checkout.

Runs: pre_install migrations → pixi install → post_install migrations.
Prints a JSON ApplyResult to stdout.

The editable install (.pth) means the freshly checked-out source tree is
already importable — no pixi install needed to reach migration code.
Only stdlib + the editable package are used before pixi install.

STABILITY CONTRACT: this file's path relative to the repo root, its argv
interface, and its stdout JSON shape are load-bearing for every deployed
host. Changes must be backwards-compatible.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from openhost_system_agent.migrations.runner import apply_system_migrations

PIXI_BIN = "/home/host/.pixi/bin/pixi"


def main() -> None:
    project = str(Path(__file__).resolve().parent.parent.parent)
    applied: list[int] = []

    applied += apply_system_migrations(phase="pre_install")

    result = subprocess.run([PIXI_BIN, "install"], cwd=project, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(json.dumps({"error": f"pixi install failed (exit {result.returncode}):\n{result.stderr}"}))
        sys.exit(1)

    applied += apply_system_migrations(phase="post_install")

    ref = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    print(json.dumps({"ref": ref, "system_migrations_applied": applied, "already_up_to_date": False}))


if __name__ == "__main__":
    main()
