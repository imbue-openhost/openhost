from __future__ import annotations

from openhost_system_agent.migrations.versions.v0002_baseline import RECLAIM_EXEC_START_PRE
from openhost_system_agent.migrations.versions.v0002_baseline import build_openhost_service_unit


class TestOpenhostServiceUnit:
    def test_contains_privileged_reclaim_execstartpre(self) -> None:
        unit = build_openhost_service_unit(1001)
        # The reclaim must run as root (`+` prefix) before ExecStart.
        assert "ExecStartPre=+/bin/sh -c" in unit
        assert "chown -Rh host:host" in unit
        assert "/home/host/.pixi" in unit
        assert "/home/host/openhost/.pixi" in unit

    def test_reclaim_runs_before_execstart(self) -> None:
        unit = build_openhost_service_unit(1001)
        assert unit.index("ExecStartPre=+") < unit.index("ExecStart=/home/host/.pixi/bin/pixi run")

    def test_uses_the_shared_reclaim_constant(self) -> None:
        # The exact ExecStartPre line is a module constant so migrations that
        # rewrite the unit stay byte-identical with the baseline.
        assert RECLAIM_EXEC_START_PRE in build_openhost_service_unit(1234)

    def test_host_uid_is_substituted(self) -> None:
        unit = build_openhost_service_unit(4242)
        assert "XDG_RUNTIME_DIR=/run/user/4242" in unit
        assert "user@4242.service" in unit
