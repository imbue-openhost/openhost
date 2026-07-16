"""Tests that an app update requiring NEW service permissions is gated on
explicit owner approval, mirroring the approval required at install time.

Before this, ``reload_app_background`` re-synced manifest-derived columns but
silently left newly declared permissions ungranted — the update proceeded and
the new permissions only showed up passively on the app detail page. Now
``_reload_app_impl`` refuses the reload (via ``_gate_new_permissions``) until
the owner approves, and the app keeps running its current version meanwhile.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.core.apps import manifest_ungranted_permissions_v2
from compute_space.core.auth.permissions_v2 import PermissionRecord
from compute_space.core.auth.permissions_v2 import get_all_permissions_v2
from compute_space.core.auth.permissions_v2 import grant_permission_v2
from compute_space.core.manifest import parse_manifest
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.web.routes.api.apps import _gate_new_permissions
from compute_space.web.routes.api.apps import api_apps_routes

from .conftest import _make_test_config

_CONSUMES = """\
[app]
name = "perm-app"
version = "1.0.0"

[runtime.container]
image = "Dockerfile"
port = 5000

{consumes}
"""


def _manifest_with_consumes(repo: Path, consumes: str) -> Any:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "openhost.toml").write_text(_CONSUMES.format(consumes=consumes))
    return parse_manifest(str(repo))


def _consume_block(service: str, shortname: str, grants: str) -> str:
    return (
        "[[services.v2.consumes]]\n"
        f'service = "{service}"\n'
        f'shortname = "{shortname}"\n'
        'version = ">=0.1.0"\n'
        f"grants = [{grants}]\n"
    )


# ─── the shared diff helper ───────────────────────────────────────────────────


def test_ungranted_empty_when_no_consumes(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(tmp_path / "m", "")
    assert manifest_ungranted_permissions_v2(manifest, []) == []


def test_ungranted_lists_declared_when_nothing_granted(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(
        tmp_path / "m", _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )
    ungranted = manifest_ungranted_permissions_v2(manifest, [])
    assert len(ungranted) == 1
    assert ungranted[0].service_url == "github.com/x/secrets"
    assert ungranted[0].grant == {"key": "API_KEY"}


def test_ungranted_excludes_already_granted(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(
        tmp_path / "m", _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/secrets",
            grant={"key": "API_KEY"},
            scope="global",
            provider_app_id=None,
        )
    ]
    assert manifest_ungranted_permissions_v2(manifest, granted) == []


def test_ungranted_grant_match_is_key_order_insensitive(tmp_path: Path) -> None:
    """A stored grant and a declared grant compare equal regardless of dict key
    order (both normalize via json.dumps(sort_keys=True))."""
    manifest = _manifest_with_consumes(
        tmp_path / "m",
        _consume_block("github.com/x/svc", "svc", '{ a = "1", b = "2" }'),
    )
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/svc",
            # Same content, different insertion order.
            grant={"b": "2", "a": "1"},
            scope="global",
            provider_app_id=None,
        )
    ]
    assert manifest_ungranted_permissions_v2(manifest, granted) == []


def test_ungranted_dedups_repeated_declarations(tmp_path: Path) -> None:
    manifest = _manifest_with_consumes(
        tmp_path / "m",
        _consume_block("github.com/x/svc", "svc", '{ key = "K" }, { key = "K" }'),
    )
    ungranted = manifest_ungranted_permissions_v2(manifest, [])
    assert len(ungranted) == 1


def test_ungranted_reports_only_the_new_grant(tmp_path: Path) -> None:
    """When one of two declared grants is already held, only the other is new."""
    manifest = _manifest_with_consumes(
        tmp_path / "m",
        _consume_block("github.com/x/svc", "svc", '{ key = "OLD" }, { key = "NEW" }'),
    )
    granted = [
        PermissionRecord(
            consumer_app_id="a1",
            service_url="github.com/x/svc",
            grant={"key": "OLD"},
            scope="global",
            provider_app_id=None,
        )
    ]
    ungranted = manifest_ungranted_permissions_v2(manifest, granted)
    assert len(ungranted) == 1
    assert ungranted[0].grant == {"key": "NEW"}


# ─── the reload gate ──────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path_factory: pytest.TempPathFactory) -> Any:
    c = _make_test_config(tmp_path_factory.mktemp("perm-gate"), port=20900)
    init_db(c.db_path)
    return c


def _seed_perm_app(cfg: Any, repo_path: str) -> str:
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, 'perm-app', '1.0', ?, 20901, 'running')",
            (app_id, repo_path),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def test_gate_returns_none_when_no_new_permissions(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, "")  # no consumes
    app_id = _seed_perm_app(cfg, str(repo))

    assert _gate_new_permissions(app_id, str(repo), approve_new_permissions=False) is None


def test_gate_refuses_new_permission_without_approval(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    app_id = _seed_perm_app(cfg, str(repo))

    result = _gate_new_permissions(app_id, str(repo), approve_new_permissions=False)
    assert result is not None
    assert result.ok is False
    assert len(result.permissions_required) == 1
    assert result.permissions_required[0]["service_url"] == "github.com/x/secrets"
    assert result.permissions_required[0]["shortname"] == "secrets"
    # Nothing was granted (the gate must not persist anything when refusing).
    assert get_all_permissions_v2(consumer_app_id=app_id) == []


def test_gate_grants_and_proceeds_when_approved(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    app_id = _seed_perm_app(cfg, str(repo))

    result = _gate_new_permissions(app_id, str(repo), approve_new_permissions=True)
    assert result is None
    granted = get_all_permissions_v2(consumer_app_id=app_id)
    assert len(granted) == 1
    assert granted[0].service_url == "github.com/x/secrets"
    assert granted[0].grant == {"key": "API_KEY"}


def test_gate_ignores_already_granted_permission(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _manifest_with_consumes(repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }'))
    app_id = _seed_perm_app(cfg, str(repo))
    grant_permission_v2(consumer_app_id=app_id, service_url="github.com/x/secrets", grant_payload={"key": "API_KEY"})

    # Already held -> not "new" -> gate passes without prompting.
    assert _gate_new_permissions(app_id, str(repo), approve_new_permissions=False) is None


def test_gate_returns_none_for_unparseable_manifest(cfg: Any, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "openhost.toml").write_text("not = valid [ toml")
    app_id = _seed_perm_app(cfg, str(repo))

    # A broken manifest isn't the gate's problem; the reload path surfaces it.
    assert _gate_new_permissions(app_id, str(repo), approve_new_permissions=False) is None


# ─── the /reload_app route end-to-end ─────────────────────────────────────────


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_git_app_with_consumes(cfg: Any, repo: Path, consumes: str) -> str:
    """Seed a running app whose on-disk repo is a git checkout declaring
    ``consumes`` permissions. A plain (non-update) reload reads this manifest."""
    _manifest_with_consumes(repo, consumes)
    (repo / ".git").mkdir()  # marks it a git checkout so reload uses it in place
    return _seed_perm_app(cfg, str(repo))


def test_reload_route_refuses_when_new_permission_unapproved(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(
        cfg, repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )

    with (
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        resp = client.post(f"/reload_app/{app_id}", json={"update": False}, cookies=cookies)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["permissions_required"][0]["service_url"] == "github.com/x/secrets"
    # The running app was NOT touched and no reload thread was spawned.
    stop.assert_not_called()
    thread.assert_not_called()
    assert get_all_permissions_v2(consumer_app_id=app_id) == []
    # App row stays 'running' (not flipped to 'building').
    db = sqlite3.connect(cfg.db_path)
    try:
        status = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()[0]
    finally:
        db.close()
    assert status == "running"


def test_reload_route_grants_and_reloads_when_approved(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(
        cfg, repo, _consume_block("github.com/x/secrets", "secrets", '{ key = "API_KEY" }')
    )

    with (
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        resp = client.post(
            f"/reload_app/{app_id}",
            json={"update": False, "approve_new_permissions": True},
            cookies=cookies,
        )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # The permission was granted and the reload proceeded.
    granted = get_all_permissions_v2(consumer_app_id=app_id)
    assert len(granted) == 1
    assert granted[0].service_url == "github.com/x/secrets"
    stop.assert_called_once()
    thread.assert_called_once()


def test_reload_route_proceeds_when_no_new_permissions(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    app_id = _seed_git_app_with_consumes(cfg, repo, "")  # no consumes

    with (
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
        patch("compute_space.web.routes.api.apps.Thread") as thread,
    ):
        resp = client.post(f"/reload_app/{app_id}", json={"update": False}, cookies=cookies)

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    stop.assert_called_once()
    thread.assert_called_once()
