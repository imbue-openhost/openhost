"""Unit tests for ``compute_space.core.archive_backend``.

These cover the DB-state machinery, the path-resolution helpers, and
the switch-backend orchestration with all subprocess work mocked out.
The juicefs-mount + S3 round-trip is exercised separately on a real
VM (see the PR description).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from compute_space.core import archive_backend
from compute_space.core.archive_backend import (
    AppHook,
    BackendState,
    BackendSwitchError,
    apply_backend_to_config,
    archive_dir_for_backend,
    juicefs_mount_dir,
    read_state,
    switch_backend,
)
from compute_space.db.connection import init_db

from .conftest import _FakeApp, _make_test_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg(tmp_path: Path):
    """A test Config with all dirs created.  port chosen high enough to
    not collide with other tests' ROUTER_PORT etc.
    """
    return _make_test_config(tmp_path, port=20300)


@pytest.fixture
def db(cfg):
    """Initialised sqlite DB so the archive_backend table exists with
    the seeded ``local`` row.
    """
    init_db(_FakeApp(cfg.db_path))
    conn = sqlite3.connect(cfg.db_path)
    yield conn
    conn.close()


def _make_hook(*, archive_apps: list[str] | None = None) -> tuple[AppHook, dict[str, list[str]]]:
    """Build an AppHook whose callbacks record what was called.  Tests
    use the recorded calls to assert ordering + contents.
    """
    calls: dict[str, list[str]] = {"stopped": [], "started": [], "set_config": []}

    def list_apps() -> list[str]:
        return list(archive_apps or [])

    def stop(name: str) -> None:
        calls["stopped"].append(name)

    def start(name: str) -> None:
        calls["started"].append(name)

    def set_cfg(_cfg) -> None:  # noqa: ANN001
        calls["set_config"].append("called")

    return (
        AppHook(
            list_app_archive_apps=list_apps,
            stop_app=stop,
            start_app=start,
            set_config=set_cfg,
        ),
        calls,
    )


# ---------------------------------------------------------------------------
# State read/write
# ---------------------------------------------------------------------------


def test_seeded_state_is_local_idle(db):
    """The v4 migration seeds a single row in 'local' / 'idle' state."""
    state = read_state(db)
    assert state.backend == "local"
    assert state.state == "idle"
    assert state.s3_bucket is None
    assert state.s3_secret_access_key is None
    assert state.juicefs_volume_name == "openhost"


def test_apply_backend_to_config_local(cfg, db):
    """In the default ``local`` state, apply_backend_to_config returns a
    Config with archive_dir_override unset so app_archive_dir falls
    back to the persistent_data_dir/app_archive default."""
    new_cfg = apply_backend_to_config(cfg, db)
    assert new_cfg.archive_dir_override is None
    expected = os.path.join(cfg.persistent_data_dir, "app_archive")
    assert new_cfg.app_archive_dir == expected


def test_apply_backend_to_config_s3(cfg, db):
    """When the DB row is in s3 state, apply_backend_to_config sets
    archive_dir_override to the JuiceFS mount path so app_archive_dir
    points at it.
    """
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='mybucket', "
        "s3_access_key_id='AKIA…', s3_secret_access_key='hunter2'"
    )
    db.commit()
    new_cfg = apply_backend_to_config(cfg, db)
    assert new_cfg.archive_dir_override == juicefs_mount_dir(cfg)
    assert new_cfg.app_archive_dir == juicefs_mount_dir(cfg)


def test_archive_dir_for_backend(cfg):
    assert archive_dir_for_backend(cfg, "local") == os.path.join(
        cfg.persistent_data_dir, "app_archive"
    )
    assert archive_dir_for_backend(cfg, "s3") == juicefs_mount_dir(cfg)


# ---------------------------------------------------------------------------
# attach_on_startup
# ---------------------------------------------------------------------------


def test_attach_on_startup_local_is_no_op(cfg, db):
    """For the local backend there's nothing to attach; the function
    just returns a Config matching the DB state."""
    new_cfg = archive_backend.attach_on_startup(cfg, db)
    assert new_cfg.archive_dir_override is None


def test_attach_on_startup_clears_stale_switching_state(cfg, db):
    """If openhost-core crashed mid-switch and rebooted, the DB row
    would be left in ``state='switching'``.  Boot must not stay
    locked there; clear it and annotate state_message so the operator
    can see what happened.
    """
    db.execute(
        "UPDATE archive_backend SET state='switching', state_message='copying'"
    )
    db.commit()
    archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state == "idle"
    assert "interrupted" in (state.state_message or "")


def test_attach_on_startup_s3_happy_path(cfg, db):
    """When the persisted backend is s3 with valid creds, attach on
    boot installs juicefs (if missing), mounts, and returns a Config
    pointing at the JuiceFS mount.
    """
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
        "s3_region='us-east-1', s3_access_key_id='AKIA', "
        "s3_secret_access_key='hunter2'"
    )
    db.commit()
    install_calls: list[None] = []
    mount_calls: list[tuple[str, str]] = []

    def fake_install(_cfg) -> None:
        install_calls.append(None)

    def fake_mount(_cfg, akid: str, secret: str) -> None:
        mount_calls.append((akid, secret))

    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=False),
        mock.patch.object(archive_backend, "install_juicefs", side_effect=fake_install),
        mock.patch.object(archive_backend, "mount", side_effect=fake_mount),
    ):
        new_cfg = archive_backend.attach_on_startup(cfg, db)

    assert install_calls == [None]
    assert mount_calls == [("AKIA", "hunter2")]
    assert new_cfg.archive_dir_override == archive_backend.juicefs_mount_dir(cfg)
    state = read_state(db)
    assert state.state == "idle"
    assert state.state_message is None


def test_attach_on_startup_s3_missing_creds_records_error(cfg, db):
    """If somehow the row is in s3 state without creds, attach must
    not crash boot — record the error and let the operator fix it
    via the dashboard."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
        "s3_access_key_id=NULL, s3_secret_access_key=NULL"
    )
    db.commit()
    # Pretend juicefs is already installed so we don't try to download.
    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        new_cfg = archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state == "idle"
    assert "credentials" in (state.state_message or "").lower()
    # The Config still reflects the desired backend so subsequent
    # provision_data calls fail loudly rather than silently writing
    # to local disk.
    assert new_cfg.archive_dir_override == juicefs_mount_dir(cfg)


