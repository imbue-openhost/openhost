"""Edge-case coverage for the always-JuiceFS archive backend rework.

These complement ``test_archive_backend.py`` / ``test_api_archive_backend.py``
with a broad sweep of boundary conditions found while hardening the local
file-backed backend and the ``juicefs sync`` + ``juicefs config`` migration on
live instances (HTTP endpoints, credential handling, fail-open, URL shapes,
volume-name preservation, ordering, and the app quiesce/resume dance).
"""

from __future__ import annotations

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
    return _make_test_config(tmp_path, port=20500)


@pytest.fixture
def db(cfg):
    init_db(cfg.db_path)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ── 1-8: _endpoint_is_insecure_http boundary matrix ───────────────────────


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("http://localhost:9106", True),
        ("http://minio.internal", True),
        ("HTTP://UPPER.example", True),
        ("  http://ws.example  ", True),  # whitespace-trimmed
        ("https://minio.example.com", False),
        ("https://s3.amazonaws.com", False),
        (None, False),
        ("", False),
    ],
)
def test_endpoint_is_insecure_http_matrix(endpoint, expected):
    assert archive_backend._endpoint_is_insecure_http(endpoint) is expected


# ── _bucket_url / sync-url boundary shapes not covered elsewhere ──────────
# (The base suite covers the happy AWS/custom cases; these add the HTTP-scheme
#  and multi-slash boundaries that the migration URL construction depends on.)


def test_bucket_url_custom_http_endpoint_keeps_scheme():
    assert archive_backend._bucket_url("b", "x", "http://localhost:9106") == "http://localhost:9106/b"


def test_bucket_url_multiple_trailing_slashes():
    assert archive_backend._bucket_url("b", "x", "https://m.example///") == "https://m.example/b"


def test_s3_sync_dest_https_custom_endpoint():
    assert archive_backend._s3_sync_dest("b", None, "https://minio.x:9000", "vol") == "s3://b.minio.x:9000/vol/"


def test_s3_sync_dest_blank_region_defaults():
    assert archive_backend._s3_sync_dest("b", "", None, "vol") == "s3://b.s3.us-east-1.amazonaws.com/vol/"


def test_file_bucket_ends_with_single_slash(cfg):
    assert archive_backend._file_bucket("/a/b") == "/a/b/"
    assert archive_backend._file_bucket("/a/b/") == "/a/b/"


# ── read_state / defaults (missing-row + s3 field round-trip) ─────────────


def test_read_state_missing_row_defaults_local(db):
    db.execute("DELETE FROM archive_backend")
    db.commit()
    st = read_state(db)
    assert st.backend == "local"
    assert st.juicefs_volume_name == "openhost"


def test_read_state_volume_name_default_on_missing_row(db):
    # The schema enforces NOT NULL on juicefs_volume_name, so the only way to
    # exercise the default is the missing-row fallback path.
    db.execute("DELETE FROM archive_backend")
    db.commit()
    assert read_state(db).juicefs_volume_name == "openhost"


def test_read_state_preserves_s3_fields(db):
    db.execute(
        "UPDATE archive_backend SET backend='s3', s3_bucket='bkt', s3_region='r', "
        "s3_endpoint='http://e', s3_prefix='p', s3_access_key_id='ak', s3_secret_access_key='sk' WHERE id=1"
    )
    db.commit()
    st = read_state(db)
    assert (st.backend, st.s3_bucket, st.s3_region, st.s3_endpoint, st.s3_prefix) == (
        "s3",
        "bkt",
        "r",
        "http://e",
        "p",
    )


# ── is_archive_dir_healthy across backends & mount states (matrix) ────────


@pytest.mark.parametrize("backend", ["local", "s3"])
@pytest.mark.parametrize("mounted", [True, False])
def test_health_follows_mount_for_local_and_s3(db, cfg, backend, mounted):
    db.execute("UPDATE archive_backend SET backend=? WHERE id=1", (backend,))
    db.commit()
    with mock.patch.object(archive_backend, "is_mounted", return_value=mounted):
        assert archive_backend.is_archive_dir_healthy(cfg, db) is mounted


def test_health_disabled_always_true(db, cfg):
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    with mock.patch.object(archive_backend, "is_mounted", return_value=False):
        assert archive_backend.is_archive_dir_healthy(cfg, db) is True


def test_health_checks_the_mountpoint_path(db, cfg):
    with mock.patch.object(archive_backend, "is_mounted", return_value=True) as m:
        archive_backend.is_archive_dir_healthy(cfg, db)
    assert m.call_args.args[0] == juicefs_mount_dir(cfg)


# ── 33-38: manifest predicates ────────────────────────────────────────────


@pytest.mark.parametrize(
    "toml,requires,uses",
    [
        ("[data]\napp_archive=true\n", True, True),
        ("[data]\napp_archive=false\n", False, False),
        ("[data]\naccess_all_archive=true\n", False, True),
        ("[data]\naccess_all_data=true\n", False, True),
        ("[data]\naccess_all_app_data=true\n", False, False),
        ("", False, False),
    ],
)
def test_manifest_predicates(toml, requires, uses):
    assert archive_backend.manifest_requires_archive(toml) is requires
    assert archive_backend.manifest_uses_archive(toml) is uses


