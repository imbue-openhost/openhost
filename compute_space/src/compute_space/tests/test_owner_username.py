"""Tests for the owner-username plumbing.

Covers three load-bearing pieces of the OPENHOST_OWNER_USERNAME
feature, in this order:

  1. ``auth.validate_owner_username`` — input rules.
  2. ``auth.read_owner_username`` / ``auth.update_owner_username``
     — round-tripping through the owner row.  Pre-setup zones
     (no owner row) must return None, never raise; the env-var
     plumbing keys on None to mean "skip the variable".
  3. ``provision_data`` — env var stamped iff ``owner_username``
     is non-empty.
  4. Route + middleware + proxy layers — wired together end to
     end against a real (file-backed) SQLite DB.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import bcrypt
import jwt as pyjwt
import pytest
from quart import Quart
from quart import g

import compute_space.web.routes.api.settings as settings_mod
from compute_space.config import DefaultConfig
from compute_space.core.auth.auth import DEFAULT_OWNER_USERNAME
from compute_space.core.auth.auth import read_owner_username
from compute_space.core.auth.auth import update_owner_username
from compute_space.core.auth.auth import validate_api_token
from compute_space.core.auth.auth import validate_owner_username
from compute_space.core.auth.keys import load_keys
from compute_space.core.auth.tokens import create_access_token
from compute_space.core.data import provision_data
from compute_space.core.manifest import AppManifest
from compute_space.db.connection import close_db
from compute_space.db.schema import schema_path
from compute_space.web.auth.cookies import COOKIE_ACCESS
from compute_space.web.auth.cookies import COOKIE_REFRESH
from compute_space.web.auth.middleware import _try_refresh
from compute_space.web.routes.pages.login import auth_bp
from compute_space.web.routes.pages.setup import setup_bp
from compute_space.web.routes.proxy import _identity_headers

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _db_file_conn(path: str) -> Iterator[sqlite3.Connection]:
    """Open a sqlite3 connection to ``path`` and guarantee it's closed."""
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()


def _init_schema(path: str) -> None:
    """Initialise ``path`` with the production schema."""
    with _db_file_conn(path) as conn, open(schema_path()) as f:
        conn.executescript(f.read())
        conn.commit()


def _seed_owner(conn_or_path: sqlite3.Connection | str, username: str = "owner") -> None:
    """Insert an owner row with the given username.

    Accepts either an open sqlite3 connection (the ``db`` fixture's
    in-memory DB) or a path to a file-backed DB.  In both cases the
    inserted ``password_hash`` is a dummy value — only auth/login
    tests care about hashing.
    """
    pw_hash = "$2b$12$dummyhashfortestonly"
    if isinstance(conn_or_path, sqlite3.Connection):
        conn_or_path.execute(
            "INSERT INTO owner (id, username, password_hash) VALUES (1, ?, ?)",
            (username, pw_hash),
        )
        conn_or_path.commit()
        return
    with _db_file_conn(conn_or_path) as conn:
        conn.execute(
            "INSERT INTO owner (id, username, password_hash) VALUES (1, ?, ?)",
            (username, pw_hash),
        )
        conn.commit()


def _read_owner_username_direct(db_path: str) -> str | None:
    """Test-side read that bypasses the Quart connection cache."""
    with _db_file_conn(db_path) as conn:
        row = conn.execute("SELECT username FROM owner WHERE id = 1").fetchone()
        return None if row is None else row[0]


