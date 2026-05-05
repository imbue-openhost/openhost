"""Unit tests for ``compute_space.core.archive_backend``.

These cover the DB-state machinery, the path-resolution helpers, and
the switch-backend orchestration with all subprocess work mocked out.
The juicefs-mount + S3 round-trip is exercised separately on a real
VM (see the PR description).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from compute_space.core import archive_backend
from compute_space.core.archive_backend import AppHook
from compute_space.core.archive_backend import BackendSwitchError
from compute_space.core.archive_backend import apply_backend_to_config
from compute_space.core.archive_backend import archive_dir_for_backend
from compute_space.core.archive_backend import juicefs_mount_dir
from compute_space.core.archive_backend import read_state
from compute_space.core.archive_backend import switch_backend
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


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


def test_seeded_state_is_disabled_idle(db):
    """A fresh DB built from schema.sql comes up at backend='disabled' so apps opting into the app_archive tier refuse to install until the operator picks a backend on the System tab."""
    state = read_state(db)
    assert state.backend == "disabled"
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
    assert archive_dir_for_backend(cfg, "local") == os.path.join(cfg.persistent_data_dir, "app_archive")
    assert archive_dir_for_backend(cfg, "s3") == juicefs_mount_dir(cfg)
    assert archive_dir_for_backend(cfg, "disabled") is None


def test_bucket_url_aws_default():
    """AWS bucket URL: no endpoint -> region-suffixed virtual-host URL; per-zone isolation under a shared bucket goes via the JuiceFS volume name (not the bucket URL) because JuiceFS treats the first path component as the bucket name."""
    assert archive_backend._bucket_url("mybucket", "us-west-2", None) == "https://mybucket.s3.us-west-2.amazonaws.com"


def test_bucket_url_aws_default_region_fallback():
    """Empty/unset region falls back to us-east-1 to match JuiceFS's own default and avoid the bucket URL collapsing into ``mybucket.s3..amazonaws.com`` (extra dot AWS DNS rejects)."""
    assert archive_backend._bucket_url("mybucket", "", None) == "https://mybucket.s3.us-east-1.amazonaws.com"


def test_bucket_url_with_custom_endpoint():
    """Non-AWS endpoint (MinIO, etc.) is path-style: the bucket rides as a path component on the explicit endpoint URL."""
    assert (
        archive_backend._bucket_url("mybucket", "us-east-1", "https://minio.example.com:9000")
        == "https://minio.example.com:9000/mybucket"
    )


def test_bucket_url_endpoint_strips_trailing_slash():
    """Operator-supplied ``https://minio:9000/`` (with trailing slash) is normalised to avoid ``//mybucket``, which JuiceFS's parser would treat as bucket-name = '' after split."""
    assert (
        archive_backend._bucket_url("mybucket", "us-east-1", "https://minio.example.com:9000/")
        == "https://minio.example.com:9000/mybucket"
    )


