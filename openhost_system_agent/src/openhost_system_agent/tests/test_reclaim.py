from __future__ import annotations

from unittest.mock import patch

import pytest

from openhost_system_agent import reclaim


class TestReclaimPixiOwnership:
    def test_refuses_when_not_root(self) -> None:
        with patch("os.geteuid", return_value=1000):
            with pytest.raises(RuntimeError, match="must be run as root"):
                reclaim.reclaim_pixi_ownership()

    def test_chowns_existing_paths_as_root(self) -> None:
        # Both known pixi paths exist -> both chowned to host:host, recursively,
        # with symlinks handled in place (-h).
        with (
            patch("os.geteuid", return_value=0),
            patch("os.path.exists", return_value=True),
            patch("subprocess.run") as mock_run,
        ):
            reclaim.reclaim_pixi_ownership()

        called_paths = [call.args[0] for call in mock_run.call_args_list]
        assert called_paths == [
            ["chown", "-Rh", "host:host", "/home/host/.pixi"],
            ["chown", "-Rh", "host:host", "/home/host/openhost/.pixi"],
        ]
        for call in mock_run.call_args_list:
            assert call.kwargs["check"] is True

    def test_skips_missing_paths(self) -> None:
        # Only the second path exists -> only it is chowned; a fresh host
        # without the project env yet must not error.
        def fake_exists(path: str) -> bool:
            return path == "/home/host/openhost/.pixi"

        with (
            patch("os.geteuid", return_value=0),
            patch("os.path.exists", side_effect=fake_exists),
            patch("subprocess.run") as mock_run,
        ):
            reclaim.reclaim_pixi_ownership()

        called_paths = [call.args[0] for call in mock_run.call_args_list]
        assert called_paths == [["chown", "-Rh", "host:host", "/home/host/openhost/.pixi"]]

    def test_noop_when_no_paths_exist(self) -> None:
        with (
            patch("os.geteuid", return_value=0),
            patch("os.path.exists", return_value=False),
            patch("subprocess.run") as mock_run,
        ):
            reclaim.reclaim_pixi_ownership()
        mock_run.assert_not_called()