def _make_test_cfg(root: Path, **overrides: Any) -> DefaultConfig:
    """Build a minimal test config rooted at ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    cfg = DefaultConfig(
        host="127.0.0.1",
        data_root_dir=str(root),
        zone_domain=overrides.pop("zone_domain", "testzone.local"),
        tls_enabled=False,
        start_caddy=False,
        **overrides,
    )
    cfg.make_all_dirs()
    return cfg


def _make_quart_app(
    db_path: str,
    *,
    cfg: DefaultConfig | None = None,
    register_auth_bp: bool = False,
) -> Quart:
    """Build a Quart app wired to a file-backed DB.

    Used by the route / middleware / proxy tests.  Adds a stub
    ``apps.dashboard`` endpoint when ``register_auth_bp=True`` so
    /setup's redirect can build a URL without importing the full
    apps blueprint (which would pull in podman / git plumbing).
    """
    app = Quart(
        __name__,
        template_folder=str(Path(__file__).resolve().parent.parent / "web" / "templates"),
    )
    app.config["DB_PATH"] = db_path
    app.teardown_appcontext(close_db)
    if cfg is not None:
        app.openhost_config = cfg  # type: ignore[attr-defined]
    if register_auth_bp:
        app.register_blueprint(auth_bp)
        app.register_blueprint(setup_bp)

        @app.route("/dashboard", endpoint="apps.dashboard")
        async def _dashboard_stub() -> str:
            return "ok"

    return app


def _set_cookie_from_response(resp: Any, name: str) -> str | None:
    """Pluck a Set-Cookie value out of a Quart test-client response."""
    for cookie in resp.headers.getlist("Set-Cookie"):
        if cookie.startswith(f"{name}="):
            return cookie.split("=", 1)[1].split(";", 1)[0]
    return None


def _bare_manifest() -> AppManifest:
    """Minimal manifest with app_data enabled so provision_data runs."""
    return AppManifest(  # type: ignore[call-arg]
        name="probe",
        version="1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=128,
        cpu_millicores=100,
        app_data=True,
    )


def _provision(tmp_path: Path, **kwargs: Any) -> dict[str, str]:
    """Call provision_data with realistic-but-irrelevant defaults.

    archive_dir is passed (and pre-created) because main added it
    to the signature while this PR was in review; the
    owner-username tests don't exercise the archive path, so we
    just need provision_data to run to completion.
    """
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(exist_ok=True)
    return provision_data(
        "probe",
        _bare_manifest(),
        str(tmp_path / "data"),
        str(tmp_path / "temp"),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
        **kwargs,
    )


async def _set_username_via_route(app: Quart, payload: Any) -> tuple[int, dict]:
    """POST ``payload`` to the set_owner_username route and unpack
    Quart's ``(body, status)`` tuple shape into ``(status, body)``.
    """
    async with app.test_request_context(
        "/api/settings/owner_username",
        method="POST",
        json=payload,
    ):
        result = await settings_mod.set_owner_username.__wrapped__()
    if isinstance(result, tuple):
        resp, status = result
    else:
        resp, status = result, result.status_code
    body = await resp.get_json()
    return status, body


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """A file-backed SQLite DB with the production schema."""
    path = str(tmp_path / "test.db")
    _init_schema(path)
    return path


@pytest.fixture
def settings_route_app(db_path: str) -> Quart:
    """Quart app wired for the GET/POST /api/settings/owner_username routes."""
    return _make_quart_app(db_path)


@pytest.fixture
def setup_route_app(tmp_path: Path, db_path: str) -> Iterator[tuple[Quart, str]]:
    """Quart app + auth blueprint + JWT keys, ready to drive /setup and /login.

    Patches ``get_config`` in both modules where it's used so
    ``create_access_token`` and the redirect machinery can see a
    real config.
    """
    cfg = _make_test_cfg(tmp_path / "data")
    load_keys(str(tmp_path / "keys"))
    with contextlib.suppress(FileNotFoundError):
        os.remove(cfg.claim_token_path)

    app = _make_quart_app(db_path, cfg=cfg, register_auth_bp=True)

    patches = [
        patch("compute_space.web.routes.pages.setup.get_config", return_value=cfg),
        patch("compute_space.core.auth.tokens.get_config", return_value=cfg),
        patch("compute_space.web.auth.cookies.get_config", return_value=cfg),
    ]
    for p in patches:
        p.start()
    try:
        yield app, db_path
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# validate_owner_username
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "alice",
        "Alice",  # mixed case is fine
        "alice42",
        "alice.bishop",
        "alice_bishop",
        "alice-bishop",
        "a",  # 1-char min
        "a" * 50,  # max length
        "x.y.z-1_2",  # all-allowed-punct mix
    ],
)
def test_validate_owner_username_accepts(value: str) -> None:
    assert validate_owner_username(value) is None, value


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        ".alice",  # leading punct breaks PeerTube
        "_alice",
        "-alice",
        "alice@example.com",  # email shape rejected (avoid SSO identifier collisions)
        "alice space",  # internal whitespace breaks JWT preferred_username
        "alice/bob",
        "a" * 51,  # over max
        "alice\nfoo",  # control char breaks HTTP headers
        "\u00fcnicode",  # non-ASCII
    ],
)
def test_validate_owner_username_rejects(value: str) -> None:
    assert validate_owner_username(value) is not None, value


# ---------------------------------------------------------------------------
# read / update owner username
# ---------------------------------------------------------------------------


def test_read_owner_username_returns_none_pre_setup(db) -> None:  # type: ignore[no-untyped-def]
    """Pre-setup, the owner table is empty; read must return None,
    not raise — provisioning keys on None to mean "skip the env var"."""
    assert read_owner_username(db) is None


def test_read_owner_username_returns_value_after_setup(db) -> None:  # type: ignore[no-untyped-def]
    _seed_owner(db, "alice")
    assert read_owner_username(db) == "alice"


def test_update_owner_username_persists(db) -> None:  # type: ignore[no-untyped-def]
    _seed_owner(db, "owner")
    update_owner_username(db, "zack")
    db.commit()
    assert read_owner_username(db) == "zack"


def test_update_owner_username_does_not_create_extra_row(db) -> None:  # type: ignore[no-untyped-def]
    """The owner table has CHECK(id = 1); update_* must mutate,
    not insert."""
    _seed_owner(db, "owner")
    update_owner_username(db, "zack")
    update_owner_username(db, "alice")
    db.commit()
    rows = db.execute("SELECT id, username FROM owner").fetchall()
    assert len(rows) == 1
    assert rows[0]["username"] == "alice"


# ---------------------------------------------------------------------------
# provision_data: OPENHOST_OWNER_USERNAME stamping
# ---------------------------------------------------------------------------


def test_provision_data_stamps_owner_username_when_provided(tmp_path: Path) -> None:
    env = _provision(tmp_path, owner_username="alice")
    assert env["OPENHOST_OWNER_USERNAME"] == "alice"


@pytest.mark.parametrize("falsy", [None, ""])
def test_provision_data_omits_owner_username_when_falsy(tmp_path: Path, falsy: Any) -> None:
    """None (pre-setup) and "" (defensive against buggy callers)
    both omit the env var entirely — apps distinguish "not set"
    from "set to empty"."""
    env = _provision(tmp_path, owner_username=falsy)
    assert "OPENHOST_OWNER_USERNAME" not in env


def test_provision_data_owner_username_default_is_none(tmp_path: Path) -> None:
    """Backward-compat: callers that don't pass ``owner_username``
    still get a working env dict, just without the new var."""
    env = _provision(tmp_path)
    assert "OPENHOST_OWNER_USERNAME" not in env
    assert env["OPENHOST_APP_NAME"] == "probe"
    assert env["OPENHOST_ZONE_DOMAIN"] == "example.com"


# ---------------------------------------------------------------------------
# /api/settings/owner_username route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_get_returns_null_pre_setup(settings_route_app: Quart) -> None:
    async with settings_route_app.test_request_context("/api/settings/owner_username", method="GET"):
        resp = await settings_mod.get_owner_username.__wrapped__()
    assert (await resp.get_json()) == {"ok": True, "username": None}


@pytest.mark.asyncio
async def test_route_get_returns_current_value(settings_route_app: Quart, db_path: str) -> None:
    _seed_owner(db_path, "zack")
    async with settings_route_app.test_request_context("/api/settings/owner_username", method="GET"):
        resp = await settings_mod.get_owner_username.__wrapped__()
    assert (await resp.get_json()) == {"ok": True, "username": "zack"}


@pytest.mark.asyncio
async def test_route_set_updates_owner_row(settings_route_app: Quart, db_path: str) -> None:
    _seed_owner(db_path, "owner")
    status, body = await _set_username_via_route(settings_route_app, {"username": "alice"})
    assert status == 200
    assert body == {"ok": True, "username": "alice"}
    # Pin persisted state, not just the response — protects against
    # a bug where the route returns the new value but forgets to
    # commit.
    assert _read_owner_username_direct(db_path) == "alice"


@pytest.mark.asyncio
async def test_route_set_400_when_no_owner_row(settings_route_app: Quart) -> None:
    """Setter against pre-setup DB must 400 (and mention /setup)
    rather than silently succeed."""
    status, body = await _set_username_via_route(settings_route_app, {"username": "alice"})
    assert status == 400
    assert body["ok"] is False
    assert "/setup" in body["error"]


@pytest.mark.parametrize(
    ("payload", "needle"),
    [
        ({"username": "alice space"}, "letters"),  # validator error verbatim
        ({"username": ""}, "Username"),  # empty username explicitly rejected
        ({"username": 42}, "string"),  # type-confusion guard
        (["alice"], "JSON object"),  # non-object body rejected
    ],
)
@pytest.mark.asyncio
async def test_route_set_rejects(settings_route_app: Quart, payload: Any, needle: str) -> None:
    status, body = await _set_username_via_route(settings_route_app, payload)
    assert status == 400
    assert body["ok"] is False
    assert needle in body["error"]


# ---------------------------------------------------------------------------
# /setup end-to-end (form-driven persistence + JWT signing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_persists_custom_username_and_signs_jwt_with_it(
    setup_route_app: tuple[Quart, str],
) -> None:
    """The /setup POST path must (a) persist the operator-supplied
    username, and (b) sign the freshly-issued JWT with the same
    username — otherwise apps consuming the access token immediately
    after setup see the literal 'owner' instead of the chosen name."""
    app, db_path = setup_route_app
    client = app.test_client()
    resp = await client.post(
        "/setup",
        form={"username": "zack", "password": "secretpass1", "confirm_password": "secretpass1"},
    )
    assert resp.status_code in (302, 303), await resp.get_data(as_text=True)
    assert _read_owner_username_direct(db_path) == "zack"

    cookie = _set_cookie_from_response(resp, COOKIE_ACCESS)
    assert cookie, "expected zone_auth cookie on setup response"
    claims = pyjwt.decode(cookie, options={"verify_signature": False})
    assert claims["sub"] == "zack"
    assert claims["username"] == "zack"


@pytest.mark.asyncio
async def test_setup_blank_username_falls_back_to_default(
    setup_route_app: tuple[Quart, str],
) -> None:
    app, db_path = setup_route_app
    client = app.test_client()
    resp = await client.post(
        "/setup",
        form={"username": "", "password": "secretpass1", "confirm_password": "secretpass1"},
    )
    assert resp.status_code in (302, 303), await resp.get_data(as_text=True)
    assert _read_owner_username_direct(db_path) == DEFAULT_OWNER_USERNAME == "owner"


@pytest.mark.asyncio
async def test_setup_invalid_username_re_renders_form(
    setup_route_app: tuple[Quart, str],
) -> None:
    """Bad input must not persist anything and must re-render the
    form with the validator's error string."""
    app, db_path = setup_route_app
    resp = await app.test_client().post(
        "/setup",
        form={
            "username": "alice space",
            "password": "secretpass1",
            "confirm_password": "secretpass1",
        },
    )
    assert resp.status_code == 200
    assert "letters" in (await resp.get_data(as_text=True))
    with _db_file_conn(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM owner").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# /login signs JWT with persisted username (mirror invariant of /setup)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_signs_jwt_with_persisted_username(
    setup_route_app: tuple[Quart, str],
) -> None:
    app, db_path = setup_route_app
    # Seed an owner with a custom username (simulating a zone that
    # already completed /setup under the new feature).
    pw_hash = bcrypt.hashpw(b"loginpass1", bcrypt.gensalt()).decode()
    with _db_file_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO owner (id, username, password_hash) VALUES (1, ?, ?)",
            ("alice", pw_hash),
        )
        conn.commit()

    resp = await app.test_client().post("/login", form={"password": "loginpass1"})
    assert resp.status_code in (302, 303), await resp.get_data(as_text=True)
    cookie = _set_cookie_from_response(resp, COOKIE_ACCESS)
    assert cookie, "expected zone_auth cookie on login response"
    claims = pyjwt.decode(cookie, options={"verify_signature": False})
    assert claims["sub"] == "alice"
    assert claims["username"] == "alice"