def test_format_volume_passes_no_agent_flag(cfg):
    """``juicefs format`` must run with ``--no-agent`` (a JuiceFS global flag, so it must precede the subcommand) to avoid leaving the pprof debug HTTP server bound on 6060/6061 across backend switches."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend.subprocess, "run", side_effect=fake_run):
        archive_backend.format_volume(
            cfg,
            s3_bucket="mybucket",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_access_key_id="AKIA",
            s3_secret_access_key="hunter2",
            juicefs_volume_name="andrew-3",
        )

    cmd = captured["cmd"]
    assert "--no-agent" in cmd, cmd
    no_agent_idx = cmd.index("--no-agent")
    format_idx = cmd.index("format")
    assert no_agent_idx < format_idx, "--no-agent is a JuiceFS global flag and must precede the subcommand"


def test_mount_passes_no_agent_flag(cfg):
    """``juicefs mount`` must run with ``--no-agent`` (a JuiceFS global flag, so it must precede the subcommand) because the stage-0 supervisor and stage-3 daemon would otherwise each bind a pprof port and surface as unexpected listeners."""
    captured: dict[str, list[str]] = {}

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            captured["cmd"] = list(cmd)
            self._poll_return: int | None = None

        def poll(self):
            return self._poll_return

        def terminate(self):
            self._poll_return = -15

        def wait(self, timeout=None):
            return self._poll_return

    with (
        mock.patch.object(archive_backend, "is_mounted", return_value=True),
        mock.patch.object(archive_backend.subprocess, "Popen", FakePopen),
        mock.patch.object(archive_backend, "_mount_proc", None),
    ):
        archive_backend.mount(cfg, "AKIA", "hunter2")

    cmd = captured.get("cmd")
    if cmd is None:
        states = iter([False, True])
        with (
            mock.patch.object(archive_backend, "is_mounted", side_effect=lambda _: next(states)),
            mock.patch.object(archive_backend.subprocess, "Popen", FakePopen),
            mock.patch.object(archive_backend, "_mount_proc", None),
        ):
            archive_backend.mount(cfg, "AKIA", "hunter2")
        cmd = captured["cmd"]

    assert "--no-agent" in cmd, cmd
    no_agent_idx = cmd.index("--no-agent")
    mount_idx = cmd.index("mount")
    assert no_agent_idx < mount_idx, "--no-agent is a JuiceFS global flag and must precede the subcommand"


def test_juicefs_state_and_runtime_dirs_are_separate_and_under_data_path(cfg):
    """The on-disk layout splits JuiceFS state into two trees: critical
    (``state/``, must back up) and regenerable (``runtime/``).  Both
    live under ``openhost_data_path/juicefs/`` so the existing backup
    flow picks the critical tree up by path-prefix matching.
    """
    state_dir = archive_backend._juicefs_state_dir(cfg)
    runtime_dir = archive_backend._juicefs_runtime_dir(cfg)
    juicefs_root = os.path.join(cfg.openhost_data_path, "juicefs")
    assert state_dir == os.path.join(juicefs_root, "state")
    assert runtime_dir == os.path.join(juicefs_root, "runtime")
    assert not state_dir.startswith(runtime_dir + os.sep)
    assert not runtime_dir.startswith(state_dir + os.sep)


def test_juicefs_meta_db_lives_in_state_dir(cfg):
    """The metadata DB is the single must-back-up file, so it must
    live in the critical-state directory (the one whose loss makes
    the S3 bucket bytes uninterpretable without a meta-dump replay).
    """
    meta_db = archive_backend._juicefs_meta_db(cfg)
    state_dir = archive_backend._juicefs_state_dir(cfg)
    assert meta_db == os.path.join(state_dir, "meta.db")


def test_juicefs_meta_db_path_is_public_alias(cfg):
    """``juicefs_meta_db_path`` (no underscore) is the route layer's
    entrypoint to the meta-DB path.  Must agree with the private
    helper so an operator-visible "back this file up" message and
    the actual file the migration writes are guaranteed identical.
    """
    assert archive_backend.juicefs_meta_db_path(cfg) == archive_backend._juicefs_meta_db(cfg)


def test_migrate_legacy_layout_renames_old_meta_db(cfg, tmp_path):
    """A pre-tidy zone with its meta DB at ``<openhost_data_path>/juicefs-meta.db`` is moved to the new path on first boot after the layout-tidy commit; pins the upgrade-on-first-boot behaviour."""
    legacy_meta = os.path.join(cfg.openhost_data_path, "juicefs-meta.db")
    new_meta = archive_backend._juicefs_meta_db(cfg)
    os.makedirs(cfg.openhost_data_path, exist_ok=True)
    Path(legacy_meta).write_bytes(b"FAKE_SQLITE_HEADER\x00pretend-meta-db")
    assert os.path.isfile(legacy_meta)
    assert not os.path.exists(new_meta)

    archive_backend._migrate_legacy_layout(cfg)

    assert not os.path.exists(legacy_meta), "legacy path should have been renamed away"
    assert os.path.isfile(new_meta), "new path should now hold the file"
    assert Path(new_meta).read_bytes() == b"FAKE_SQLITE_HEADER\x00pretend-meta-db"


def test_migrate_legacy_layout_is_idempotent(cfg):
    """Subsequent boots must not re-rename or otherwise touch the
    file once it's at the new path.  ``install_juicefs`` calls the
    migration on every install, so non-idempotency would mean every
    s3 switch re-fires the ``Migrated JuiceFS metadata DB`` log line.
    """
    new_meta = archive_backend._juicefs_meta_db(cfg)
    os.makedirs(os.path.dirname(new_meta), exist_ok=True)
    Path(new_meta).write_bytes(b"already-at-new-path")

    archive_backend._migrate_legacy_layout(cfg)
    archive_backend._migrate_legacy_layout(cfg)

    assert Path(new_meta).read_bytes() == b"already-at-new-path"


def test_migrate_legacy_layout_does_not_clobber_new_meta(cfg):
    """If both legacy and new paths exist, migration must not overwrite either; silently picking one of two diverging meta DBs could turn an operator's inconsistency into permanent data loss."""
    legacy_meta = os.path.join(cfg.openhost_data_path, "juicefs-meta.db")
    new_meta = archive_backend._juicefs_meta_db(cfg)
    os.makedirs(cfg.openhost_data_path, exist_ok=True)
    os.makedirs(os.path.dirname(new_meta), exist_ok=True)
    Path(legacy_meta).write_bytes(b"old-data")
    Path(new_meta).write_bytes(b"new-data")

    archive_backend._migrate_legacy_layout(cfg)

    assert Path(legacy_meta).read_bytes() == b"old-data"
    assert Path(new_meta).read_bytes() == b"new-data"