def test_manifest_predicates_tolerate_bad_toml():
    assert archive_backend.manifest_requires_archive("this is not = valid toml [[[") is False
    assert archive_backend.manifest_uses_archive("this is not = valid toml [[[") is False


# ── 39-44: local_archive_apps_with_data ───────────────────────────────────


def test_local_apps_empty_when_not_mounted(db, cfg):
    with mock.patch.object(archive_backend, "is_mounted", return_value=False):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == []


def test_local_apps_empty_when_backend_s3(db, cfg):
    db.execute("UPDATE archive_backend SET backend='s3' WHERE id=1")
    db.commit()
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == []


def test_local_apps_skips_empty_per_app_dirs(db, cfg):
    mp = juicefs_mount_dir(cfg)
    os.makedirs(os.path.join(mp, "emptyapp"), exist_ok=True)
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == []


def test_local_apps_lists_only_apps_with_content(db, cfg):
    mp = juicefs_mount_dir(cfg)
    for app in ("alpha", "beta"):
        os.makedirs(os.path.join(mp, app), exist_ok=True)
    with open(os.path.join(mp, "beta", "f"), "wb") as f:
        f.write(b"x")
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == ["beta"]


def test_local_apps_sorted(db, cfg):
    mp = juicefs_mount_dir(cfg)
    for app in ("zeta", "alpha", "mid"):
        os.makedirs(os.path.join(mp, app), exist_ok=True)
        with open(os.path.join(mp, app, "f"), "wb") as f:
            f.write(b"x")
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == ["alpha", "mid", "zeta"]


def test_local_apps_ignores_regular_files_at_root(db, cfg):
    mp = juicefs_mount_dir(cfg)
    os.makedirs(mp, exist_ok=True)
    with open(os.path.join(mp, ".stats"), "wb") as f:  # juicefs control file
        f.write(b"")
    with mock.patch.object(archive_backend, "is_mounted", return_value=True):
        assert archive_backend.local_archive_apps_with_data(cfg, db) == []


# ── 45-50: configure_backend guards & migration wiring ────────────────────


def test_configure_refuses_unknown_backend(db, cfg):
    # The schema CHECK constraint blocks a bad backend value in the DB, so the
    # defensive guard is reached via a crafted read_state (belt-and-braces if
    # a future migration ever loosens the constraint).
    bad = archive_backend.BackendState(
        backend="weird",
        s3_bucket=None,
        s3_region=None,
        s3_endpoint=None,
        s3_prefix=None,
        s3_access_key_id=None,
        s3_secret_access_key=None,
        juicefs_volume_name="openhost",
        configured_at=None,
        state_message=None,
    )
    with mock.patch.object(archive_backend, "read_state", return_value=bad):
        with pytest.raises(BackendConfigureError, match="cannot configure"):
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


def test_configure_from_local_preserves_existing_volume_name(db, cfg):
    db.execute("UPDATE archive_backend SET juicefs_volume_name='pre-existing' WHERE id=1")
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "_ensure_local_volume_formatted"),
        mock.patch.object(archive_backend, "mount"),
        mock.patch.object(archive_backend, "umount"),
        mock.patch.object(archive_backend, "_migrate_local_to_s3"),
        mock.patch.object(archive_backend, "_remove_local_object_store"),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region=None,
            s3_endpoint=None,
            s3_prefix="ignored",
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    # The already-formatted volume name wins over the prefix.
    assert read_state(db).juicefs_volume_name == "pre-existing"