# ---------------------------------------------------------------------------
# proxy._identity_headers: owner detection follows persisted username
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_headers_emits_owner_only_for_matching_sub(
    settings_route_app: Quart,
    db_path: str,
) -> None:
    """Pins all three branches of the proxy's owner check:
    owner sub  => X-OpenHost-Is-Owner: true
    other sub  => no header (forward-compat for future non-owner tokens)
    no claims  => no header
    """
    _seed_owner(db_path, "alice")
    async with settings_route_app.test_request_context("/x"):
        assert _identity_headers({"sub": "alice", "username": "alice"}) == {"X-OpenHost-Is-Owner": "true"}
    async with settings_route_app.test_request_context("/x"):
        assert _identity_headers({"sub": "someone-else"}) == {}
    async with settings_route_app.test_request_context("/x"):
        assert _identity_headers(None) == {}


@pytest.mark.asyncio
async def test_identity_headers_omits_owner_pre_setup(settings_route_app: Quart) -> None:
    """No owner row => even a (somehow) valid-looking JWT must not
    inherit owner privilege.  Without this guard, a stale token
    from a wiped instance could grant impersonation against the
    freshly-claimed zone."""
    async with settings_route_app.test_request_context("/x"):
        assert _identity_headers({"sub": "owner"}) == {}