def test_migrate_legacy_layout_moves_legacy_binary_to_runtime_bin(cfg):
    """The legacy install dir put the JuiceFS binary at
    ``<openhost_data_path>/juicefs/juicefs-<version>``.  The new
    layout has ``<openhost_data_path>/juicefs/runtime/bin/juicefs-<version>``.
    Migration moves the binary into the new home so we don't
    re-download it on the post-upgrade boot.
    """
    legacy_install = os.path.join(cfg.openhost_data_path, "juicefs")
    os.makedirs(legacy_install, exist_ok=True)
    legacy_binary = os.path.join(legacy_install, f"juicefs-{archive_backend.JUICEFS_VERSION}")
    Path(legacy_binary).write_bytes(b"#!/bin/sh\nfake juicefs binary\n")

    archive_backend._migrate_legacy_layout(cfg)

    new_binary = archive_backend._juicefs_binary(cfg)
    assert os.path.isfile(new_binary), "binary should move into runtime/bin/"
    assert not os.path.exists(legacy_binary), "legacy binary should be renamed away"


def test_list_meta_dumps_summarises_dump_objects():
    """The summariser walks ``<bucket>/<prefix>/meta/`` and reports count + most-recent timestamp; used by the System tab to render the metadata-dump status without dashboard JS talking to S3 directly."""
    fake_resp = {
        "Contents": [
            {
                "Key": "andrew-3/meta/dump-2026-05-01-180000.json.gz",
                "LastModified": dt.datetime(2026, 5, 1, 18, 0, 0, tzinfo=dt.UTC),
            },
            {
                "Key": "andrew-3/meta/dump-2026-05-01-190000.json.gz",
                "LastModified": dt.datetime(2026, 5, 1, 19, 0, 0, tzinfo=dt.UTC),
            },
            {
                "Key": "andrew-3/meta/README.txt",
                "LastModified": dt.datetime(2026, 5, 1, 12, 0, 0, tzinfo=dt.UTC),
            },
        ]
    }
    fake_client = mock.MagicMock()
    fake_client.list_objects_v2.return_value = fake_resp
    with mock.patch("boto3.client", return_value=fake_client):
        summary = archive_backend.list_meta_dumps("imbue-openhost", "us-west-2", None, "AKIA", "hunter2", "andrew-3")
    assert summary is not None
    assert summary.count == 2
    assert summary.latest_key == "andrew-3/meta/dump-2026-05-01-190000.json.gz"
    assert "2026-05-01" in (summary.latest_at or "")
    fake_client.list_objects_v2.assert_called_once()
    call = fake_client.list_objects_v2.call_args
    assert call.kwargs["Bucket"] == "imbue-openhost"
    assert call.kwargs["Prefix"] == "andrew-3/meta/"


def test_list_meta_dumps_empty_bucket_returns_zero_count():
    """A freshly-formatted bucket returns a structured summary with count=0 (not None) so the dashboard distinguishes "no dumps yet" from "list failed"."""
    fake_client = mock.MagicMock()
    fake_client.list_objects_v2.return_value = {}
    with mock.patch("boto3.client", return_value=fake_client):
        summary = archive_backend.list_meta_dumps("imbue-openhost", "us-west-2", None, "AKIA", "hunter2", "andrew-3")
    assert summary is not None
    assert summary.count == 0
    assert summary.latest_at is None
    assert summary.latest_key is None


