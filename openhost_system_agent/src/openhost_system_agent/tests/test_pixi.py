from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from openhost_system_agent import pixi


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


class TestEnsurePixiVersion:
    def test_runs_self_update_as_host_when_root(self) -> None:
        with (
            patch("os.geteuid", return_value=0),
            patch("subprocess.run", return_value=_ok()) as mock_run,
        ):
            pixi.ensure_pixi_version()

        cmd = mock_run.call_args.args[0]
        # Must drop to the host user so self-update never leaves root-owned
        # files under /home/host/.pixi.
        assert cmd[:4] == ["sudo", "-u", pixi.HOST_USER, "-H"]
        assert cmd[4:] == [pixi.PIXI_BIN, "self-update", "--version", pixi.PIXI_VERSION]

    def test_runs_pixi_directly_when_not_root(self) -> None:
        with (
            patch("os.geteuid", return_value=1000),
            patch("subprocess.run", return_value=_ok()) as mock_run,
        ):
            pixi.ensure_pixi_version()

        cmd = mock_run.call_args.args[0]
        assert cmd == [pixi.PIXI_BIN, "self-update", "--version", pixi.PIXI_VERSION]

    def test_raises_on_failure(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
        with (
            patch("os.geteuid", return_value=0),
            patch("subprocess.run", return_value=failed),
        ):
            with pytest.raises(RuntimeError, match="self-update"):
                pixi.ensure_pixi_version()