# ---------------------------------------------------------------------------
# switch_backend
# ---------------------------------------------------------------------------


def test_switch_to_same_backend_is_no_op(cfg, db):
    """Posting the current backend again must succeed silently so the
    dashboard's idempotent re-saves don't trip an error."""
    hook, calls = _make_hook()
    switch_backend(cfg, db, hook, target_backend="local")
    assert calls["stopped"] == []
    assert calls["started"] == []
    state = read_state(db)
    assert state.backend == "local"


def test_switch_unknown_backend_rejected(cfg, db):
    hook, _ = _make_hook()
    with pytest.raises(BackendSwitchError, match="Unknown target backend"):
        switch_backend(cfg, db, hook, target_backend="blob")


def test_switch_to_s3_requires_credentials(cfg, db):
    hook, _ = _make_hook()
    with pytest.raises(BackendSwitchError, match="bucket"):
        switch_backend(cfg, db, hook, target_backend="s3")


def test_switch_local_to_s3_happy_path(cfg, db, tmp_path):
    """End-to-end happy path: local -> s3 with JuiceFS install/format/
    mount mocked out.  Verifies the data copy, DB state transition,
    and the apps stop->start ordering.
    """
    # Pre-seed local archive with one app's worth of content so we
    # know the copy actually moved bytes.
    local_dir = Path(cfg.persistent_data_dir) / "app_archive" / "myapp"
    local_dir.mkdir(parents=True)
    (local_dir / "marker.txt").write_text("hello")

    # Pre-create the JuiceFS mount target so the (mocked) "mount"
    # leaves us with a real on-disk dir to copy into.  In real life
    # the mount step does this.
    target = juicefs_mount_dir(cfg)
    os.makedirs(target, exist_ok=True)

    hook, calls = _make_hook(archive_apps=["myapp"])

    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(archive_backend, "format_volume"),
        mock.patch.object(archive_backend, "mount"),
    ):
        switch_backend(
            cfg,
            db,
            hook,
            target_backend="s3",
            s3_bucket="mybucket",
            s3_region="us-east-1",
            s3_access_key_id="AKIA",
            s3_secret_access_key="hunter2",
        )

    # Apps were stopped before the swap and started after it.
    assert calls["stopped"] == ["myapp"]
    assert calls["started"] == ["myapp"]
    assert calls["set_config"] == ["called"]

    # DB now reflects the s3 backend with creds persisted.
    state = read_state(db)
    assert state.backend == "s3"
    assert state.state == "idle"
    assert state.s3_bucket == "mybucket"
    assert state.s3_secret_access_key == "hunter2"
    assert state.last_switched_at is not None

    # Data made it to the target.
    assert (Path(target) / "myapp" / "marker.txt").read_text() == "hello"


