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

from compute_space.core import apps as apps_mod
from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendConfigureError
from compute_space.core.archive_backend import configure_backend
from compute_space.core.archive_backend import juicefs_mount_dir
from compute_space.core.archive_backend import read_state
from compute_space.db.connection import init_db
from compute_space.tests.conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path):
    return _make_test_config(tmp_path, port=20300)


@pytest.fixture
def db(cfg):
    """Initialised sqlite DB so the archive_backend table exists, seeded disabled."""
    init_db(cfg.db_path)
    conn = sqlite3.connect(cfg.db_path)
    yield conn
    conn.close()


# --- read_state ------------------------------------------------------------


def test_seeded_state_is_local(db):
    """Fresh DB comes up at backend='local': the archive tier is always
    available (local-disk backed); apps with app_archive=true install
    immediately, and the operator can upgrade to S3 later."""
    state = read_state(db)
    assert state.backend == "local"
    assert state.s3_bucket is None


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


def test_format_volume_creates_state_dir_for_meta_db(cfg):
    """JuiceFS's sqlite3 meta backend won't mkdir its parent; format_volume must.
    Regression: a fresh zone with no legacy meta.db otherwise fails with
    'unable to open database file: no such file or directory'."""
    state_dir = archive_backend.juicefs_state_dir(cfg)
    assert not os.path.exists(state_dir)
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        captured["state_dir_exists"] = os.path.isdir(state_dir)
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
    assert captured["state_dir_exists"] is True


def test_mount_writes_env_file_and_starts_service(cfg):
    """mount() must write the env file with the correct binary, meta DSN,
    mount dir, and S3 creds, then enable+start the systemd service."""
    systemctl_calls: list[tuple[str, ...]] = []

    def fake_systemctl(*args, timeout=30):
        systemctl_calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # is_mounted: False initially, True after systemctl start.
    is_mounted_calls = [False, True]
    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/usr/local/bin/juicefs"):
        with mock.patch.object(
            archive_backend, "is_mounted", side_effect=lambda mp: is_mounted_calls.pop(0) if is_mounted_calls else True
        ):
            with mock.patch.object(archive_backend, "_systemctl", side_effect=fake_systemctl):
                archive_backend.mount(cfg, "ak", "sk")

    # Verify env file was written with correct content.
    env_path = archive_backend._juicefs_env_file(cfg)
    assert os.path.isfile(env_path)
    content = open(env_path).read()
    assert "JUICEFS_BINARY=/usr/local/bin/juicefs" in content
    assert "ACCESS_KEY=ak" in content
    assert "SECRET_KEY=sk" in content
    assert f"JUICEFS_MOUNT_DIR={cfg.app_archive_dir}" in content
    # Mode must be 0600 (owner-only).
    assert oct(os.stat(env_path).st_mode & 0o777) == "0o600"

    # Verify systemctl calls: daemon-reload, then enable --now.
    assert ("daemon-reload",) in systemctl_calls
    assert ("enable", "--now", archive_backend.JUICEFS_SERVICE) in systemctl_calls


def test_mount_idempotent_when_already_mounted(cfg):
    """mount() must be a no-op if the mount is already active."""
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        with mock.patch.object(archive_backend, "_systemctl") as sctl:
            archive_backend.mount(cfg, "ak", "sk")
    sctl.assert_not_called()


# --- on-disk layout helpers ----------------------------------------------


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


def test_list_meta_dumps_lists_under_volume_name():
    """Regression: JuiceFS writes dumps to ``<volume>/meta/``, so the listing
    must use the volume name — not s3_prefix, which is null on most installs."""
    captured = {}

    def fake_list(*, Bucket, Prefix, MaxKeys):
        captured["prefix"] = Prefix
        return {"Contents": []}

    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=fake_list),
    ):
        archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "openhost")
    assert captured["prefix"] == "openhost/meta/"