def test_configure_from_disabled_uses_prefix_as_volume(db, cfg):
    db.execute("UPDATE archive_backend SET backend='disabled' WHERE id=1")
    db.commit()
    with (
        mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True),
        mock.patch.object(archive_backend, "format_s3_volume"),
        mock.patch.object(archive_backend, "mount"),
    ):
        configure_backend(
            cfg,
            db,
            s3_bucket="b",
            s3_region=None,
            s3_endpoint=None,
            s3_prefix="zoneprefix",
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    assert read_state(db).juicefs_volume_name == "zoneprefix"


def test_migrate_passes_insecure_for_http_endpoint(cfg):
    seen = {}

    def fake_sync(config, *, src, dst, s3_access_key_id, s3_secret_access_key, insecure):
        seen["insecure"] = insecure

    with (
        mock.patch.object(archive_backend, "_sync_objects", side_effect=fake_sync),
        mock.patch.object(archive_backend, "_reconfigure_volume_storage"),
    ):
        archive_backend._migrate_local_to_s3(
            cfg,
            volume="v",
            s3_bucket="b",
            s3_region=None,
            s3_endpoint="http://localhost:9106",
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    assert seen["insecure"] is True


def test_migrate_passes_secure_for_https_endpoint(cfg):
    seen = {}

    def fake_sync(config, *, src, dst, s3_access_key_id, s3_secret_access_key, insecure):
        seen["insecure"] = insecure

    with (
        mock.patch.object(archive_backend, "_sync_objects", side_effect=fake_sync),
        mock.patch.object(archive_backend, "_reconfigure_volume_storage"),
    ):
        archive_backend._migrate_local_to_s3(
            cfg,
            volume="v",
            s3_bucket="b",
            s3_region="us-east-1",
            s3_endpoint=None,
            s3_access_key_id="ak",
            s3_secret_access_key="sk",
        )
    assert seen["insecure"] is False


# ── 51-56: reconfigure / sync command construction ────────────────────────


def test_reconfigure_to_file_omits_creds(cfg):
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend._reconfigure_volume_storage(
                cfg, storage="file", bucket="/local/store/", s3_access_key_id=None, s3_secret_access_key=None
            )
    assert "--storage" in captured["cmd"] and "file" in captured["cmd"]
    assert "--access-key" not in captured["cmd"]
    assert "--secret-key" not in captured["cmd"]


def test_reconfigure_raises_on_failure(cfg):
    def fake_run(cmd, env, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="re-point"):
                archive_backend._reconfigure_volume_storage(
                    cfg, storage="s3", bucket="https://b", s3_access_key_id="ak", s3_secret_access_key="sk"
                )


def test_sync_raises_on_failure(cfg):
    def fake_run(cmd, env, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="list failed")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="sync failed"):
                archive_backend._sync_objects(
                    cfg, src="/s/", dst="s3://b/v/", s3_access_key_id="ak", s3_secret_access_key="sk"
                )


def test_sync_no_https_absent_by_default(cfg):
    captured = {}

    def fake_run(cmd, env, capture_output, text, timeout):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            archive_backend._sync_objects(
                cfg, src="/s/", dst="s3://b/v/", s3_access_key_id="ak", s3_secret_access_key="sk"
            )
    assert "--no-https" not in captured["cmd"]


def test_format_local_volume_raises_on_failure(cfg):
    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="format err")

    with mock.patch.object(archive_backend, "is_juicefs_installed", return_value=True):
        with mock.patch.object(archive_backend, "_juicefs_binary", return_value="/jfs"):
            with mock.patch.object(subprocess, "run", side_effect=fake_run):
                with pytest.raises(RuntimeError, match="format"):
                    archive_backend.format_local_volume(cfg, "vol")


def test_ensure_local_volume_formatted_skips_when_meta_exists(cfg):
    os.makedirs(archive_backend.juicefs_state_dir(cfg), exist_ok=True)
    Path(archive_backend.juicefs_meta_db_path(cfg)).write_bytes(b"")
    with mock.patch.object(archive_backend, "format_local_volume") as fmt:
        archive_backend._ensure_local_volume_formatted(cfg, "vol")
    fmt.assert_not_called()


# ── 57-60: quiesce/resume helpers ─────────────────────────────────────────


def test_stop_running_archive_apps_selects_running_archive_only(db, cfg):
    for aid, name, status, toml, port in [
        ("r1", "arch-run", "running", "[data]\napp_archive=true\n", 20510),
        ("r2", "arch-stop", "stopped", "[data]\napp_archive=true\n", 20511),
        ("r3", "plain-run", "running", "[data]\napp_data=true\n", 20512),
        ("r4", "aaa-run", "running", "[data]\naccess_all_archive=true\n", 20513),
    ]:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status, manifest_raw, container_id) "
            "VALUES (?, ?, '1', ?, ?, ?, ?, ?)",
            (aid, name, f"/tmp/{name}", port, status, toml, f"ctr-{aid}"),
        )
    db.commit()

    # Quiescence is verified against real container state; stopped -> gone.
    with (
        mock.patch.object(apps_mod, "stop_app_process"),
        mock.patch.object(apps_mod, "is_container_running", return_value=False),
    ):
        stopped = apps_mod.stop_running_archive_apps(db, cfg)
    # running archive apps: arch-run (app_archive) and aaa-run (access_all_archive)
    assert set(stopped) == {"r1", "r4"}


def test_stop_running_archive_apps_none_when_empty(db, cfg):
    with mock.patch.object(apps_mod, "stop_app_process"):
        assert apps_mod.stop_running_archive_apps(db, cfg) == []


def test_start_apps_by_id_empty_noop(db, cfg):
    with mock.patch.object(apps_mod, "start_app_process") as start:
        apps_mod.start_apps_by_id([], db, cfg)
    start.assert_not_called()


def test_remove_local_object_store_noop_when_absent(cfg):
    # Path doesn't exist yet -> no error, no recreation.
    assert not os.path.isdir(archive_backend.local_object_store_dir(cfg))
    archive_backend._remove_local_object_store(cfg)
    assert not os.path.isdir(archive_backend.local_object_store_dir(cfg))