def test_switch_local_to_s3_with_delete_source_clears_local(cfg, db):
    """When delete_source_after_copy is set on a local->s3 switch,
    the local-disk archive directory is recursively removed once the
    copy succeeds (and re-created empty so future deploys don't
    fail before another switch).  This is how operators free local
    disk after migrating to S3.
    """
    local_dir = Path(cfg.persistent_data_dir) / "app_archive" / "myapp"
    local_dir.mkdir(parents=True)
    (local_dir / "marker.txt").write_text("hello")

    target = juicefs_mount_dir(cfg)
    os.makedirs(target, exist_ok=True)

    hook, _ = _make_hook(archive_apps=[])
    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(archive_backend, "format_volume"),
        mock.patch.object(archive_backend, "mount"),
    ):
        switch_backend(
            cfg,
            db,
            hook,
            target_backend="s3",
            s3_bucket="b",
            s3_access_key_id="a",
            s3_secret_access_key="s",
            delete_source_after_copy=True,
        )

    # Source was deleted (per-app dir gone) but the local archive
    # parent dir was recreated empty so the next deploy can mkdir
    # under it without bumping into a missing parent.
    assert not local_dir.exists()
    assert (Path(cfg.persistent_data_dir) / "app_archive").is_dir()
    # And the data made it to the new backend.
    assert (Path(target) / "myapp" / "marker.txt").read_text() == "hello"


def test_switch_s3_to_local_clears_credentials(cfg, db):
    """Switching back to local must drop the secret access key from
    the DB so it doesn't outlive its usefulness; the bucket/region
    columns stay so the operator's next switch-back-to-S3 form is
    pre-filled with the same params.
    """
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='mybucket', "
        "s3_region='us-east-1', s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
    )
    db.commit()
    # Pre-create both archive dirs so the copy phase has somewhere
    # to read from / write to without us mocking the mount.
    src = juicefs_mount_dir(cfg)
    os.makedirs(src, exist_ok=True)
    Path(src, "marker").write_text("x")
    dst = Path(cfg.persistent_data_dir) / "app_archive"
    dst.mkdir(parents=True, exist_ok=True)

    hook, _ = _make_hook()
    with mock.patch.object(archive_backend, "umount"):
        switch_backend(cfg, db, hook, target_backend="local")

    state = read_state(db)
    assert state.backend == "local"
    # Bucket / region kept (convenient for the next switch back),
    # creds dropped (sensitive — drop when no longer needed).
    assert state.s3_bucket == "mybucket"
    assert state.s3_region == "us-east-1"
    assert state.s3_access_key_id is None
    assert state.s3_secret_access_key is None
    assert (dst / "marker").read_text() == "x"