def test_list_meta_dumps_handles_empty_volume_name():
    """Defensive: a blank volume name lists at meta/ (no leading slash, no double /)."""
    captured = {}

    def fake_list(*, Bucket, Prefix, MaxKeys):
        captured["prefix"] = Prefix
        return {"Contents": []}

    with mock.patch.object(
        archive_backend,
        "_s3_client",
        return_value=mock.MagicMock(list_objects_v2=fake_list),
    ):
        archive_backend.list_meta_dumps("b", "us-east-1", None, "ak", "sk", "")
    assert captured["prefix"] == "meta/"


# --- is_archive_dir_healthy -----------------------------------------------


def test_is_archive_dir_healthy_local_true_when_dir_exists(cfg, db):
    """On the local backend the health check passes once the local archive
    directory exists (created at boot / before provisioning)."""
    # Fresh DB defaults to 'local'.  The dir doesn't exist yet -> unhealthy.
    assert not archive_backend.is_archive_dir_healthy(cfg, db)
    archive_backend.ensure_local_archive_dir(cfg)
    assert archive_backend.is_archive_dir_healthy(cfg, db)


def test_is_archive_dir_healthy_disabled_returns_true(cfg, db):
    """The legacy 'disabled' state (no archive tier) always reports healthy
    so it never blocks app operations."""
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    assert archive_backend.is_archive_dir_healthy(cfg, db)


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
            archive_backend.attach_on_startup(cfg, db)
    inst.assert_not_called()
    mnt.assert_not_called()


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
            archive_backend.attach_on_startup(cfg, db)
    mnt.assert_called_once()
    state = read_state(db)
    assert state.state_message is None


def test_attach_on_startup_s3_missing_creds_records_error(cfg, db):
    """If creds are NULL, attach must record a state_message + not crash boot."""
    db.execute("UPDATE archive_backend SET backend='s3', s3_bucket='b' WHERE id=1")
    db.commit()
    archive_backend.attach_on_startup(cfg, db)
    state = read_state(db)
    assert state.state_message is not None
    assert "credentials" in state.state_message.lower()


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
    """If format/mount fail, the DB row stays at its pre-configure value so
    the operator can retry without first having to undo a half-applied
    state.  A fresh zone is on 'local', so it must remain 'local' (and its
    local archive data — none here — would be untouched)."""
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
    assert state.backend == "local"


# --- local backend + local->S3 migration ----------------------------------


def _write_local_archive_file(cfg, app: str, rel: str, content: bytes) -> str:
    """Helper: write a file into the local archive dir for ``app``."""
    root = archive_backend.local_archive_dir(cfg)
    path = os.path.join(root, app, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def test_effective_archive_dir_selects_by_backend(cfg, db):
    # default local
    assert archive_backend.effective_archive_dir(cfg, db) == archive_backend.local_archive_dir(cfg)
    # s3
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    assert archive_backend.effective_archive_dir(cfg, db) == archive_backend.juicefs_mount_dir(cfg)
    # legacy disabled -> juicefs mount path (absent, so 'not available')
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    assert archive_backend.effective_archive_dir(cfg, db) == archive_backend.juicefs_mount_dir(cfg)


def test_local_archive_has_data_and_apps(cfg):
    assert archive_backend.local_archive_has_data(cfg) is False
    assert archive_backend.local_archive_apps_with_data(cfg) == []
    # An empty per-app dir does not count as data.
    os.makedirs(os.path.join(archive_backend.local_archive_dir(cfg), "emptyapp"), exist_ok=True)
    assert archive_backend.local_archive_has_data(cfg) is False
    # A file inside a per-app dir does.
    _write_local_archive_file(cfg, "nextcloud", "files/a.txt", b"hello")
    assert archive_backend.local_archive_has_data(cfg) is True
    assert archive_backend.local_archive_apps_with_data(cfg) == ["nextcloud"]


def test_attach_on_startup_local_creates_dir(cfg, db):
    # Fresh DB defaults to local; the dir doesn't exist yet.
    assert not os.path.isdir(archive_backend.local_archive_dir(cfg))
    archive_backend.attach_on_startup(cfg, db)
    assert os.path.isdir(archive_backend.local_archive_dir(cfg))


def test_configure_backend_migrates_local_to_s3(cfg, db, tmp_path):
    """Positive migration case: local archive data is copied into the mount,
    the row flips to s3, and the local source is removed afterwards."""
    # Seed local archive data.
    _write_local_archive_file(cfg, "nextcloud", "files/doc.txt", b"important-bytes")
    _write_local_archive_file(cfg, "nextcloud", "files/sub/deep.bin", b"\x00\x01\x02\x03")

    # Fake the JuiceFS mount as a plain local dir the "mount" creates, and
    # make format/mount no-ops that just create the mount dir.
    mount_dir = archive_backend.juicefs_mount_dir(cfg)

    def fake_mount(config, ak, sk):
        os.makedirs(mount_dir, exist_ok=True)

    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "format_volume"),
        mock.patch.object(archive_backend, "mount", side_effect=fake_mount),
        mock.patch.object(archive_backend, "_podman_available", return_value=False),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_prefix=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )

    state = read_state(db)
    assert state.backend == "s3"
    # Data copied into the mount, byte-identical.
    with open(os.path.join(mount_dir, "nextcloud", "files", "doc.txt"), "rb") as f:
        assert f.read() == b"important-bytes"
    with open(os.path.join(mount_dir, "nextcloud", "files", "sub", "deep.bin"), "rb") as f:
        assert f.read() == b"\x00\x01\x02\x03"
    # Local source content removed after a successful migration (the empty
    # root dir may remain, but no app data should be left behind).
    local_root = archive_backend.local_archive_dir(cfg)
    leftover = []
    for _dirpath, _dirs, files in os.walk(local_root):
        leftover.extend(files)
    assert leftover == []