def test_list_meta_dumps_returns_none_on_s3_failure():
    """When the list call raises, return None instead of propagating so the GET path serving the dashboard stays responsive on partial S3 outage."""
    fake_client = mock.MagicMock()
    fake_client.list_objects_v2.side_effect = RuntimeError("S3 unreachable")
    with mock.patch("boto3.client", return_value=fake_client):
        summary = archive_backend.list_meta_dumps("imbue-openhost", "us-west-2", None, "AKIA", "hunter2", "andrew-3")
    assert summary is None


def test_list_meta_dumps_handles_no_prefix():
    """Operator running without a prefix (single-zone bucket): the
    list_prefix collapses to plain ``meta/`` instead of ``<prefix>/meta/``.
    """
    fake_client = mock.MagicMock()
    fake_client.list_objects_v2.return_value = {"Contents": []}
    with mock.patch("boto3.client", return_value=fake_client):
        archive_backend.list_meta_dumps("imbue-openhost", "us-west-2", None, "AKIA", "hunter2", None)
    fake_client.list_objects_v2.assert_called_once()
    assert fake_client.list_objects_v2.call_args.kwargs["Prefix"] == "meta/"


def test_is_archive_dir_healthy_local(cfg, db):
    """For the local backend the check just verifies the archive directory exists; sets backend='local' to exercise the dir-exists path (the v7 'disabled' default short-circuits to False)."""
    db.execute("UPDATE archive_backend SET backend='local'")
    db.commit()
    assert archive_backend.is_archive_dir_healthy(cfg, db) is True


def test_is_archive_dir_healthy_disabled_returns_false(cfg, db):
    """The disabled state always returns False so any caller that gates on this (install-time precheck on archive-using apps) refuses to proceed — that's the entire point of the disabled state."""
    state = archive_backend.read_state(db)
    assert state.backend == "disabled", "test setup: expected fresh seed"
    assert archive_backend.is_archive_dir_healthy(cfg, db) is False


def test_is_archive_dir_healthy_s3_uses_is_mounted(cfg, db):
    """For the s3 backend the check distinguishes 'directory exists'
    (which would be True for a dead mount) from 'mount is live'.
    Without this distinction, ``provision_data`` would silently
    accept writes that go to the underlying empty mount-point.
    """
    db.execute("UPDATE archive_backend SET backend='s3'")
    db.commit()
    os.makedirs(juicefs_mount_dir(cfg), exist_ok=True)
    assert archive_backend.is_archive_dir_healthy(cfg, db) is False
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.is_archive_dir_healthy(cfg, db) is True


def test_copy_tree_preserves_symlinks(tmp_path):
    """Symlinks in the source must be recreated as symlinks at the
    destination, not followed.  Following would expand a symlink to
    a large dir into N copies AND raise IsADirectoryError on a
    symlink to a directory once shutil.copy2 is reached.
    """
    src = tmp_path / "src"
    src.mkdir()
    real = src / "real"
    real.mkdir()
    (real / "file.txt").write_text("content")
    (src / "link-to-real").symlink_to("real")
    dst = tmp_path / "dst"

    archive_backend._copy_tree(str(src), str(dst))

    assert (dst / "real" / "file.txt").read_text() == "content"
    link_at_dst = dst / "link-to-real"
    assert link_at_dst.is_symlink()
    assert os.readlink(link_at_dst) == "real"


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
    db.execute("UPDATE archive_backend SET state='switching', state_message='copying'")
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
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id=NULL, s3_secret_access_key=NULL"
    )
    db.commit()
    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        new_cfg = archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state == "idle"
    assert "credentials" in (state.state_message or "").lower()
    assert new_cfg.archive_dir_override == juicefs_mount_dir(cfg)


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


def test_switch_disabled_to_local_happy_path(cfg, db):
    """A fresh zone (backend='disabled') configures local with no apps to stop and no data to copy; returns idle with backend='local' written. Smallest-possible switch path the disabled state introduces."""
    state_before = read_state(db)
    assert state_before.backend == "disabled", "test setup: expected fresh seed"

    hook, calls = _make_hook(archive_apps=[])
    switch_backend(cfg, db, hook, target_backend="local")

    state_after = read_state(db)
    assert state_after.backend == "local"
    assert state_after.state == "idle"
    assert state_after.last_switched_at is not None
    assert calls["stopped"] == []
    assert calls["started"] == []