# ---------------------------------------------------------------------------
# Middleware refresh path
# ---------------------------------------------------------------------------


def _seed_refresh_token(db_path: str, token: str) -> None:
    """Insert a non-revoked refresh_tokens row valid for 30 days."""
    expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with _db_file_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO refresh_tokens (token_hash, expires_at) VALUES (?, ?)",
            (hashlib.sha256(token.encode()).hexdigest(), expires_at),
        )
        conn.commit()


def _seed_api_token(db_path: str, token: str, name: str = "test-token") -> None:
    """Insert an api_tokens row valid for 30 days."""
    expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    with _db_file_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO api_tokens (name, token_hash, expires_at) VALUES (?, ?, ?)",
            (name, hashlib.sha256(token.encode()).hexdigest(), expires_at),
        )
        conn.commit()


@pytest.fixture
def refresh_app(tmp_path: Path, db_path: str) -> Iterator[tuple[Quart, str]]:
    """Quart app + JWT keys loaded, ready for _try_refresh tests."""
    cfg = _make_test_cfg(tmp_path / "refresh-data")
    load_keys(str(tmp_path / "refresh-keys"))
    app = _make_quart_app(db_path, cfg=cfg)
    with patch("compute_space.core.auth.tokens.get_config", return_value=cfg):
        yield app, db_path