def test_configure_backend_migration_failopen_keeps_local(cfg, db):
    """Fail-open: if the migration/verify step fails, the backend stays
    'local' and the local data is left fully intact."""
    _write_local_archive_file(cfg, "nextcloud", "files/keepme.txt", b"do-not-lose-me")
    mount_dir = archive_backend.juicefs_mount_dir(cfg)

    def fake_mount(config, ak, sk):
        os.makedirs(mount_dir, exist_ok=True)

    # Force the copy to blow up mid-migration.
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "format_volume"),
        mock.patch.object(archive_backend, "mount", side_effect=fake_mount),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(
            archive_backend,
            "_migrate_local_archive_into_mount",
            side_effect=RuntimeError("copy exploded"),
        ),
    ):
        with pytest.raises(BackendConfigureError):
            configure_backend(
                cfg,
                db,
                s3_bucket="b",
                s3_region="us-east-1",
                s3_endpoint=None,
                s3_prefix=None,
                s3_access_key_id="ak",
                s3_secret_access_key="sk",
            )

    state = read_state(db)
    assert state.backend == "local"
    # Local data still there, untouched.
    p = os.path.join(archive_backend.local_archive_dir(cfg), "nextcloud", "files", "keepme.txt")
    assert os.path.isfile(p)
    with open(p, "rb") as f:
        assert f.read() == b"do-not-lose-me"


def test_migrate_verify_detects_short_copy(cfg):
    """The verification step raises if a destination file is truncated,
    so a silent short copy can't be mistaken for success.  We simulate the
    short write by letting the copy run, then truncating the destination
    before verification (patch copytree/copy2 to a no-op after we've
    pre-seeded a truncated dest)."""
    _write_local_archive_file(cfg, "nextcloud", "files/big.bin", b"x" * 1000)
    mount_dir = archive_backend.juicefs_mount_dir(cfg)
    # Pre-create the destination with a TRUNCATED copy of the file.
    dst_file = os.path.join(mount_dir, "nextcloud", "files", "big.bin")
    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
    with open(dst_file, "wb") as f:
        f.write(b"x" * 10)  # short!

    # Make the copy phase a no-op so our truncated dest survives into verify.
    with (
        mock.patch("shutil.copytree"),
        mock.patch("shutil.copy2"),
    ):
        with pytest.raises(RuntimeError, match="verification failed"):
            archive_backend._copy_and_verify_in_process(archive_backend.local_archive_dir(cfg), mount_dir)
    # Source intact regardless.
    src = os.path.join(archive_backend.local_archive_dir(cfg), "nextcloud", "files", "big.bin")
    assert os.path.getsize(src) == 1000