def test_switch_disabled_to_s3_happy_path(cfg, db):
    """disabled -> s3 with JuiceFS install/format/mount mocked.
    Like the local-to-s3 happy path but with no source data to copy
    and no apps to bounce.
    """
    target = juicefs_mount_dir(cfg)
    os.makedirs(target, exist_ok=True)

    hook, calls = _make_hook(archive_apps=[])
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

    state_after = read_state(db)
    assert state_after.backend == "s3"
    assert state_after.state == "idle"
    assert state_after.s3_bucket == "mybucket"
    assert calls["stopped"] == []
    assert calls["started"] == []


def test_switch_local_to_disabled_rejected(cfg, db):
    """Operators cannot step a configured local backend back to disabled — that would orphan whatever's in the local-disk archive dir with no openhost-side handle to recover it."""
    db.execute("UPDATE archive_backend SET backend='local'")
    db.commit()
    hook, _ = _make_hook(archive_apps=[])
    with pytest.raises(BackendSwitchError, match="disabled"):
        switch_backend(cfg, db, hook, target_backend="disabled")
    state = read_state(db)
    assert state.backend == "local"
    assert state.state == "idle"


def test_switch_s3_to_disabled_rejected(cfg, db):
    """Like the local→disabled rejection but coming from s3: the S3 bucket would still be reachable but openhost would have no way to mount or address it; refusing keeps that surface clean."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
    )
    db.commit()
    hook, _ = _make_hook(archive_apps=[])
    with pytest.raises(BackendSwitchError, match="disabled"):
        switch_backend(cfg, db, hook, target_backend="disabled")
    state = read_state(db)
    assert state.backend == "s3"
    assert state.state == "idle"


def test_switch_disabled_to_disabled_is_noop(cfg, db):
    """``target_backend == current.backend`` is a no-op for every pair of equal values, including disabled→disabled, so dashboard re-saves don't trigger the full switch flow on idempotent re-renders."""
    hook, calls = _make_hook(archive_apps=[])
    switch_backend(cfg, db, hook, target_backend="disabled")
    state = read_state(db)
    assert state.backend == "disabled"
    assert state.state == "idle"
    assert calls["stopped"] == []
    assert calls["started"] == []


def test_switch_local_to_s3_happy_path(cfg, db, tmp_path):
    """End-to-end happy path: local -> s3 with JuiceFS install/format/mount mocked out. Verifies the data copy, DB state transition, and the apps stop->start ordering."""
    db.execute("UPDATE archive_backend SET backend='local'")
    db.commit()
    local_dir = Path(cfg.persistent_data_dir) / "app_archive" / "myapp"
    local_dir.mkdir(parents=True)
    (local_dir / "marker.txt").write_text("hello")

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

    assert calls["stopped"] == ["myapp"]
    assert calls["started"] == ["myapp"]
    assert calls["set_config"] == ["called"]

    state = read_state(db)
    assert state.backend == "s3"
    assert state.state == "idle"
    assert state.s3_bucket == "mybucket"
    assert state.s3_secret_access_key == "hunter2"
    assert state.last_switched_at is not None

    assert (Path(target) / "myapp" / "marker.txt").read_text() == "hello"


def test_switch_local_to_s3_with_delete_source_clears_local(cfg, db):
    """When delete_source_after_copy is set on a local->s3 switch, the local-disk archive directory is recursively removed once the copy succeeds (and re-created empty so future deploys don't fail before another switch)."""
    db.execute("UPDATE archive_backend SET backend='local'")
    db.commit()
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

    assert not local_dir.exists()
    assert (Path(cfg.persistent_data_dir) / "app_archive").is_dir()
    assert (Path(target) / "myapp" / "marker.txt").read_text() == "hello"