@pytest.mark.asyncio
async def test_middleware_refresh_picks_up_changed_username(
    refresh_app: tuple[Quart, str],
) -> None:
    """The refresh path must read the *current* owner.username from
    the DB rather than re-using the stale ``sub`` from the expired
    token.

    Scenario: operator sets up as ``alice``, renames themselves to
    ``alice2`` via /api/settings/owner_username, then their access
    token expires.  The refreshed JWT's ``sub`` must be ``alice2``;
    otherwise the proxy silently drops X-OpenHost-Is-Owner for the
    rest of the session."""
    app, db_path = refresh_app
    refresh_tok = "refresh-token-12345"
    _seed_owner(db_path, "alice")
    _seed_refresh_token(db_path, refresh_tok)

    old_token = create_access_token("alice")
    # Operator renames themselves while their session is alive.
    with _db_file_conn(db_path) as conn:
        conn.execute("UPDATE owner SET username = ? WHERE id = 1", ("alice2",))
        conn.commit()

    async with app.test_request_context(
        "/x",
        headers={"Cookie": f"{COOKIE_ACCESS}={old_token}; {COOKIE_REFRESH}={refresh_tok}"},
    ):
        claims = _try_refresh()
        new_jwt = g.new_access_token

    assert claims is not None
    decoded = pyjwt.decode(new_jwt, options={"verify_signature": False})
    assert decoded["sub"] == "alice2"
    assert decoded["username"] == "alice2"


@pytest.mark.asyncio
async def test_middleware_refresh_returns_none_when_owner_row_missing(
    refresh_app: tuple[Quart, str],
) -> None:
    """Stricter than "any valid refresh token works": a pre-setup
    or wiped instance must refuse to mint from a leftover refresh
    cookie.  Otherwise a stale cookie could bootstrap into a
    freshly-claimed zone via refresh-only flow."""
    app, db_path = refresh_app
    refresh_tok = "refresh-token-orphan"
    _seed_refresh_token(db_path, refresh_tok)  # no owner row inserted

    old_token = create_access_token("ghost")
    async with app.test_request_context(
        "/x",
        headers={"Cookie": f"{COOKIE_ACCESS}={old_token}; {COOKIE_REFRESH}={refresh_tok}"},
    ):
        assert _try_refresh() is None


# ---------------------------------------------------------------------------
# _validate_api_token: same owner-row gate as middleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_api_token_returns_owner_username(
    settings_route_app: Quart,
    db_path: str,
) -> None:
    """Bearer tokens mint claims whose sub/username reflect the
    current owner.username — apps presenting the token are treated
    as the owner via the proxy's identity-headers comparison."""
    _seed_owner(db_path, "alice")
    api_token = "api-token-12345"
    _seed_api_token(db_path, api_token)

    async with settings_route_app.test_request_context("/x"):
        claims = validate_api_token(api_token)

    assert claims is not None
    assert claims["sub"] == "alice"
    assert claims["username"] == "alice"


@pytest.mark.asyncio
async def test_validate_api_token_returns_none_when_owner_missing(
    settings_route_app: Quart,
    db_path: str,
) -> None:
    """Same orphan-cookie defence as the refresh-middleware gate
    above, but for the api-token path."""
    api_token = "api-token-orphan"
    _seed_api_token(db_path, api_token, name="orphan-token")

    async with settings_route_app.test_request_context("/x"):
        assert validate_api_token(api_token) is None