# --- storage_summary -------------------------------------------------------


_ARCHIVE_MANIFEST = 'name="x"\n[data]\napp_data=true\napp_archive=true\n'
_PLAIN_MANIFEST = 'name="x"\n[data]\napp_data=true\n'


def test_storage_summary_local_backend_warns(cfg, db):
    s = archive_backend.storage_summary(_ARCHIVE_MANIFEST, db)
    assert s.uses_archive is True
    assert s.requires_archive is True
    assert s.archive_backend == "local"
    assert s.archive_is_durable is False
    assert len(s.warnings) == 1
    assert "LOCAL" in s.warnings[0]


def test_storage_summary_s3_backend_no_warn(cfg, db):
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    s = archive_backend.storage_summary(_ARCHIVE_MANIFEST, db)
    assert s.archive_backend == "s3"
    assert s.archive_is_durable is True
    assert s.warnings == []


def test_storage_summary_non_archive_app_no_warn(cfg, db):
    s = archive_backend.storage_summary(_PLAIN_MANIFEST, db)
    assert s.uses_archive is False
    assert s.warnings == []


def test_migrate_preserves_ownership(cfg, monkeypatch):
    """The in-process migration copy must reproduce source uid/gid on the
    destination, so migrated files stay writable by their owning app
    container.  We can't really chown as an unprivileged test user, so we
    capture the os.chown calls and assert the dest paths are chowned to the
    source's uid/gid."""
    _write_local_archive_file(cfg, "file-browser", "e2e-test-file.txt", b"hello")
    src_root = archive_backend.local_archive_dir(cfg)
    dst_root = os.path.join(cfg.data_root_dir, "app_archive")
    os.makedirs(dst_root, exist_ok=True)

    src_file = os.path.join(src_root, "file-browser", "e2e-test-file.txt")
    src_st = os.lstat(src_file)

    chowned: dict[str, tuple[int, int]] = {}
    real_chown = os.chown

    def fake_chown(path, uid, gid, *a, **k):
        chowned[os.path.realpath(path)] = (uid, gid)
        # don't actually chown (unprivileged); record only.

    monkeypatch.setattr(os, "chown", fake_chown)
    archive_backend._copy_and_verify_in_process(src_root, dst_root)
    monkeypatch.setattr(os, "chown", real_chown)

    dst_file = os.path.realpath(os.path.join(dst_root, "file-browser", "e2e-test-file.txt"))
    assert dst_file in chowned, f"dest file was not chowned: {list(chowned)}"
    assert chowned[dst_file] == (src_st.st_uid, src_st.st_gid)


def test_restart_archive_apps_only_recycles_running_archive_apps(cfg, db):
    """After a migration, restart_archive_apps must recycle exactly the
    RUNNING apps whose manifest uses the archive tier — not stopped apps,
    not non-archive apps."""
    db.row_factory = sqlite3.Row  # restart_archive_apps reads rows by column name

    def _seed(app_id, name, status, manifest_raw, port):
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw) "
            "VALUES (?, ?, '1.0', ?, ?, ?, ?)",
            (app_id, name, f"/tmp/{name}", port, status, manifest_raw),
        )
        db.commit()

    arch = 'name="a"\n[data]\napp_archive=true\n'
    plain = 'name="b"\n[data]\napp_data=true\n'
    _seed("id_arch_run", "arch-run", "running", arch, 20401)
    _seed("id_arch_stop", "arch-stop", "stopped", arch, 20402)
    _seed("id_plain_run", "plain-run", "running", plain, 20403)

    with (
        mock.patch.object(apps_mod, "stop_app_process") as stop,
        mock.patch.object(apps_mod, "start_app_process") as start,
    ):
        restarted = apps_mod.restart_archive_apps(db, cfg)

    assert restarted == ["arch-run"]
    assert stop.call_count == 1
    assert start.call_count == 1
    # started the right app_id
    assert start.call_args.args[0] == "id_arch_run"
