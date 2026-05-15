from __future__ import annotations

import json
import sys
from typing import Annotated

import attrs
import cappa

from openhost_system_agent.update import apply_update
from openhost_system_agent.update import fetch_updates
from openhost_system_agent.update import get_remote_info
from openhost_system_agent.update import set_remote_url
from openhost_system_agent.update import show_diff


def _output(data: dict[str, object]) -> None:
    print(json.dumps(data))


def _error(msg: str) -> None:
    _output({"ok": False, "error": msg})
    raise SystemExit(1)


@cappa.command(name="update", help="Manage code updates and system migrations.")
@attrs.define
class UpdateCmd:
    @cappa.command(name="fetch", help="Fetch latest code from remote.")
    def fetch(self) -> None:
        try:
            result = fetch_updates()
        except Exception as e:
            _error(str(e))
        _output({"ok": True, **result})

    @cappa.command(name="show_diff", help="Show pending changes between HEAD and remote.")
    def show_diff(self) -> None:
        try:
            result = show_diff()
        except Exception as e:
            _error(str(e))
        _output({"ok": True, **result})

    @cappa.command(name="apply", help="Apply pending update: checkout, install deps, run system migrations.")
    def apply(self) -> None:
        try:
            result = apply_update()
        except Exception as e:
            _error(str(e))
        _output({"ok": True, **result})

    @cappa.command(name="set_remote", help="Set the git remote URL.")
    def set_remote(
        self,
        url: Annotated[str, cappa.Arg(help="Git remote URL")],
    ) -> None:
        try:
            result = set_remote_url(url)
        except Exception as e:
            _error(str(e))
        _output({"ok": True, **result})

    @cappa.command(name="get_remote", help="Get the current git remote URL and ref.")
    def get_remote(self) -> None:
        try:
            result = get_remote_info()
        except Exception as e:
            _error(str(e))
        _output({"ok": True, **result})


@cappa.command(
    name="openhost_system_agent",
    help="OpenHost system agent — host-level updates and migrations.",
)
@attrs.define
class SystemAgent:
    subcommand: cappa.Subcommands[UpdateCmd]


def main() -> None:
    if len(sys.argv) == 1:
        sys.argv.append("--help")
    cappa.invoke(SystemAgent, color=False)
