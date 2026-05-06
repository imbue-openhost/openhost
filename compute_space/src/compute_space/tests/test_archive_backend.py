"""Unit tests for ``compute_space.core.archive_backend``."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendConfigureError
from compute_space.core.archive_backend import apply_backend_to_config
from compute_space.core.archive_backend import archive_dir_for_backend
from compute_space.core.archive_backend import configure_backend
from compute_space.core.archive_backend import juicefs_mount_dir
from compute_space.core.archive_backend import read_state
from compute_space.db.connection import init_db

from .conftest import _FakeApp
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path):
    return _make_test_config(tmp_path, port=20300)


@pytest.fixture
def db(cfg):
    """Initialised sqlite DB so the archive_backend table exists, seeded disabled."""
    init_db(_FakeApp(cfg.db_path))
    conn = sqlite3.connect(cfg.db_path)
    yield conn
    conn.close()


# --- read_state / apply_backend / archive_dir_for_backend -----------------


def test_seeded_state_is_disabled(db):
    """Fresh DB comes up at backend='disabled'; apps with app_archive=true
    refuse to install until S3 is configured."""
    state = read_state(db)
    assert state.backend == "disabled"
    assert state.s3_bucket is None


def test_apply_backend_to_config_disabled(cfg, db):
    new_cfg = apply_backend_to_config(cfg, db)
    assert new_cfg.archive_dir_override is None


def test_apply_backend_to_config_s3(cfg, db):
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
        "s3_access_key_id='ak', s3_secret_access_key='sk' WHERE id=1"
    )
    db.commit()
    new_cfg = apply_backend_to_config(cfg, db)
    assert new_cfg.archive_dir_override == juicefs_mount_dir(cfg)


def test_archive_dir_for_backend(cfg):
    assert archive_dir_for_backend(cfg, "s3") == juicefs_mount_dir(cfg)
    assert archive_dir_for_backend(cfg, "disabled") is None


# --- _bucket_url -----------------------------------------------------------


def test_bucket_url_aws_default():
    """AWS bucket URL: virtual-host style with region-suffixed endpoint.
    Per-zone isolation under a shared bucket goes via the JuiceFS volume
    name, not the URL — JuiceFS treats the first path component as the
    bucket name, so any extra path here would break the lookup."""
    assert archive_backend._bucket_url("mybucket", "us-west-2", None) == "https://mybucket.s3.us-west-2.amazonaws.com"


def test_bucket_url_aws_default_region_fallback():
    """Empty region falls back to us-east-1 (matches JuiceFS default)."""
    assert archive_backend._bucket_url("mybucket", "", None) == "https://mybucket.s3.us-east-1.amazonaws.com"


def test_bucket_url_with_custom_endpoint():
    """Non-AWS endpoint uses path-style: <endpoint>/<bucket>."""
    assert (
        archive_backend._bucket_url("mybucket", "us-east-1", "https://minio.example.com:9000")
        == "https://minio.example.com:9000/mybucket"
    )


def test_bucket_url_endpoint_strips_trailing_slash():
    """Operator-supplied trailing slash is normalised so we don't end up
    with ``//mybucket`` (which JuiceFS would parse as bucket-name='')."""
    assert (
        archive_backend._bucket_url("mybucket", "us-east-1", "https://minio.example.com:9000/")
        == "https://minio.example.com:9000/mybucket"
    )


# --- format/mount: --no-agent -----------------------------------------------


def test_format_volume_passes_no_agent_flag(cfg):
    """``--no-agent`` must precede the format subcommand so JuiceFS doesn't
    bind 6060+ for its pprof debug HTTP server (which our security audit
    flags as an unexpected listener)."""
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/usr/local/bin/juicefs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend.format_volume(
                cfg,
                s3_bucket="b",
                s3_region="us-east-1",
                s3_endpoint=None,
                s3_access_key_id="ak",
                s3_secret_access_key="sk",
                juicefs_volume_name="zone",
            )
    cmd = captured["cmd"]
    assert "--no-agent" in cmd
    assert cmd.index("--no-agent") < cmd.index("format")


def test_mount_passes_no_agent_flag(cfg):
    captured = {}

    class _FakeProc:
        def __init__(self):
            self.returncode = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            self.returncode = self.returncode or 0
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_popen(cmd, env, stdout, stderr):
        captured["cmd"] = cmd
        return _FakeProc()

    # is_mounted: False before Popen (so we actually start the process),
    # True after (so the readiness loop sees it succeed immediately).
    is_mounted_calls = [False, True]
    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/usr/local/bin/juicefs"):
        with mock.patch.object(
            archive_backend, "is_mounted", side_effect=lambda mp: is_mounted_calls.pop(0) if is_mounted_calls else True
        ):
            with mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
                archive_backend.mount(cfg, "ak", "sk")
    cmd = captured["cmd"]
    assert "--no-agent" in cmd
    assert cmd.index("--no-agent") < cmd.index("mount")