def test_switch_refuses_when_already_switching(cfg, db):
    """Concurrent switch requests must be rejected.  The dashboard
    button is supposed to be disabled while a switch is in flight, but
    a determined operator with curl shouldn't be able to wedge things.
    """
    db.execute("UPDATE archive_backend SET state='switching'")
    db.commit()
    hook, _ = _make_hook()
    with pytest.raises(BackendSwitchError, match="already in state"):
        switch_backend(
            cfg,
            db,
            hook,
            target_backend="s3",
            s3_bucket="mybucket",
            s3_access_key_id="A",
            s3_secret_access_key="B",
        )


def test_switch_state_transition_is_atomic(cfg, db):
    """Two concurrent switch_backend calls must not both proceed.

    Without the atomic UPDATE-WHERE-state='idle' transition, a
    read_state-then-update sequence would let two callers both observe
    'idle' and both enter the flow, stepping on each other's stops/
    copies/mounts.  We can't easily exercise true concurrency in a
    unit test, but we CAN exercise the symmetrical case: once one
    caller has flipped the row to 'switching', a second call must
    raise rather than silently proceed.

    Also: after a successful no-op (target == current), the second
    call must NOT see 'switching' — the no-op path releases the
    lock cleanly.
    """
    hook, _ = _make_hook()
    # No-op: target == current (both 'local'); should release lock.
    switch_backend(cfg, db, hook, target_backend="local")
    state = read_state(db)
    assert state.state == "idle"

    # Now manually wedge the row in 'switching' state, simulating an
    # in-flight switch from another process; the second call refuses.
    db.execute("UPDATE archive_backend SET state='switching'")
    db.commit()
    with pytest.raises(BackendSwitchError, match="already in state"):
        switch_backend(
            cfg,
            db,
            hook,
            target_backend="s3",
            s3_bucket="b",
            s3_access_key_id="a",
            s3_secret_access_key="s",
        )


def test_install_juicefs_rejects_sha256_mismatch(cfg, tmp_path, monkeypatch):
    """The sha256 verify is the primary defence against a compromised
    release.  A mismatched tarball must abort install with a clear
    error rather than silently writing whatever we got to disk.
    """
    fake_bytes = b"definitely-not-juicefs"

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

    monkeypatch.setattr(
        archive_backend.urllib.request,
        "urlopen",
        lambda url, timeout=120: _FakeResp(fake_bytes),
    )
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        archive_backend.install_juicefs(cfg)
    # And nothing got written to disk on the way out.
    assert not archive_backend.is_juicefs_installed(cfg)


def test_switch_failure_during_format_recovers_state(cfg, db):
    """If the format step fails, the DB state must end up back at
    'idle' (with an error message) rather than wedged in 'switching'.
    """
    hook, calls = _make_hook(archive_apps=["myapp"])

    with (
        mock.patch.object(archive_backend, "install_juicefs"),
        mock.patch.object(
            archive_backend,
            "format_volume",
            side_effect=RuntimeError("mock format failure"),
        ),
    ):
        with pytest.raises(BackendSwitchError, match="mock format failure"):
            switch_backend(
                cfg,
                db,
                hook,
                target_backend="s3",
                s3_bucket="mybucket",
                s3_access_key_id="A",
                s3_secret_access_key="B",
            )

    state = read_state(db)
    assert state.state == "idle"
    assert "mock format failure" in (state.state_message or "")
    # The backend stays at the original value (didn't flip to s3) so
    # subsequent reads see the source-of-truth state.
    assert state.backend == "local"
    # Apps got stopped AND restarted — even on failure, the switch
    # always restarts what it stopped, so the operator's retry isn't
    # left with permanently-orphaned 'stopped' apps that the next
    # ``list_app_archive_apps`` would no longer pick up.
    assert calls["stopped"] == ["myapp"]
    assert calls["started"] == ["myapp"]
