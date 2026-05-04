"""Tests for the ``/api/storage/archive_backend`` endpoints.

Drives the routes through Quart's test client so the full
form-parsing + JSON serialisation paths are exercised.  The actual
JuiceFS subprocess work in ``switch_backend`` is mocked at the
core-module boundary so these tests stay fast and don't need a
real S3 bucket.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest
from quart import Quart

import compute_space.web.routes.api.archive_backend as routes
from compute_space.core import archive_backend
from compute_space.db.connection import init_db

from .conftest import _FakeApp, _make_test_config


def _make_app(cfg) -> Quart:  # noqa: ANN001
    app = Quart(__name__)
    app.config["DB_PATH"] = cfg.db_path
    app.openhost_config = cfg  # type: ignore[attr-defined]
    # Wire the unwrapped endpoints so login_required doesn't bounce.
    app.add_url_rule(
        "/api/storage/archive_backend",
        view_func=routes.get_archive_backend.__wrapped__,  # type: ignore[attr-defined]
        methods=["GET"],
    )
    app.add_url_rule(
        "/api/storage/archive_backend",
        endpoint="post_archive_backend",
        view_func=routes.post_archive_backend.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    app.add_url_rule(
        "/api/storage/archive_backend/test_connection",
        view_func=routes.test_connection.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    return app


@pytest.fixture
def cfg(tmp_path: Path):
    return _make_test_config(tmp_path, port=20400)


@pytest.fixture
def app(cfg):
    init_db(_FakeApp(cfg.db_path))
    yield _make_app(cfg)


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_seeded_disabled_state(app):
    """A fresh DB returns the seeded ``disabled`` row with no S3
    fields set and ``archive_dir`` null (no backing exists yet).

    The default flipped from 'local' to 'disabled' in the v7 migration
    so apps that opt into the app_archive tier refuse to install on
    a brand-new zone until the operator picks a backend on the
    System tab.  Existing zones at 'local' from before v7 are
    preserved by the migration and follow a separate code path
    (covered by ``test_get_redacts_secret_when_s3`` etc.).
    """
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    assert resp.status_code == 200
    body = await resp.get_json()
    assert body["backend"] == "disabled"
    assert body["state"] == "idle"
    assert body["s3_bucket"] is None
    # archive_dir is null on disabled — no host-side backing exists.
    # The dashboard renders this as "(not yet provisioned)" rather
    # than as an empty <code> block.
    assert body["archive_dir"] is None
    # meta_dumps is null too (no S3 to list).
    assert body["meta_dumps"] is None
    # The secret access key field must NEVER be in the response.
    assert "s3_secret_access_key" not in body


@pytest.mark.asyncio
async def test_get_redacts_secret_when_s3(app):
    """In the s3 backend the access_key_id is visible (so the
    dashboard can display "currently using AKIA…") but the secret is
    never returned, even to authenticated requests."""
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIASOMETHING', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["s3_access_key_id"] == "AKIASOMETHING"
    assert "s3_secret_access_key" not in body
    assert body["s3_bucket"] == "b"


@pytest.mark.asyncio
async def test_get_surfaces_meta_db_path(app):
    """The dashboard renders ``meta_db_path`` so an operator can see
    where the must-back-up file lives.  Surfaced for both backends —
    on local the path is the one a future s3 switch would use, so
    the operator can pre-plan their backup story before flipping
    the switch.
    """
    client = app.test_client()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    # Path is canonical: under ``juicefs/state/`` so it's grouped
    # with the rest of the must-back-up state.
    assert "meta_db_path" in body
    assert body["meta_db_path"].endswith("/juicefs/state/meta.db"), body["meta_db_path"]


@pytest.mark.asyncio
async def test_get_surfaces_meta_dumps_when_s3(app):
    """When backend=s3, GET runs ``list_meta_dumps`` and surfaces the
    summary so the dashboard can render "Last metadata dump: <ts>".
    Mocked at the core helper level to avoid hitting S3.
    """
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_prefix='zone-a', s3_access_key_id='AKIA', "
            "s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    summary = archive_backend.MetaDumpSummary(
        count=42,
        latest_at="2026-05-01T18:00:00Z",
        latest_key="zone-a/meta/dump-2026-05-01-180000.json.gz",
    )
    client = app.test_client()
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=summary):
        resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["meta_dumps"] == {
        "count": 42,
        "latest_at": "2026-05-01T18:00:00Z",
        "latest_key": "zone-a/meta/dump-2026-05-01-180000.json.gz",
    }


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_non_s3_backends(app):
    """Backends other than s3 (disabled, local) have no S3 to list,
    so ``meta_dumps`` is deliberately ``None`` so the dashboard
    renders nothing in that column rather than a misleading
    "0 dumps" message.

    Covers both the seeded-default (disabled) and an explicit
    local zone in one test because the route-layer logic skips the
    list_meta_dumps call for both cases.
    """
    client = app.test_client()
    # Default (disabled) state.
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "disabled"
    assert body["meta_dumps"] is None

    # Pre-v7 zones at backend='local'.
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute("UPDATE archive_backend SET backend='local'")
        db.commit()
    finally:
        db.close()
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "local"
    assert body["meta_dumps"] is None


@pytest.mark.asyncio
async def test_get_meta_dumps_null_on_s3_list_failure(app):
    """When ``list_meta_dumps`` returns None (S3 unreachable, etc.),
    we propagate that through to the response as ``meta_dumps: null``.
    The dashboard distinguishes this from the "0 dumps" case by
    rendering a yellow "status unavailable" line instead of a red
    "no dumps yet" warning.
    """
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
        )
        db.commit()
    finally:
        db.close()

    client = app.test_client()
    with mock.patch.object(archive_backend, "list_meta_dumps", return_value=None):
        resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["meta_dumps"] is None


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_rejects_unknown_backend(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "blob", "confirm_data_loss": "true"},
    )
    assert resp.status_code == 400
    assert "local" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_rejects_disabled_as_target(app):
    """``backend=disabled`` is intentionally not a valid POST target.

    The disabled state is the seed for fresh zones; once an operator
    has picked local or s3 they can flip between those two but
    can't go back to disabled (which would orphan the on-disk /
    in-bucket archive bytes with no openhost-side handle to recover
    them).  Surfacing this rejection at the route layer with a 400
    means the dashboard's switch form doesn't even need to expose
    a 'Disable archive tier' button.
    """
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "disabled", "confirm_data_loss": "true"},
    )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert "local" in body["error"] or "s3" in body["error"], body


@pytest.mark.asyncio
async def test_post_requires_confirm_data_loss(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "s3", "s3_bucket": "b", "s3_access_key_id": "a", "s3_secret_access_key": "s"},
    )
    assert resp.status_code == 400
    assert "confirm_data_loss" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_s3_requires_creds(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={"backend": "s3", "confirm_data_loss": "true"},
    )
    assert resp.status_code == 400
    assert "Missing required fields" in (await resp.get_json())["error"]


@pytest.mark.asyncio
async def test_post_rejects_invalid_s3_prefix(app):
    """A malformed prefix (path traversal, weird characters,
    multi-segment, uppercase, too-short, etc.) must be rejected at
    the route layer.  We want the dashboard form to bounce bad
    input with a clear message rather than have the operator stare
    at a generic 'juicefs format failed: invalid name' error 30 s
    later.

    The accepted shape is JuiceFS's volume-name regex
    (``^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$``) because the prefix is
    used directly as the JuiceFS volume name on the format step.
    """
    client = app.test_client()
    bads = (
        "../etc",            # traversal-style
        "with space",        # no whitespace allowed
        "embedded\x00null",  # NUL banned
        "a/b",               # multi-segment forbidden — JuiceFS regex has no /
        "UPPER",             # uppercase forbidden
        "under_score",       # underscore forbidden by regex
        "ab",                # too short (regex requires len 3+)
        "-leading-dash",     # dash leader forbidden
        "trailing-dash-",    # dash trailer forbidden
        "with.dot",          # dot forbidden by JuiceFS regex
    )
    for bad in bads:
        resp = await client.post(
            "/api/storage/archive_backend",
            form={
                "backend": "s3",
                "confirm_data_loss": "true",
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                "s3_prefix": bad,
            },
        )
        body = await resp.get_json()
        assert resp.status_code == 400, (bad, body)
        assert "s3_prefix" in body["error"], (bad, body)


@pytest.mark.asyncio
async def test_post_rejects_when_already_switching(app):
    db = sqlite3.connect(app.config["DB_PATH"])
    try:
        db.execute("UPDATE archive_backend SET state='switching'")
        db.commit()
    finally:
        db.close()
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend",
        form={
            "backend": "s3",
            "confirm_data_loss": "true",
            "s3_bucket": "b",
            "s3_access_key_id": "a",
            "s3_secret_access_key": "s",
        },
    )
    assert resp.status_code == 409
    assert "already in progress" in (await resp.get_json())["error"]


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_local_to_s3_returns_202_and_runs_switch(app, cfg):
    """Switching local -> s3 returns 202 with state=switching; the
    background thread eventually flips the DB row to s3/idle.
    """
    # Pre-create the JuiceFS mount target so the (mocked) mount
    # leaves us with somewhere to copy into.
    juicefs_mount = archive_backend.juicefs_mount_dir(cfg)
    Path(juicefs_mount).mkdir(parents=True, exist_ok=True)

    client = app.test_client()
    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(archive_backend, "format_volume"),
        mock.patch.object(archive_backend, "mount"),
    ):
        resp = await client.post(
            "/api/storage/archive_backend",
            form={
                "backend": "s3",
                "confirm_data_loss": "true",
                "s3_bucket": "mybucket",
                "s3_region": "us-east-1",
                "s3_access_key_id": "AKIA",
                "s3_secret_access_key": "hunter2",
            },
        )
        assert resp.status_code == 202
        body = await resp.get_json()
        assert body["state"] == "switching"

        # Wait for the worker thread to finish.  The switch is small
        # (no real S3 work) so this should settle very quickly.
        deadline = time.time() + 5
        while time.time() < deadline:
            db = sqlite3.connect(cfg.db_path)
            try:
                row = db.execute(
                    "SELECT backend, state FROM archive_backend WHERE id=1"
                ).fetchone()
            finally:
                db.close()
            if row[0] == "s3" and row[1] == "idle":
                break
            time.sleep(0.05)

    # GET reflects the new state and still redacts the secret.
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["state"] == "idle"
    assert body["s3_bucket"] == "mybucket"
    assert "s3_secret_access_key" not in body
    # And the resolved archive_dir now points at the JuiceFS mount.
    assert body["archive_dir"] == juicefs_mount


@pytest.mark.asyncio
async def test_post_local_to_s3_with_prefix_persists_prefix(app, cfg):
    """When the operator supplies a non-empty s3_prefix on the
    switch form, it must round-trip cleanly:

      - become the JuiceFS volume name passed to format_volume
        (NOT a separate s3_prefix kwarg — see the long comment on
        ``_bucket_url`` in core.archive_backend for why JuiceFS
        won't accept a path component on the bucket URL)
      - be persisted as both ``s3_prefix`` and
        ``juicefs_volume_name`` in the DB row
      - be surfaced on the next GET response in both fields
    """
    juicefs_mount = archive_backend.juicefs_mount_dir(cfg)
    Path(juicefs_mount).mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    def _capture_format(*args, **kwargs):
        # format_volume is called with config as a positional and
        # the rest as kwargs in the production call site; this
        # signature accepts both shapes defensively.
        captured.update(kwargs)
        if args:
            captured["_positional_count"] = len(args)

    client = app.test_client()
    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(archive_backend, "format_volume", side_effect=_capture_format),
        mock.patch.object(archive_backend, "mount"),
    ):
        resp = await client.post(
            "/api/storage/archive_backend",
            form={
                "backend": "s3",
                "confirm_data_loss": "true",
                "s3_bucket": "imbue-openhost",
                "s3_region": "us-west-2",
                "s3_prefix": "andrew-3",
                "s3_access_key_id": "AKIA",
                "s3_secret_access_key": "hunter2",
            },
        )
        assert resp.status_code == 202

        deadline = time.time() + 5
        while time.time() < deadline:
            db = sqlite3.connect(cfg.db_path)
            try:
                row = db.execute(
                    "SELECT backend, state FROM archive_backend WHERE id=1"
                ).fetchone()
            finally:
                db.close()
            if row[0] == "s3" and row[1] == "idle":
                break
            time.sleep(0.05)

    # format_volume sees the prefix as the JuiceFS volume name.
    # Important: NOT as a separate ``s3_prefix`` kwarg — that field
    # has been deliberately removed from format_volume's signature
    # because JuiceFS can't take a path-segment on the bucket URL.
    assert captured["juicefs_volume_name"] == "andrew-3", captured
    assert "s3_prefix" not in captured, (
        "format_volume should NOT receive an s3_prefix kwarg; it must "
        "go through juicefs_volume_name instead.  Captured: " + str(captured)
    )

    # GET surfaces both fields as we stored them: s3_prefix is the
    # operator-visible name; juicefs_volume_name is the same value
    # written verbatim, kept in its own column for the migration-
    # path code that already keys off it.
    resp = await client.get("/api/storage/archive_backend")
    body = await resp.get_json()
    assert body["backend"] == "s3"
    assert body["s3_prefix"] == "andrew-3"
    assert body["juicefs_volume_name"] == "andrew-3"
    assert body["s3_bucket"] == "imbue-openhost"
    assert body["s3_region"] == "us-west-2"


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_test_connection_requires_fields(app):
    client = app.test_client()
    resp = await client.post(
        "/api/storage/archive_backend/test_connection",
        form={"s3_bucket": "b"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_test_connection_rejects_invalid_s3_prefix(app):
    """The pre-flight endpoint validates s3_prefix shape too — same
    rules as the switch POST — so the operator catches typos before
    the actual switch runs.  The bad-prefix branch must reject
    BEFORE we burn a head_bucket round-trip on it.
    """
    client = app.test_client()
    with mock.patch.object(archive_backend, "test_s3_credentials") as mocked:
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
                # Multi-segment prefix used to be accepted; the new
                # contract rejects it because the prefix has to map
                # 1:1 to a JuiceFS volume name (which forbids /).
                "s3_prefix": "a/b",
            },
        )
        body = await resp.get_json()
        assert resp.status_code == 400, body
        assert "s3_prefix" in body["error"], body
        # Critical: the head_bucket call must not have been made on
        # an invalid prefix — we want fail-fast, not after a network
        # round-trip.
        mocked.assert_not_called()


@pytest.mark.asyncio
async def test_test_connection_surfaces_errors(app):
    """A failed reachability check returns 400 with the underlying
    error string so the dashboard can surface it next to the form.
    """
    client = app.test_client()
    with mock.patch.object(
        archive_backend,
        "test_s3_credentials",
        return_value="bucket not found",
    ):
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
            },
        )
    assert resp.status_code == 400
    body = await resp.get_json()
    assert body["ok"] is False
    assert "bucket not found" in body["error"]


@pytest.mark.asyncio
async def test_list_archive_apps_heuristic_precision(app, cfg):
    """The heuristic that decides which apps to stop during a switch
    must match exactly ``app_archive = true`` (or
    ``access_all_data = true``), not the substring "true" anywhere
    in the manifest.  Without this, an app with
    ``app_archive = false`` plus ``app_data = true`` would be
    erroneously stopped — which means a routine s3 backend switch
    would needlessly bounce every app on the zone that happened to
    have any boolean opt-in.
    """
    # Seed three apps with manifests covering the relevant cases.
    db = sqlite3.connect(cfg.db_path)
    try:
        db.executemany(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, '1.0', ?, ?, 'running', ?)",
            [
                # Should match: explicit app_archive = true.
                ("real-archiver", "/r/a", 19501, "[data]\napp_archive = true\n"),
                # Should match: access_all_data = true.
                (
                    "all-access",
                    "/r/aa",
                    19502,
                    "[data]\naccess_all_data = true\n",
                ),
                # Should NOT match: app_archive=false with a different
                # boolean=true elsewhere — the old substring heuristic
                # got this wrong.
                (
                    "innocent",
                    "/r/i",
                    19503,
                    "[data]\napp_archive = false\napp_data = true\n",
                ),
                # Should NOT match: no archive/access fields at all.
                ("plain", "/r/p", 19504, "[data]\napp_data = true\n"),
            ],
        )
        db.commit()
    finally:
        db.close()

    hook = routes._build_hook(app)
    matched = sorted(hook.list_app_archive_apps())
    assert matched == ["all-access", "real-archiver"], matched


@pytest.mark.asyncio
async def test_reload_app_refuses_when_archive_unhealthy(app, cfg):
    """An archive-using app cannot be reloaded while the operator-
    configured archive backend is unhealthy.  Without this guard, the
    next provision_data would write to the underlying empty mount-
    point on local disk and lose those writes once the mount came
    back.
    """
    import compute_space.web.routes.api.apps as apps_routes

    db = sqlite3.connect(cfg.db_path)
    try:
        # Mark the backend s3 with a missing mount, and seed an
        # archive-using app row.
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES ('archived', '1.0', '/r/archived', 19601, 'running', "
            "'[data]\napp_archive = true\n')"
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_name>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    resp = await client.post("/reload_app/archived")
    assert resp.status_code == 503
    body = await resp.get_json()
    assert "Archive backend is not healthy" in body["error"]


@pytest.mark.asyncio
async def test_reload_app_allows_non_archive_when_archive_unhealthy(app, cfg):
    """An app that doesn't use the archive tier must still be
    reloadable when the archive backend is unhealthy — the precheck
    is targeted, not a blanket lock-out.
    """
    import compute_space.web.routes.api.apps as apps_routes

    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
            "s3_access_key_id='a', s3_secret_access_key='s'"
        )
        db.execute(
            "INSERT INTO apps (name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES ('plain', '1.0', '/r/plain', 19602, 'running', "
            "'[data]\napp_data = true\n')"
        )
        db.commit()
    finally:
        db.close()

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/reload_app/<app_name>",
        view_func=apps_routes.reload_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    # We can't exercise the full reload flow without podman, so
    # we just verify the precheck DOESN'T return 503 — the call
    # may still fail later for unrelated reasons (which is fine
    # for this test's purposes).
    with mock.patch(
        "compute_space.web.routes.api.apps.stop_app_process"
    ), mock.patch(
        "compute_space.web.routes.api.apps.reload_app_background"
    ):
        resp = await client.post("/reload_app/plain")
        assert resp.status_code != 503, await resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Install gate: app_archive=true + backend='disabled'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_app_refuses_archive_app_when_backend_disabled(app, cfg, tmp_path):
    """An app whose manifest opts into ``app_archive`` cannot be
    installed while the archive backend is at its v7-default
    'disabled' state.

    The route returns 400 (operator-actionable) with a message that
    points at the System tab, NOT 503 (transient) — pre-v7 the
    install path always succeeded for archive apps because the
    seeded 'local' backend was always healthy.  The disabled state
    introduces a permanent rejection until the operator picks a
    backend, so 400 is the right surface.

    Mocked at the manifest-parse level to avoid cloning a real repo.
    """
    import compute_space.web.routes.api.apps as apps_routes
    from compute_space.core.manifest import AppManifest

    # Default seeded state is 'disabled' — assert the precondition.
    db = sqlite3.connect(cfg.db_path)
    try:
        backend = db.execute(
            "SELECT backend FROM archive_backend WHERE id=1"
        ).fetchone()[0]
    finally:
        db.close()
    assert backend == "disabled", "test setup: expected fresh seed"

    # Pretend a clone has already happened and produced an archive-
    # using manifest at clone_dir.  We mock parse_manifest to return
    # the manifest directly so we don't need to materialise a fake
    # openhost.toml on disk.
    archive_manifest = AppManifest(
        name="probe",
        version="1.0",
        description="archive probe",
        runtime_type="serverfull",
        container_image="Dockerfile",
        container_port=8080,
        container_command=None,
        memory_mb=128,
        cpu_millicores=100,
        gpu=False,
        app_data=True,
        app_archive=True,  # <- the trigger
        access_all_data=False,
        access_vm_data=False,
        app_temp_data=False,
        public_paths=["/"],
        capabilities=[],
        devices=[],
        permissions_v2=[],
        port_mappings=[],
        provides_services=[],
        provides_services_v2=[],
        requires_services={},
        sqlite_dbs=[],
        health_check="/",
        hidden=False,
        authors=[],
        raw_toml="",
    )
    fake_clone_dir = str(tmp_path / "clone")
    os.makedirs(fake_clone_dir)

    test_app = Quart(__name__)
    test_app.config["DB_PATH"] = cfg.db_path
    test_app.openhost_config = cfg  # type: ignore[attr-defined]
    test_app.add_url_rule(
        "/api/add_app",
        view_func=apps_routes.api_add_app.__wrapped__,  # type: ignore[attr-defined]
        methods=["POST"],
    )
    client = test_app.test_client()
    with (
        mock.patch.object(apps_routes, "parse_manifest", return_value=archive_manifest),
        mock.patch.object(apps_routes, "validate_manifest", return_value=None),
    ):
        resp = await client.post(
            "/api/add_app",
            form={
                "repo_url": "https://example.invalid/repo",
                "app_name": "probe",
                "clone_dir": fake_clone_dir,
                "grant_permissions": "",
            },
        )
    assert resp.status_code == 400, await resp.get_data(as_text=True)
    body = await resp.get_json()
    assert "archive backend" in body["error"].lower(), body
    assert "system page" in body["error"].lower(), body


@pytest.mark.asyncio
async def test_test_connection_succeeds(app):
    client = app.test_client()
    with mock.patch.object(archive_backend, "test_s3_credentials", return_value=None):
        resp = await client.post(
            "/api/storage/archive_backend/test_connection",
            form={
                "s3_bucket": "b",
                "s3_access_key_id": "a",
                "s3_secret_access_key": "s",
            },
        )
    assert resp.status_code == 200
    assert (await resp.get_json())["ok"] is True