# --- on-disk layout helpers + legacy-layout migration ---------------------


def test_juicefs_state_and_runtime_dirs_are_separate_and_under_data_path(cfg):
    """Critical state (must back up) lives under juicefs/state/; the
    regenerable binary lives under juicefs/runtime/bin/.  Both under
    openhost_data_path."""
    state = archive_backend._juicefs_state_dir(cfg)
    runtime = archive_backend._juicefs_runtime_dir(cfg)
    base = str(cfg.openhost_data_path)
    assert state.startswith(base)
    assert runtime.startswith(base)
    assert state != runtime


def test_juicefs_meta_db_lives_in_state_dir(cfg):
    """meta.db is the single must-back-up file; it MUST be under state/."""
    assert archive_backend._juicefs_meta_db(cfg).startswith(archive_backend._juicefs_state_dir(cfg))
    assert archive_backend._juicefs_meta_db(cfg).endswith("meta.db")


def test_juicefs_meta_db_path_is_public_alias(cfg):
    assert archive_backend.juicefs_meta_db_path(cfg) == archive_backend._juicefs_meta_db(cfg)


def test_migrate_legacy_layout_renames_old_meta_db(cfg, tmp_path):
    """A pre-tidy zone has meta.db at openhost_data_path/juicefs-meta.db;
    it must be moved into juicefs/state/meta.db on first boot."""
    legacy = os.path.join(cfg.openhost_data_path, "juicefs-meta.db")
    Path(legacy).write_bytes(b"sqlite-bytes")
    archive_backend._migrate_legacy_layout(cfg)
    new = archive_backend._juicefs_meta_db(cfg)
    assert os.path.isfile(new)
    assert not os.path.isfile(legacy)


def test_migrate_legacy_layout_is_idempotent(cfg):
    """Calling twice must be a no-op."""
    archive_backend._migrate_legacy_layout(cfg)
    archive_backend._migrate_legacy_layout(cfg)


def test_migrate_legacy_layout_does_not_clobber_new_meta(cfg):
    """If both old and new paths exist, the migration must NOT overwrite
    the new path (loud-fail rather than silent-pick)."""
    legacy = os.path.join(cfg.openhost_data_path, "juicefs-meta.db")
    new = archive_backend._juicefs_meta_db(cfg)
    Path(legacy).write_bytes(b"old")
    os.makedirs(os.path.dirname(new), exist_ok=True)
    Path(new).write_bytes(b"new")
    archive_backend._migrate_legacy_layout(cfg)
    assert Path(new).read_bytes() == b"new"


# --- list_meta_dumps -------------------------------------------------------


def test_list_meta_dumps_summarises_dump_objects():
    """Summary returns count + most-recent timestamp.  Filters to
    dump-*.json.gz so unrelated objects under meta/ don't inflate count."""
    fake_now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(
            list_objects_v2=mock.MagicMock(
                return_value={
                    "Contents": [
                        {"Key": "p/meta/dump-1.json.gz", "LastModified": fake_now - dt.timedelta(hours=2)},
                        {"Key": "p/meta/dump-2.json.gz", "LastModified": fake_now},
                        {"Key": "p/meta/something-else.txt", "LastModified": fake_now},
                    ]
                }
            )
        ),
    ):
        summary = archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "p")
    assert summary is not None
    assert summary.count == 2
    assert summary.latest_key == "p/meta/dump-2.json.gz"


def test_list_meta_dumps_empty_bucket_returns_zero_count():
    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=mock.MagicMock(return_value={"Contents": []})),
    ):
        summary = archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "p")
    assert summary is not None
    assert summary.count == 0
    assert summary.latest_at is None


def test_list_meta_dumps_returns_none_on_s3_failure():
    """Any S3 failure surfaces as None so the dashboard renders 'unknown'
    rather than mistaking it for 'no dumps yet'."""
    with mock.patch.object(archive_backend, "_s3_client", side_effect=RuntimeError("boom")):
        assert archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "p") is None


def test_list_meta_dumps_handles_no_prefix():
    """Empty prefix lists at meta/ (no leading slash, no double /)."""
    captured = {}

    def fake_list(*, Bucket, Prefix, MaxKeys):
        captured["prefix"] = Prefix
        return {"Contents": []}

    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=fake_list),
    ):
        archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", None)
    assert captured["prefix"] == "meta/"


