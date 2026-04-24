"""Unit tests for V2 services: version resolution, permissions, URL parsing, access rules,
and OAuth app-scoped permission grant flow."""

import asyncio
import hashlib
import json
import sqlite3
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import attr
import pytest
from quart import Quart
from quart import Response

from compute_space.core.permissions_v2 import get_all_permissions_v2
from compute_space.core.permissions_v2 import get_granted_permissions_v2
from compute_space.core.permissions_v2 import grant_permission_v2
from compute_space.core.permissions_v2 import revoke_permission_v2
from compute_space.core.service_access_rules import ServiceAccessDenied
from compute_space.core.service_access_rules import check_service_access_rules
from compute_space.core.services import ServiceNotAvailable
from compute_space.core.services_v2 import resolve_provider
from compute_space.web.routes.api.permissions_v2 import api_permissions_v2_bp
from compute_space.web.routes.pages.permissions_v2 import pages_permissions_v2_bp
from compute_space.web.routes.services_v2 import _add_grant_url_if_global_grant_needed

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
            PRIMARY KEY (service_url, app_name, version)
        );
        CREATE TABLE service_defaults (
            service_url TEXT PRIMARY KEY,
            app_name TEXT NOT NULL
        );
        CREATE TABLE permissions_v2 (
            consumer_app TEXT NOT NULL,
            service_url TEXT NOT NULL,
            grant_payload TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'global',
            provider_app TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (consumer_app, service_url, grant_payload, scope, provider_app)
        );
        CREATE TABLE app_tokens (
            app_name TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE
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
        assert revoke_permission_v2("test-app", SVC_SECRETS, {"key": "X"}) is True

        grants = get_granted_permissions_v2("test-app", SVC_SECRETS)
        assert len(grants) == 0

    def test_revoke_nonexistent_returns_false(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        assert revoke_permission_v2("test-app", SVC_SECRETS, {"key": "NOPE"}) is False

    def test_revoke_requires_matching_scope_and_provider(self, db, monkeypatch):
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2(
            "test-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email"},
            scope="app",
            provider_app="secrets",
        )
        # Wrong scope
        assert (
            revoke_permission_v2(
                "test-app",
                SVC_OAUTH,
                {"provider": "google", "scope": "email"},
            )
            is False
        )
        # Wrong provider_app
        assert (
            revoke_permission_v2(
                "test-app",
                SVC_OAUTH,
                {"provider": "google", "scope": "email"},
                scope="app",
                provider_app="other",
            )
            is False
        )
        # Correct full key
        assert (
            revoke_permission_v2(
                "test-app",
                SVC_OAUTH,
                {"provider": "google", "scope": "email"},
                scope="app",
                provider_app="secrets",
            )
            is True
        )
        assert len(get_granted_permissions_v2("test-app", SVC_OAUTH)) == 0

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_app_token(db, app_name: str, token: str) -> None:
    """Register an app token in the test DB."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    db.execute(
        "INSERT OR REPLACE INTO app_tokens (app_name, token_hash) VALUES (?, ?)",
        (app_name, token_hash),
    )
    db.commit()


# ---------------------------------------------------------------------------
# OAuth app-scoped permission grant flow
# ---------------------------------------------------------------------------


class TestOAuthPermissionFlow:
    """Tests for the full OAuth app-scoped permission grant flow:

    1. Consumer requests OAuth token → no permission → 403 with grant_url
    2. User visits grant_url on provider, completes OAuth consent
    3. Provider grants app-scoped permission via router API (authed with app token)
    4. Consumer retries → permission included → provider serves token
    """

    def test_app_scoped_grant_stores_provider_app(self, db, monkeypatch):
        """App-scoped grants record which provider app created them."""
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2(
            "my-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email", "account": "alice@gmail.com"},
            scope="app",
            provider_app="secrets",
        )
        grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
        assert len(grants) == 1
        assert grants[0].scope == "app"
        assert grants[0].provider_app == "secrets"
        assert grants[0].grant["account"] == "alice@gmail.com"

    def test_app_scoped_grants_from_different_providers(self, db, monkeypatch):
        """App-scoped grants from different provider apps coexist independently."""
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2(
            "my-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email", "account": "alice@gmail.com"},
            scope="app",
            provider_app="secrets-a",
        )
        grant_permission_v2(
            "my-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email", "account": "bob@gmail.com"},
            scope="app",
            provider_app="secrets-b",
        )
        grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
        assert len(grants) == 2
        providers = {g.provider_app for g in grants}
        assert providers == {"secrets-a", "secrets-b"}

    def test_app_scoped_grant_serializes_for_permissions_header(self, db, monkeypatch):
        """App-scoped grants serialize correctly for the X-OpenHost-Permissions header."""
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)
        grant_permission_v2(
            "my-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email", "account": "alice@gmail.com"},
            scope="app",
            provider_app="secrets",
        )
        grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
        serialized = json.loads(json.dumps([attr.asdict(g) for g in grants]))
        assert serialized == [
            {
                "grant": {"provider": "google", "scope": "email", "account": "alice@gmail.com"},
                "scope": "app",
                "provider_app": "secrets",
            }
        ]

    def test_reform_403_app_scoped_passes_through_unchanged(self, monkeypatch):
        """App-scoped 403s are passed through unchanged — the provider owns the grant_url."""
        expected_url = "https://secrets.example.com/oauth/connect?provider=google&consumer=my-app"
        original_body = {
            "error": "permission_required",
            "required_grant": {
                "grant_payload": {"provider": "google", "scope": "email"},
                "scope": "app",
                "grant_url": expected_url,
            },
        }

        async def _run():
            app = Quart(__name__)
            async with app.app_context():
                provider_response = Response(
                    json.dumps(original_body),
                    status=403,
                    content_type="application/json",
                )
                result = await _add_grant_url_if_global_grant_needed(provider_response, SVC_OAUTH, "my-app")
                body = json.loads(await result.get_data())

            assert result.status_code == 403
            assert body == original_body

        asyncio.run(_run())

    def test_reform_403_global_scoped_generates_grant_url(self, monkeypatch):
        """Global-scoped grants get a router-generated grant_url."""

        async def _run():
            app = Quart(__name__)
            app.config["SERVER_NAME"] = "test.example.com"
            app.register_blueprint(pages_permissions_v2_bp)

            mock_config = MagicMock()
            mock_config.zone_domain = "test.example.com"
            monkeypatch.setattr("compute_space.web.routes.services_v2.get_config", lambda: mock_config)
            async with app.app_context():
                provider_response = Response(
                    json.dumps(
                        {
                            "error": "permission_required",
                            "required_grant": {
                                "grant_payload": {"key": "DATABASE_URL"},
                            },
                        }
                    ),
                    status=403,
                    content_type="application/json",
                )
                result = await _add_grant_url_if_global_grant_needed(provider_response, SVC_SECRETS, "my-app")
                body = json.loads(await result.get_data())

            assert result.status_code == 403
            assert "grant_url" in body["required_grant"]
            assert "test.example.com" in body["required_grant"]["grant_url"]

        asyncio.run(_run())

    def test_provider_can_grant_via_app_token(self, db, monkeypatch):
        """Provider app can grant an app-scoped permission via the app-grant endpoint."""
        _add_provider(db, SVC_OAUTH, "secrets", "0.1.0", "/_oauth_v2/")
        _register_app_token(db, "secrets", "secrets-token-123")

        monkeypatch.setattr("compute_space.core.auth.get_db", lambda: db)
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)

        async def _run():
            app = Quart(__name__)
            app.register_blueprint(api_permissions_v2_bp)
            client = app.test_client()

            resp = await client.post(
                "/api/permissions_v2/grant-app-scoped",
                json={
                    "consumer_app": "my-app",
                    "service_url": SVC_OAUTH,
                    "grant": {
                        "provider": "google",
                        "scope": "email",
                        "account": "alice@gmail.com",
                    },
                },
                headers={"Authorization": "Bearer secrets-token-123"},
            )
            assert resp.status_code == 200
            body = await resp.get_json()
            assert body["ok"] is True

            grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
            assert len(grants) == 1
            assert grants[0].scope == "app"
            assert grants[0].provider_app == "secrets"
            assert grants[0].grant["account"] == "alice@gmail.com"

        asyncio.run(_run())

    def test_app_grant_scopes_to_calling_app(self, db, monkeypatch):
        """The grant is scoped to the calling app regardless of which app it is."""
        db.execute(
            "INSERT INTO apps (name, local_port, status) VALUES (?, ?, ?)",
            ("other-app", 9999, "running"),
        )
        _register_app_token(db, "other-app", "other-token-456")
        db.commit()

        monkeypatch.setattr("compute_space.core.auth.get_db", lambda: db)
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)

        async def _run():
            app = Quart(__name__)
            app.register_blueprint(api_permissions_v2_bp)
            client = app.test_client()

            resp = await client.post(
                "/api/permissions_v2/grant-app-scoped",
                json={
                    "consumer_app": "my-app",
                    "service_url": SVC_OAUTH,
                    "grant": {"provider": "google", "scope": "email"},
                },
                headers={"Authorization": "Bearer other-token-456"},
            )
            assert resp.status_code == 200

            grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
            assert len(grants) == 1
            assert grants[0].provider_app == "other-app"

        asyncio.run(_run())

    def test_app_grant_rejects_invalid_token(self, db, monkeypatch):
        """Requests with invalid app tokens are rejected."""
        monkeypatch.setattr("compute_space.core.auth.get_db", lambda: db)

        async def _run():
            app = Quart(__name__)
            app.register_blueprint(api_permissions_v2_bp)
            client = app.test_client()

            resp = await client.post(
                "/api/permissions_v2/grant-app-scoped",
                json={
                    "consumer_app": "my-app",
                    "service_url": SVC_OAUTH,
                    "grant": {"provider": "google", "scope": "email"},
                },
                headers={"Authorization": "Bearer bad-token"},
            )
            assert resp.status_code == 401

        asyncio.run(_run())

    def test_full_oauth_permission_flow(self, db, monkeypatch):
        """End-to-end: no permission → 403 with grant_url → provider grants → retry sees permission."""
        monkeypatch.setattr("compute_space.core.permissions_v2.get_db", lambda: db)

        # Step 1: Consumer has no permissions for the OAuth service
        grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
        assert len(grants) == 0

        # Step 2: Provider would return 403 with required_grant including grant_url.
        # (The provider determines what permissions are needed and provides a URL
        # to walk the user through authorization.)
        # The consumer sees the grant_url and redirects the user there.

        # Step 3: After the user completes OAuth consent, the provider knows the
        # account identity (e.g., alice@gmail.com) and grants the permission.
        grant_permission_v2(
            "my-app",
            SVC_OAUTH,
            {"provider": "google", "scope": "email", "account": "alice@gmail.com"},
            scope="app",
            provider_app="secrets",
        )

        # Step 4: Consumer retries — the permission is now available
        grants = get_granted_permissions_v2("my-app", SVC_OAUTH)
        assert len(grants) == 1

        # Step 5: Router serializes grants for the X-OpenHost-Permissions header
        header_value = json.dumps([attr.asdict(g) for g in grants])
        parsed = json.loads(header_value)
        assert parsed[0]["grant"]["account"] == "alice@gmail.com"
        assert parsed[0]["scope"] == "app"
        assert parsed[0]["provider_app"] == "secrets"

        # Step 6: Provider reads the header, verifies the grant covers the request,
        # and serves the token.
        granted_pairs = set()
        for g in parsed:
            payload = g["grant"]
            if "provider" in payload and "scope" in payload:
                granted_pairs.add((payload["provider"], payload["scope"]))
        assert ("google", "email") in granted_pairs

    def test_return_to_url_flow(self, db, monkeypatch):
        """The grant_url includes the consumer's return_to so the user is redirected back."""
        mock_config = MagicMock()
        mock_config.zone_domain = "test.example.com"
        monkeypatch.setattr("compute_space.web.routes.services_v2.get_config", lambda: mock_config)

        return_to = "//my-app.test.example.com/dashboard"
        grant_url = (
            f"https://secrets.test.example.com/oauth/connect?provider=google&consumer=my-app&return_to={return_to}"
        )

        async def _run():
            app = Quart(__name__)
            async with app.app_context():
                provider_response = Response(
                    json.dumps(
                        {
                            "error": "permission_required",
                            "required_grant": {
                                "grant_payload": {"provider": "google", "scope": "email"},
                                "scope": "app",
                                "grant_url": grant_url,
                            },
                        }
                    ),
                    status=403,
                    content_type="application/json",
                )
                result = await _add_grant_url_if_global_grant_needed(provider_response, SVC_OAUTH, "my-app")
                body = json.loads(await result.get_data())

            assert "return_to" in body["required_grant"]["grant_url"]
            assert "my-app.test.example.com/dashboard" in body["required_grant"]["grant_url"]

        asyncio.run(_run())
