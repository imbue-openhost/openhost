"""Tests for the diagnostics collectors and their HTTP endpoints."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import mock_open
from unittest.mock import patch

import attr
import git
import httpx
import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.config import Config
from compute_space.core import diagnostics
from compute_space.core.app_id import new_app_id
from compute_space.core.diagnostics import DIAGNOSTICS_SCHEMA_VERSION
from compute_space.core.diagnostics import AppHealth
from compute_space.core.diagnostics import GitInfo
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import _app_diagnostics_filename
from compute_space.web.routes.api.apps import api_apps_routes
from compute_space.web.routes.api.system import _diagnostics_filename
from compute_space.web.routes.api.system import system_routes


def _init_git_repo(path: Path, *, remote_url: str | None = None) -> git.Repo:
    """Create a git repo at ``path`` with one commit. Optional 'origin' remote."""
    path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(path, initial_branch="main")
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")
    (path / "README.md").write_text("hello\n")
    repo.index.add(["README.md"])
    repo.index.commit("initial commit")
    if remote_url is not None:
        repo.create_remote("origin", remote_url)
    return repo


_MINIMAL_MANIFEST = """
[app]
name = "myapp"
version = "2.3.4"

[runtime.container]
image = "docker.io/library/nginx:latest"
port = 80
"""


@pytest.fixture(autouse=True)
def _no_real_network(request: Any) -> Iterator[None]:
    """Keep the suite hermetic + fast: stub the collectors that would otherwise
    make real outbound HTTP calls (reachability probes, per-app health checks).

    The higher-level endpoint/bundle tests don't care about the exact network
    result, so we stub it. Tests that exercise the network collectors directly
    mark themselves ``@pytest.mark.real_collectors`` to opt out of this stub and
    instead patch ``httpx.AsyncClient`` themselves.
    """
    if request.node.get_closest_marker("real_collectors"):
        yield
        return

    async def _fake_reachability(_config: Any) -> list[Any]:
        return []

    async def _fake_health(_local_port: Any, health_check: Any) -> AppHealth:
        path = health_check or "/"
        if not path.startswith("/"):
            path = "/" + path
        return AppHealth(checked=False, healthy=None, status_code=None, checked_path=path, error="stubbed")

    with (
        patch("compute_space.core.diagnostics._collect_reachability", side_effect=_fake_reachability),
        patch("compute_space.core.diagnostics._collect_app_health", side_effect=_fake_health),
    ):
        yield


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_app(
    db_path: str,
    name: str,
    *,
    status: str = "running",
    version: str = "1.0",
    manifest_raw: str | None = None,
    repo_path: str = "/r",
) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, runtime_type, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, ?, ?, 'serverfull', ?, 19500, ?, ?)",
            (app_id, name, version, repo_path, status, manifest_raw),
        )
        db.commit()
    finally:
        db.close()
    return app_id


# ─── unit tests for low-level collectors ─────────────────────────────────────


def test_collect_system_info_populated() -> None:
    info = diagnostics._collect_system_info()
    assert info.system  # e.g. "Linux"
    assert info.python_version
    assert info.python_implementation


def test_collect_dependencies_marks_missing() -> None:
    deps = diagnostics._collect_dependencies()
    # Every curated dependency must appear (installed or explicitly not).
    assert set(deps) == set(diagnostics._KEY_DEPENDENCIES)
    # litestar is a hard runtime dep, so it must resolve to a real version.
    assert deps["litestar"] != "(not installed)"


def test_manifest_fields_parses_version() -> None:
    version, runtime_type = diagnostics._manifest_fields(_MINIMAL_MANIFEST)
    assert version == "2.3.4"
    assert runtime_type == "serverfull"


def test_manifest_fields_handles_bad_manifest() -> None:
    assert diagnostics._manifest_fields("not valid toml [[[") == (None, None)
    assert diagnostics._manifest_fields(None) == (None, None)
    assert diagnostics._manifest_fields("") == (None, None)


def test_collect_container_runtime_no_podman() -> None:
    with patch("compute_space.core.diagnostics.shutil.which", return_value=None):
        info = diagnostics._collect_container_runtime()
    assert info.available is False
    assert info.rootless is None
    assert info.error is not None


def test_collect_container_runtime_rootless_true() -> None:
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"version": {"Version": "4.9.3"}, "host": {"security": {"rootless": True}}}),
        stderr="",
    )
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is True
    assert info.version == "4.9.3"
    assert info.rootless is True
    assert info.error is None


def test_collect_container_runtime_non_json() -> None:
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is True
    assert info.rootless is None
    assert info.error is not None


# ─── platform diagnostics endpoint ───────────────────────────────────────────


@pytest.fixture
def system_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(system_routes)) as c:
        yield c


def test_platform_diagnostics_requires_auth(system_client: TestClient[Litestar]) -> None:
    resp = system_client.get("/api/diagnostics")
    assert resp.status_code in (401, 403)


def test_platform_diagnostics_returns_bundle(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    _seed_app(cfg.db_path, "myapp", manifest_raw=_MINIMAL_MANIFEST)
    resp = system_client.get("/api/diagnostics", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert body["generated_at"]
    assert body["zone_domain"] == cfg.zone_domain
    assert "openhost" in body
    assert "system" in body
    assert body["system"]["python_version"]
    assert "container_runtime" in body
    assert "dependencies" in body
    assert body["dependencies"]["litestar"]
    # The seeded app appears, with the version re-parsed from its manifest.
    names = {a["name"]: a for a in body["apps"]}
    assert "myapp" in names
    assert names["myapp"]["version"] == "2.3.4"


def test_platform_diagnostics_download_header(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    resp = system_client.get("/api/diagnostics?download=1", cookies=cookies)
    assert resp.status_code == 200
    disp = resp.headers.get("content-disposition", "")
    assert "attachment" in disp
    assert ".json" in disp


def test_platform_diagnostics_version_falls_back_to_column(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    # No manifest_raw -> version must fall back to the stored apps.version column.
    _seed_app(cfg.db_path, "noman", version="9.9", manifest_raw=None)
    resp = system_client.get("/api/diagnostics", cookies=cookies)
    names = {a["name"]: a for a in resp.json()["apps"]}
    assert names["noman"]["version"] == "9.9"


# ─── per-app diagnostics endpoint ─────────────────────────────────────────────


@pytest.fixture
def apps_client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


def test_app_diagnostics_requires_auth(apps_client: TestClient[Litestar]) -> None:
    resp = apps_client.get(f"/api/app_diagnostics/{new_app_id()}")
    assert resp.status_code in (401, 403)


def test_app_diagnostics_404_when_missing(apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = apps_client.get(f"/api/app_diagnostics/{new_app_id()}", cookies=cookies)
    assert resp.status_code == 404


def test_app_diagnostics_400_on_bad_id(apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = apps_client.get("/api/app_diagnostics/not a valid id", cookies=cookies)
    assert resp.status_code == 400


def test_app_diagnostics_returns_bundle(cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", manifest_raw=_MINIMAL_MANIFEST)
    resp = apps_client.get(f"/api/app_diagnostics/{app_id}", cookies=cookies)
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert body["app_id"] == app_id
    assert body["name"] == "myapp"
    assert body["version"] == "2.3.4"
    assert body["runtime_type"] == "serverfull"
    assert body["status"] == "running"
    # Self-contained: carries system + openhost info too.
    assert body["system"]["python_version"]
    assert "container_runtime" in body
    assert "openhost" in body


def test_app_diagnostics_download_header(cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp")
    resp = apps_client.get(f"/api/app_diagnostics/{app_id}?download=1", cookies=cookies)
    assert resp.status_code == 200
    disp = resp.headers.get("content-disposition", "")
    assert "attachment" in disp
    assert "myapp" in disp


# ─── git collector against real repos ────────────────────────────────────────


def test_collect_git_info_none_for_non_git_dir(tmp_path: Path) -> None:
    (tmp_path / "notrepo").mkdir()
    assert asyncio.run(diagnostics._collect_git_info(tmp_path / "notrepo")) is None


def test_collect_git_info_none_for_none_path() -> None:
    assert asyncio.run(diagnostics._collect_git_info(None)) is None


def test_collect_git_info_clean_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path, remote_url="https://github.com/owner/repo.git")
    info = asyncio.run(diagnostics._collect_git_info(repo_path))
    assert info is not None
    assert info.branch == "main"
    assert len(info.sha) == 40
    assert info.short_sha == info.sha[:8]
    assert info.dirty is False
    assert info.remote_url == "https://github.com/owner/repo.git"


def test_collect_git_info_dirty_repo(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)
    (repo_path / "untracked.txt").write_text("dirty\n")
    info = asyncio.run(diagnostics._collect_git_info(repo_path))
    assert info is not None
    assert info.dirty is True


def test_collect_git_info_detached_head(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    repo = _init_git_repo(repo_path)
    repo.git.checkout(repo.head.commit.hexsha)  # detach
    info = asyncio.run(diagnostics._collect_git_info(repo_path))
    assert info is not None
    assert info.branch is None  # detached HEAD reports no branch
    assert info.sha  # but sha is still populated


def test_collect_git_info_no_remote(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path)  # no origin
    info = asyncio.run(diagnostics._collect_git_info(repo_path))
    assert info is not None
    assert info.remote_url is None


def test_collect_git_info_strips_credentials(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    _init_git_repo(repo_path, remote_url="https://oauth2:SECRETTOKEN@github.com/owner/repo.git")
    info = asyncio.run(diagnostics._collect_git_info(repo_path))
    assert info is not None
    assert info.remote_url is not None
    assert "SECRETTOKEN" not in info.remote_url
    assert "github.com/owner/repo.git" in info.remote_url


# ─── podman probe edge cases ─────────────────────────────────────────────────


def test_collect_container_runtime_timeout() -> None:
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch(
            "compute_space.core.diagnostics.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="podman", timeout=10),
        ),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is False
    assert "timed out" in (info.error or "")


def test_collect_container_runtime_nonzero_returncode() -> None:
    fake = subprocess.CompletedProcess(args=[], returncode=125, stdout="", stderr="boom")
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is False
    assert info.error is not None


def test_collect_container_runtime_rootful() -> None:
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"version": {"Version": "5.4.2"}, "host": {"security": {"rootless": False}}}),
        stderr="",
    )
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is True
    assert info.rootless is False
    assert info.version == "5.4.2"


def test_collect_container_runtime_missing_version_key() -> None:
    # Some podman versions may not emit a version table; fall back gracefully.
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({"host": {"security": {"rootless": True}}}),
        stderr="",
    )
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        info = diagnostics._collect_container_runtime()
    assert info.available is True
    assert info.rootless is True
    assert info.version is None


# ─── resilience: partial failures degrade instead of raising ─────────────────


def test_platform_diagnostics_survives_storage_failure(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    with patch(
        "compute_space.core.diagnostics.storage_status",
        side_effect=OSError("disk gone"),
    ):
        resp = system_client.get("/api/diagnostics", cookies=cookies)
    assert resp.status_code == 200
    # Storage degrades to an empty dict rather than sinking the whole bundle.
    assert resp.json()["storage"] == {}


def test_platform_diagnostics_survives_db_failure(cfg: Any) -> None:
    broken = MagicMock()
    broken.execute.side_effect = sqlite3.OperationalError("no such table")
    real_config = Config(**{f.name: getattr(cfg, f.name) for f in attr.fields(Config)})
    with patch("compute_space.core.diagnostics.storage_status", return_value={}):
        diag = asyncio.run(diagnostics.collect_platform_diagnostics(broken, real_config))
    # A failing apps query leaves apps empty but still returns a bundle.
    assert diag.apps == []
    assert diag.system.python_version


def test_platform_diagnostics_no_apps(cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = system_client.get("/api/diagnostics", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["apps"] == []


def test_openhost_git_info_stable_shape_when_not_a_checkout(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    # When OPENHOST_PROJECT_DIR isn't a git checkout, the bundle must still
    # carry an openhost GitInfo with a stable (empty) shape.
    async def _none(_path: Any) -> None:
        return None

    with patch("compute_space.core.diagnostics._collect_git_info", side_effect=_none):
        resp = system_client.get("/api/diagnostics", cookies=cookies)
    assert resp.status_code == 200
    oh = resp.json()["openhost"]
    assert oh["sha"] == ""
    assert oh["branch"] is None
    assert oh["dirty"] is False


# ─── filename sanitization ───────────────────────────────────────────────────


def test_platform_filename_sanitizes_and_stamps() -> None:
    name = _diagnostics_filename("my.zone.example.com:8443")
    assert name.startswith("openhost-diagnostics-")
    assert name.endswith(".json")
    # ':' is not filename-safe and must be replaced with '_'; the rest of the
    # host (dots included) is preserved verbatim.
    assert ":" not in name
    assert name.startswith("openhost-diagnostics-my.zone.example.com_8443-")


def test_platform_filename_handles_empty_zone() -> None:
    name = _diagnostics_filename("")
    assert "openhost" in name
    assert name.endswith(".json")


def test_app_filename_sanitizes_path_traversal() -> None:
    name = _app_diagnostics_filename("../../etc/passwd")
    # Slashes and dot-dot must not survive into the download filename.
    assert "/" not in name
    assert name.startswith("openhost-app-diagnostics-")
    assert name.endswith(".json")


def test_app_filename_handles_weird_name() -> None:
    name = _app_diagnostics_filename('bad";name\x00')
    assert '"' not in name
    assert "\x00" not in name
    assert name.endswith(".json")


# ─── per-app diagnostics surfaces real git info ───────────────────────────────


def test_app_diagnostics_surfaces_git(
    cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo_path = tmp_path / "app_repo"
    _init_git_repo(repo_path, remote_url="https://github.com/owner/app.git")
    app_id = _seed_app(cfg.db_path, "gitapp", manifest_raw=_MINIMAL_MANIFEST, repo_path=str(repo_path))
    resp = apps_client.get(f"/api/app_diagnostics/{app_id}", cookies=cookies)
    assert resp.status_code == 200
    git_info = resp.json()["git"]
    assert git_info is not None
    assert git_info["branch"] == "main"
    assert git_info["remote_url"] == "https://github.com/owner/app.git"


def test_app_diagnostics_git_none_for_non_git_app(
    cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "builtin", repo_path="/nonexistent")
    resp = apps_client.get(f"/api/app_diagnostics/{app_id}", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["git"] is None


def test_gitinfo_is_frozen() -> None:
    info = GitInfo(branch="main", sha="abc", short_sha="abc", dirty=False, remote_url=None)
    with pytest.raises(attr.exceptions.FrozenInstanceError):
        info.branch = "other"  # type: ignore[misc]


# ─── resource pressure ───────────────────────────────────────────────────────


def test_read_meminfo_parses(tmp_path: Path) -> None:
    meminfo = "MemTotal:       16384000 kB\nMemFree:         1000000 kB\nMemAvailable:    8192000 kB\n"
    m = mock_open(read_data=meminfo)
    with patch("compute_space.core.diagnostics.open", m):
        total, available = diagnostics._read_meminfo()
    assert total == 16384000 * 1024
    assert available == 8192000 * 1024


def test_read_meminfo_missing_file() -> None:
    with patch("compute_space.core.diagnostics.open", side_effect=FileNotFoundError):
        assert diagnostics._read_meminfo() == (None, None)


def test_collect_resource_pressure_computes_percent_and_load() -> None:
    with (
        patch("compute_space.core.diagnostics._read_meminfo", return_value=(1000, 250)),
        patch("compute_space.core.diagnostics.os.getloadavg", return_value=(0.5, 1.0, 2.0)),
        patch("compute_space.core.diagnostics.os.cpu_count", return_value=4),
    ):
        rp = diagnostics._collect_resource_pressure()
    assert rp.memory_total_bytes == 1000
    assert rp.memory_available_bytes == 250
    assert rp.memory_used_percent == 75.0  # (1000-250)/1000
    assert rp.load_avg_1m == 0.5
    assert rp.load_avg_15m == 2.0
    assert rp.cpu_count == 4


def test_collect_resource_pressure_degrades_without_loadavg() -> None:
    with (
        patch("compute_space.core.diagnostics._read_meminfo", return_value=(None, None)),
        patch("compute_space.core.diagnostics.os.getloadavg", side_effect=OSError),
    ):
        rp = diagnostics._collect_resource_pressure()
    assert rp.memory_total_bytes is None
    assert rp.memory_used_percent is None
    assert rp.load_avg_1m is None


# ─── podman stats parsing ─────────────────────────────────────────────────────


def test_parse_stats_bytes() -> None:
    assert diagnostics._parse_stats_bytes("128MB") == 128 * 1000**2
    assert diagnostics._parse_stats_bytes("1.5GiB") == int(1.5 * 1024**3)
    assert diagnostics._parse_stats_bytes("512kB") == 512 * 1000
    assert diagnostics._parse_stats_bytes("42B") == 42
    assert diagnostics._parse_stats_bytes("--") is None
    assert diagnostics._parse_stats_bytes("") is None
    assert diagnostics._parse_stats_bytes(None) is None
    assert diagnostics._parse_stats_bytes("garbage") is None


def test_parse_stats_percent() -> None:
    assert diagnostics._parse_stats_percent("3.14%") == 3.14
    assert diagnostics._parse_stats_percent("0.00%") == 0.0
    assert diagnostics._parse_stats_percent("--") is None
    assert diagnostics._parse_stats_percent(None) is None


def test_collect_app_resources_no_container() -> None:
    r = diagnostics._collect_app_resources(None, 0.5, 256)
    assert r.running is False
    assert r.cpu_cores_limit == 0.5
    assert r.memory_mb_limit == 256
    assert r.cpu_percent is None


def test_collect_app_resources_running_parses_stats() -> None:
    stats = json.dumps([{"CPU": "12.50%", "MemUsage": "64MB / 128MB", "MemPerc": "50.00%"}])
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=stats, stderr="")
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        r = diagnostics._collect_app_resources("cid123", 0.5, 128)
    assert r.running is True
    assert r.cpu_percent == 12.5
    assert r.memory_usage_bytes == 64 * 1000**2
    assert r.memory_limit_bytes == 128 * 1000**2
    assert r.memory_percent == 50.0


def test_collect_app_resources_not_running_when_nonzero() -> None:
    fake = subprocess.CompletedProcess(args=[], returncode=125, stdout="", stderr="no such container")
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch("compute_space.core.diagnostics.subprocess.run", return_value=fake),
    ):
        r = diagnostics._collect_app_resources("cid123", 0.5, 128)
    assert r.running is False
    assert r.error is None


def test_collect_app_resources_stats_timeout() -> None:
    with (
        patch("compute_space.core.diagnostics.shutil.which", return_value="/usr/bin/podman"),
        patch(
            "compute_space.core.diagnostics.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="podman", timeout=10),
        ),
    ):
        r = diagnostics._collect_app_resources("cid123", 0.5, 128)
    assert r.running is False
    assert "timed out" in (r.error or "")


# ─── health checks ────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAsyncClient:
    """Minimal async-context-manager httpx.AsyncClient stand-in."""

    def __init__(self, *, get_result: Any = None, get_exc: Exception | None = None, **_: Any) -> None:
        self._result = get_result
        self._exc = get_exc

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def get(self, _url: str) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.mark.real_collectors
def test_collect_app_health_no_port() -> None:
    h = asyncio.run(diagnostics._collect_app_health(None, None))
    assert h.checked is False
    assert h.healthy is None
    assert h.checked_path == "/"


@pytest.mark.real_collectors
def test_collect_app_health_ok() -> None:
    def _client(**kw: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(get_result=_FakeResp(200))

    with patch("compute_space.core.diagnostics.httpx.AsyncClient", _client):
        h = asyncio.run(diagnostics._collect_app_health(19500, "/healthz"))
    assert h.checked is True
    assert h.healthy is True
    assert h.status_code == 200
    assert h.checked_path == "/healthz"


@pytest.mark.real_collectors
def test_collect_app_health_5xx_is_unhealthy() -> None:
    def _client(**kw: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(get_result=_FakeResp(503))

    with patch("compute_space.core.diagnostics.httpx.AsyncClient", _client):
        h = asyncio.run(diagnostics._collect_app_health(19500, None))
    assert h.healthy is False
    assert h.status_code == 503


@pytest.mark.real_collectors
def test_collect_app_health_connection_error() -> None:
    def _client(**kw: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(get_exc=httpx.ConnectError("refused"))

    with patch("compute_space.core.diagnostics.httpx.AsyncClient", _client):
        h = asyncio.run(diagnostics._collect_app_health(19500, "healthz"))
    assert h.checked is True
    assert h.healthy is False
    assert h.status_code is None
    assert h.checked_path == "/healthz"  # leading slash normalized


# ─── reachability ─────────────────────────────────────────────────────────────


def test_reachability_targets_includes_config_urls(tmp_path: Path) -> None:
    cfg = _make_test_config(tmp_path)
    real = Config(**{f.name: getattr(cfg, f.name) for f in attr.fields(Config)})
    targets = diagnostics._reachability_targets(real)
    labels = {label for label, _ in targets}
    assert "github" in labels
    # cert_api_base_url has a default, so cert_api should be present.
    assert "cert_api" in labels
    # No duplicate URLs.
    urls = [url for _, url in targets]
    assert len(urls) == len(set(urls))


@pytest.mark.real_collectors
def test_collect_reachability_mixed_results(tmp_path: Path) -> None:
    real = Config(**{f.name: getattr(_make_test_config(tmp_path), f.name) for f in attr.fields(Config)})

    class _RClient:
        def __init__(self, **_: Any) -> None:
            pass

        async def __aenter__(self) -> _RClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, url: str) -> Any:
            if "github" in url:
                return _FakeResp(200)
            raise httpx.ConnectError("boom")

    with patch("compute_space.core.diagnostics.httpx.AsyncClient", _RClient):
        results = asyncio.run(diagnostics._collect_reachability(real))
    by_label = {r.label: r for r in results}
    assert by_label["github"].reachable is True
    assert by_label["github"].status_code == 200
    assert by_label["github"].latency_ms is not None
    # A non-github target should be marked unreachable with an error.
    unreachable = [r for r in results if not r.reachable]
    assert unreachable
    assert unreachable[0].error


# ─── new fields present in bundles ────────────────────────────────────────────


def test_platform_bundle_has_new_fields(
    cfg: Any, system_client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    _seed_app(cfg.db_path, "myapp", manifest_raw=_MINIMAL_MANIFEST)
    body = system_client.get("/api/diagnostics", cookies=cookies).json()
    assert body["schema_version"] == 2
    assert "resource_pressure" in body
    assert "reachability" in body
    # Per-app entries carry health + resources.
    app = body["apps"][0]
    assert "health" in app
    assert "resources" in app


def test_app_bundle_has_new_fields(cfg: Any, apps_client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", manifest_raw=_MINIMAL_MANIFEST)
    body = apps_client.get(f"/api/app_diagnostics/{app_id}", cookies=cookies).json()
    assert body["schema_version"] == 2
    assert "health" in body
    assert "resources" in body
    assert "resource_pressure" in body
