"""Unit tests for V2 services: version resolution, permissions, URL parsing, access rules."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from compute_space.core.permissions_v2 import get_all_permissions_v2
from compute_space.core.permissions_v2 import get_granted_permissions_v2
from compute_space.core.permissions_v2 import grant_permission_v2
from compute_space.core.permissions_v2 import revoke_permission_v2
from compute_space.core.service_access_rules import ServiceAccessDenied
from compute_space.core.service_access_rules import check_service_access_rules
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services_v2 import resolve_provider
from compute_space.web.routes.services_v2 import _parse_service_url_and_endpoint

SVC_SECRETS = "github.com/org/repo/services/secrets"
SVC_OAUTH = "github.com/org/repo/services/oauth"


@pytest.fixture
def db():
    """In-memory SQLite database with the v2 schema tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE apps (
            name TEXT PRIMARY KEY,
            local_port INTEGER,
            status TEXT
        );
        CREATE TABLE service_providers_v2 (
            service_url TEXT NOT NULL,
            app_name TEXT NOT NULL,
            version TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            PRIMARY KEY (service_url, app_name)
        );
        CREATE TABLE service_defaults (
            service_url TEXT PRIMARY KEY,
            app_name TEXT NOT NULL
        );
        CREATE TABLE permissions_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumer_app TEXT NOT NULL,
            service_url TEXT NOT NULL,
            grant_payload TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            provider_app TEXT,
            expires_at TEXT,
            UNIQUE(consumer_app, service_url, grant_payload, scope)
        );
    """)
    return conn


def _add_provider(db, service_url, app_name, version, endpoint, port=9000, status="running", default=True):
    db.execute(
        "INSERT OR REPLACE INTO apps (name, local_port, status) VALUES (?, ?, ?)",
        (app_name, port, status),
    )
    db.execute(
        "INSERT INTO service_providers_v2 (service_url, app_name, version, endpoint) VALUES (?, ?, ?, ?)",
        (service_url, app_name, version, endpoint),
    )
    if default:
        db.execute(
            "INSERT OR REPLACE INTO service_defaults (service_url, app_name) VALUES (?, ?)",
            (service_url, app_name),
        )
    db.commit()


# ---------------------------------------------------------------------------
# Version resolution
# ---------------------------------------------------------------------------


class TestVersionResolution:
    def test_compatible_version_resolves(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.2.0", "/_svc/")
        app, port, ver, ep = resolve_provider(SVC_SECRETS, ">=0.1.0", db)
        assert app == "secrets"
        assert ver == "0.2.0"
        assert ep == "/_svc/"

    def test_exact_version(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "1.0.0", "/_svc/")
        app, _, ver, _ = resolve_provider(SVC_SECRETS, "==1.0.0", db)
        assert ver == "1.0.0"

    def test_no_provider_raises(self, db):
        with pytest.raises(ServiceNotAvailable, match="No provider"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db)

    def test_version_mismatch_raises(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(ServiceNotAvailable, match="does not match"):
            resolve_provider(SVC_SECRETS, ">=99.0.0", db)

    def test_not_running_raises(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/", status="stopped")
        with pytest.raises(ServiceNotAvailable, match="not running"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db)

    def test_explicit_provider_app(self, db):
        _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001)
        _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)

        app, _, ver, ep = resolve_provider(SVC_SECRETS, ">=0.1.0", db, provider_app="secrets-b")
        assert app == "secrets-b"
        assert ep == "/_b/"

    def test_explicit_provider_app_not_found(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(ServiceNotAvailable, match="not found"):
            resolve_provider(SVC_SECRETS, ">=0.1.0", db, provider_app="nonexistent")

    def test_explicit_provider_app_version_mismatch(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(ServiceNotAvailable, match="does not match"):
            resolve_provider(SVC_SECRETS, ">=99.0.0", db, provider_app="secrets")

    def test_uses_default_provider(self, db):
        _add_provider(db, SVC_SECRETS, "secrets-a", "0.1.0", "/_a/", port=9001)
        _add_provider(db, SVC_SECRETS, "secrets-b", "0.2.0", "/_b/", port=9002, default=False)

        app, _, _, _ = resolve_provider(SVC_SECRETS, ">=0.1.0", db)
        assert app == "secrets-a"


# ---------------------------------------------------------------------------
# Permissions V2
# ---------------------------------------------------------------------------


class TestPermissionsV2:
    def test_grant_and_query(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "DB_URL"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 1
        assert grants[0].grant == {"key": "DB_URL"}

    def test_grant_is_idempotent(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "X"})
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "X"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 1

    def test_revoke(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "X"})
        revoke_permission_v2("test-app", SVC_SECRETS, {"key": "X"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 0

    def test_permissions_scoped_per_service(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "DB_URL"})
        grant_permission_v2("test-app", SVC_OAUTH, {"provider": "google", "scope": "email"})

        secrets_grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        oauth_grants = get_granted_permissions_v2("test-app", SVC_OAUTH)
        assert len(secrets_grants) == 1
        assert secrets_grants[0].grant == {"key": "DB_URL"}
        assert len(oauth_grants) == 1
        assert oauth_grants[0].grant == {"provider": "google", "scope": "email"}

    def test_get_all_permissions(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2("app-a", SVC_SECRETS, {"key": "X"})
        grant_permission_v2("app-a", SVC_OAUTH, {"provider": "google", "scope": "email"})
        grant_permission_v2("app-b", SVC_SECRETS, {"key": "Y"})

        all_perms = get_all_permissions_v2()
        assert len(all_perms) == 3

        app_a_perms = get_all_permissions_v2(consumer_app="app-a")
        assert len(app_a_perms) == 2
        assert {p.service_url for p in app_a_perms} == {SVC_SECRETS, SVC_OAUTH}

    def test_multiple_grants_same_service(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "SECRET_A"})
        grant_permission_v2("test-app", SVC_SECRETS, {"key": "SECRET_B"})

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 2
        granted_keys = {g.grant["key"] for g in grants}
        assert granted_keys == {"SECRET_A", "SECRET_B"}

        revoke_permission_v2("test-app", SVC_SECRETS, {"key": "SECRET_A"})
        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 1
        assert grants[0].grant["key"] == "SECRET_B"


# ---------------------------------------------------------------------------
# V2 proxy URL parsing
# ---------------------------------------------------------------------------


class TestServiceUrlParsing:
    def test_basic_url_and_endpoint(self):
        raw = b"/_services_v2/github.com%2Forg%2Frepo%2Fservices%2Fsecrets/get"
        svc, ep = _parse_service_url_and_endpoint(raw)
        assert svc == "github.com/org/repo/services/secrets"
        assert ep == "get"

    def test_nested_endpoint(self):
        raw = b"/_services_v2/github.com%2Forg%2Frepo%2Fservices%2Foauth/oauth/token"
        svc, ep = _parse_service_url_and_endpoint(raw)
        assert svc == "github.com/org/repo/services/oauth"
        assert ep == "oauth/token"

    def test_no_endpoint(self):
        raw = b"/_services_v2/github.com%2Forg%2Frepo%2Fservices%2Fsecrets"
        svc, ep = _parse_service_url_and_endpoint(raw)
        assert svc == "github.com/org/repo/services/secrets"
        assert ep == ""

    def test_query_string_stripped(self):
        raw = b"/_services_v2/github.com%2Forg%2Frepo%2Fservices%2Fsecrets/get?version=>=0.1.0"
        svc, ep = _parse_service_url_and_endpoint(raw)
        assert svc == "github.com/org/repo/services/secrets"
        assert ep == "get"


# ---------------------------------------------------------------------------
# Version resolution — edge cases
# ---------------------------------------------------------------------------


class TestVersionResolutionEdgeCases:
    def test_invalid_specifier_raises(self, db):
        _add_provider(db, SVC_SECRETS, "secrets", "0.1.0", "/_svc/")
        with pytest.raises(ServiceNotAvailable, match="Invalid version specifier"):
            resolve_provider(SVC_SECRETS, "not_a_version!!", db)


# ---------------------------------------------------------------------------
# Service access rules
# ---------------------------------------------------------------------------


def _mock_request(method: str, body: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.method = method
    if body is not None:
        req.get_json = AsyncMock(return_value=body)
    else:
        req.get_json = AsyncMock(side_effect=Exception("no body"))
    return req


class TestSecretsAccessRules:
    def test_oauth_token_produces_scoped_permissions(self):
        req = _mock_request(
            "POST",
            {
                "provider": "google",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.readonly",
                    "https://www.googleapis.com/auth/calendar",
                ],
            },
        )
        perms = asyncio.run(check_service_access_rules("secrets", "oauth/token", req))
        assert perms == [
            "secrets/oauth:google:https://www.googleapis.com/auth/gmail.readonly",
            "secrets/oauth:google:https://www.googleapis.com/auth/calendar",
        ]

    def test_get_endpoint_produces_key_permissions(self):
        req = _mock_request("POST", {"keys": ["DB_URL", "API_KEY"]})
        perms = asyncio.run(check_service_access_rules("secrets", "get", req))
        assert perms == ["secrets/key:DB_URL", "secrets/key:API_KEY"]

    def test_unknown_endpoint_denied(self):
        req = _mock_request("POST", {})
        with pytest.raises(ServiceAccessDenied, match="not available"):
            asyncio.run(check_service_access_rules("secrets", "delete_all", req))

    def test_unknown_service_denied(self):
        req = _mock_request("GET")
        with pytest.raises(ServiceAccessDenied, match="No access rules"):
            asyncio.run(check_service_access_rules("unknown_svc", "anything", req))