def test_switch_s3_to_local_clears_credentials(cfg, db):
    """Switching back to local drops the secret access key from the DB but keeps bucket/region so the operator's next switch-back-to-S3 form is pre-filled."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='mybucket', "
        "s3_region='us-east-1', s3_access_key_id='AKIA', s3_secret_access_key='hunter2'"
    )
    db.commit()
    src = juicefs_mount_dir(cfg)
    os.makedirs(src, exist_ok=True)
    Path(src, "marker").write_text("x")
    dst = Path(cfg.persistent_data_dir) / "app_archive"
    dst.mkdir(parents=True, exist_ok=True)

    hook, _ = _make_hook()
    with (
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "is_mounted", return_value=True),
    ):
        switch_backend(cfg, db, hook, target_backend="local")

    state = read_state(db)
    assert state.backend == "local"
    assert state.s3_bucket == "mybucket"
    assert state.s3_region == "us-east-1"
    assert state.s3_access_key_id is None
    assert state.s3_secret_access_key is None
    assert (dst / "marker").read_text() == "x"


def test_switch_s3_to_local_refuses_when_source_mount_dead(cfg, db):
    """If the JuiceFS mount has dropped, an s3->local migration must refuse rather than wipe the destination and copy from the empty underlying mount-point — that would silently lose every byte the operator had on S3."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', s3_access_key_id='a', s3_secret_access_key='s'"
    )
    db.commit()
    src = juicefs_mount_dir(cfg)
    os.makedirs(src, exist_ok=True)
    hook, calls = _make_hook(archive_apps=[])
    with mock.patch.object(archive_backend, "umount"):
        with pytest.raises(BackendSwitchError, match="not live"):
            switch_backend(cfg, db, hook, target_backend="local")
    state = read_state(db)
    assert state.state == "idle"
    assert "not live" in (state.state_message or "")
    assert state.backend == "s3"


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
    """Once one caller has flipped the row to 'switching' (atomic UPDATE-WHERE-state='idle'), a second call must raise rather than silently proceed; and the no-op path (target == current) releases the lock cleanly."""
    hook, _ = _make_hook()
    switch_backend(cfg, db, hook, target_backend="local")
    state = read_state(db)
    assert state.state == "idle"

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


def test_umount_when_not_mounted_is_noop(cfg):
    """Calling umount with no live mount and no supervised process must succeed (idempotent) — an operator clicking 'switch to local' with the mount already dead expects this rather than an error."""
    archive_backend.umount(cfg)


def test_umount_failed_subprocess_clears_proc_handle(cfg, monkeypatch):
    """A failed ``juicefs umount`` invocation must NULL _mount_proc
    so a retry doesn't inherit a stale handle pointing at a process
    whose state is unknown.
    """
    fake_proc = mock.Mock()
    fake_proc.poll.return_value = None
    fake_proc.wait.return_value = 0
    monkeypatch.setattr(archive_backend, "_mount_proc", fake_proc)
    monkeypatch.setattr(archive_backend, "is_mounted", lambda _path: True)

    failure = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="device busy")
    monkeypatch.setattr(archive_backend.subprocess, "run", lambda *a, **kw: failure)

    with pytest.raises(RuntimeError, match="device busy"):
        archive_backend.umount(cfg)
    assert archive_backend._mount_proc is None


def test_install_juicefs_extracts_binary_on_sha256_match(cfg, monkeypatch):
    """Happy path: with a pinned sha256 matching the fetched tarball, install_juicefs extracts the binary at the expected path with the executable bit set; second call is idempotent."""
    fake_binary = b"#!/bin/sh\necho fake juicefs\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="juicefs")
        info.size = len(fake_binary)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(fake_binary))
    tarball_bytes = buf.getvalue()
    real_sha = hashlib.sha256(tarball_bytes).hexdigest()

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            pass

    arch_key = archive_backend._arch()
    monkeypatch.setitem(archive_backend.JUICEFS_SHA256, arch_key, real_sha)
    monkeypatch.setattr(
        archive_backend.urllib.request,
        "urlopen",
        lambda url, timeout=120: _FakeResp(tarball_bytes),
    )
    archive_backend.install_juicefs(cfg)
    binary_path = archive_backend._juicefs_binary(cfg)
    assert os.path.isfile(binary_path)
    assert oct(os.stat(binary_path).st_mode & 0o777) == oct(0o750)
    archive_backend.install_juicefs(cfg)


def test_copy_tree_skips_non_regular_entries(tmp_path):
    """FIFOs / sockets / device nodes that an operator inexplicably
    stuck under app_archive must be skipped with a warning, not
    abort the whole switch with an error.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "regular.txt").write_text("ok")
    fifo_path = src / "fifo"
    os.mkfifo(fifo_path)
    dst = tmp_path / "dst"

    archive_backend._copy_tree(str(src), str(dst))

    assert (dst / "regular.txt").read_text() == "ok"
    assert not (dst / "fifo").exists()


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
    assert not archive_backend.is_juicefs_installed(cfg)


def test_switch_failure_during_format_recovers_state(cfg, db):
    """If the format step fails, the DB state must end up back at 'idle' (with an error message) rather than wedged in 'switching'."""
    db.execute("UPDATE archive_backend SET backend='local'")
    db.commit()
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
    assert state.backend == "local"
    assert calls["stopped"] == ["myapp"]
    assert calls["started"] == ["myapp"]