# --- is_archive_dir_healthy -----------------------------------------------


def test_is_archive_dir_healthy_disabled_returns_false(cfg, db):
    assert not archive_backend.is_archive_dir_healthy(cfg, db)


def test_is_archive_dir_healthy_s3_uses_is_mounted(cfg, db):
    """s3 health is determined by is_mounted(juicefs_mount_dir), NOT by
    os.path.isdir — an empty mount-point dir would silently let writes
    fall through to local disk and be shadowed when the mount reattaches."""
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    with mock.patch.object(archive_backend, "is_mounted", return_value=True) as m:
        assert archive_backend.is_archive_dir_healthy(cfg, db)
    assert m.call_args.args[0] == juicefs_mount_dir(cfg)


# --- attach_on_startup ----------------------------------------------------


def test_attach_on_startup_disabled_is_no_op(cfg, db):
    """Disabled backend means no juicefs work — boot must succeed without
    touching the install/mount path."""
    with mock.patch.object(archive_backend, "install_juicefs") as inst:
        with mock.patch.object(archive_backend, "mount") as mnt:
            new_cfg = archive_backend.attach_on_startup(cfg, db)
    inst.assert_not_called()
    mnt.assert_not_called()
    assert new_cfg.archive_dir_override is None


def test_attach_on_startup_s3_happy_path(cfg, db):
    """S3 backend installs juicefs (if missing), mounts, and clears the
    state_message."""
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='b', "
        "s3_access_key_id='ak', s3_secret_access_key='sk' WHERE id=1"
    )
    db.commit()
    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        with mock.patch.object(archive_backend, "mount") as mnt:
            new_cfg = archive_backend.attach_on_startup(cfg, db)
    mnt.assert_called_once()
    assert new_cfg.archive_dir_override == juicefs_mount_dir(cfg)
    state = read_state(db)
    assert state.state_message is None


def test_attach_on_startup_s3_missing_creds_records_error(cfg, db):
    """If creds are NULL, attach must record a state_message + not crash boot."""
    db.execute("UPDATE archive_backend SET backend='s3', s3_bucket='b' WHERE id=1")
    db.commit()
    new_cfg = archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state_message is not None
    assert "credentials" in state.state_message.lower()
    # The override is still set so reads of archive_dir don't silently fall
    # back to a different path; the operator sees the error and reconfigures.
    assert new_cfg.archive_dir_override == juicefs_mount_dir(cfg)


# --- configure_backend ----------------------------------------------------


def test_configure_backend_refuses_when_already_configured(cfg, db):
    """One-shot only: a subsequent configure call must refuse rather than
    overwriting the existing S3 setup."""
    db.execute("UPDATE archive_backend SET backend='s3', s3_bucket='b' WHERE id=1")
    db.commit()
    with pytest.raises(BackendConfigureError, match="already configured"):
        configure_backend(
            cfg,
            db,
            s3_bucket="b2",
            s3_region=None,
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )


def test_configure_backend_happy_path(cfg, db):
    """Format + mount + DB UPDATE all run in order; the row ends up with
    backend='s3' and the configured creds."""
    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        with mock.patch.object(archive_backend, "format_volume") as fmt:
            with mock.patch.object(archive_backend, "mount") as mnt:
                configure_backend(
                    cfg,
                    db,
                    s3_bucket="mybucket",
                    s3_region="us-west-2",
                    s3_endpoint=None,
                    s3_prefix="andrew-3",
                    s3_access_key_id="ak",
                    s3_secret_access_key="sk",
                )
    fmt.assert_called_once()
    mnt.assert_called_once()
    state = read_state(db)
    assert state.backend == "s3"
    assert state.s3_bucket == "mybucket"
    assert state.s3_prefix == "andrew-3"
    assert state.juicefs_volume_name == "andrew-3"
    assert state.configured_at is not None


def test_configure_backend_format_failure_does_not_persist(cfg, db):
    """If format/mount fail, the DB row stays at 'disabled' so the operator
    can retry without first having to undo a half-applied state."""
    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        with mock.patch.object(archive_backend, "format_volume", side_effect=RuntimeError("boom")):
            with pytest.raises(BackendConfigureError):
                configure_backend(
                    cfg,
                    db,
                    s3_bucket="b",
                    s3_region=None,
                    s3_endpoint=None,
                    s3_prefix=None,
                    s3_access_key_id="ak",
                    s3_secret_access_key="sk",
                )
    state = read_state(db)
    assert state.backend == "disabled"
