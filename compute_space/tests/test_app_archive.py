"""Tests for the ``app_archive`` storage tier.

The archive tier is a per-app bind mount whose host backing is
operator-selected: defaults to local disk under ``persistent_data_dir``
and can be overridden to point at a JuiceFS mount (or any other
host-mounted POSIX filesystem).  Apps see the same in-container path
either way.

These tests cover the manifest opt-in plumbing that flows from
``provision_data`` through ``deprovision_data``, plus the
``Config.app_archive_dir`` resolution rules.
"""

from __future__ import annotations

import pytest

from compute_space.config import DefaultConfig
from compute_space.core.data import deprovision_data, provision_data
from compute_space.core.manifest import AppManifest


def _manifest(**kwargs) -> AppManifest:  # type: ignore[no-untyped-def]
    """Build a minimally-populated AppManifest for tests.

    Mirrors the helper in test_containers.py so the two test files
    construct equivalent fixtures and a regression that diverges the
    two surfaces is easier to spot.
    """
    base = dict(
        name="archiveapp",
        version="0.1.0",
        container_image="Dockerfile",
        container_port=8080,
        memory_mb=128,
        cpu_millicores=100,
    )
    base.update(kwargs)
    return AppManifest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# provision_data
# ---------------------------------------------------------------------------


def test_provision_data_creates_archive_subdir_when_opted_in(tmp_path) -> None:
    """``app_archive=True`` should produce a per-app subdir under the
    operator-configured archive root and stamp
    ``OPENHOST_APP_ARCHIVE_DIR`` on the env-vars dict.
    """
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(app_data=True, app_archive=True)
    env = provision_data(
        manifest.name,
        manifest,
        str(data_dir),
        str(temp_dir),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
    )

    expected = archive_dir / manifest.name
    assert expected.is_dir()
    assert env["OPENHOST_APP_ARCHIVE_DIR"] == str(expected)


def test_provision_data_skips_archive_when_not_opted_in(tmp_path) -> None:
    """An app that doesn't ask for ``app_archive`` must NOT get the
    env var or a subdir created — apps that don't opt in shouldn't
    surface operator-configured backings to themselves."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(app_data=True)  # no app_archive
    env = provision_data(
        manifest.name,
        manifest,
        str(data_dir),
        str(temp_dir),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
    )

    assert "OPENHOST_APP_ARCHIVE_DIR" not in env
    assert not (archive_dir / manifest.name).exists()


def test_provision_data_archive_subdir_under_access_all_data(tmp_path) -> None:
    """``access_all_data`` is the catch-all that grants every tier.
    Even though the archive bind under access_all_data is the parent
    directory (handled in run_container), provision_data must still
    create the per-app subdir so its files have a stable location
    inside the archive namespace; the parent mount in the container
    just exposes that subdir under its existing path.
    """
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(access_all_data=True)
    env = provision_data(
        manifest.name,
        manifest,
        str(data_dir),
        str(temp_dir),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
    )

    # access_all_data implies app_archive coverage; the per-app
    # subdir must exist (env var stamped to the in-container path).
    assert (archive_dir / manifest.name).is_dir()
    assert env["OPENHOST_APP_ARCHIVE_DIR"] == str(archive_dir / manifest.name)


def test_provision_data_refuses_when_archive_dir_missing(tmp_path) -> None:
    """When the configured archive root doesn't exist as a directory
    (e.g. an operator-overridden JuiceFS mount that isn't attached
    yet), provisioning must fail loudly rather than silently
    creating a local-disk ghost path that gets shadowed when the
    mount eventually attaches.

    This is the load-bearing invariant on the operator-side
    JuiceFS-mount-failure path: systemd ordering makes openhost-
    core boot-fail when the mount fails, but if the operator
    bypasses that ordering somehow, this guard turns the failure
    into a clean error message instead of silent data loss.
    """
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "doesnt-exist"  # NOT created
    data_dir.mkdir()
    temp_dir.mkdir()

    manifest = _manifest(app_data=True, app_archive=True)
    with pytest.raises(RuntimeError, match="archive_dir"):
        provision_data(
            manifest.name,
            manifest,
            str(data_dir),
            str(temp_dir),
            str(archive_dir),
            my_openhost_redirect_domain="my.example.com",
            zone_domain="example.com",
            port=8080,
        )


def test_provision_data_refuses_when_archive_dir_missing_via_access_all_data(tmp_path) -> None:
    """The same guard must fire for ``access_all_data`` apps too —
    they use the archive tier even though they don't set
    ``app_archive=True`` directly.  Without this branch coverage a
    refactor that splits the two could regress the guard for one
    flag and leave the other silently broken.
    """
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "doesnt-exist"  # NOT created
    data_dir.mkdir()
    temp_dir.mkdir()

    manifest = _manifest(access_all_data=True)
    with pytest.raises(RuntimeError, match="archive_dir"):
        provision_data(
            manifest.name,
            manifest,
            str(data_dir),
            str(temp_dir),
            str(archive_dir),
            my_openhost_redirect_domain="my.example.com",
            zone_domain="example.com",
            port=8080,
        )


def test_provision_data_does_not_fail_when_archive_dir_missing_and_no_archive_opt_in(
    tmp_path,
) -> None:
    """Apps that don't ask for app_archive must NOT fail just
    because the configured archive_dir doesn't exist.  The whole
    point of the opt-in is that operators with no JuiceFS (and
    therefore potentially with a fallback path that hasn't been
    populated by Config.make_all_dirs yet) can still deploy
    archive-free apps."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "doesnt-exist"  # NOT created
    data_dir.mkdir()
    temp_dir.mkdir()

    manifest = _manifest(app_data=True)  # no app_archive
    # Must not raise.
    provision_data(
        manifest.name,
        manifest,
        str(data_dir),
        str(temp_dir),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
    )


def test_provision_data_archive_idempotent_on_redeploy(tmp_path) -> None:
    """Calling provision_data twice (the redeploy path) must succeed
    without complaining about the archive subdir already existing,
    and must preserve any data the first deploy wrote.

    Same idempotency contract as app_data and app_temp_data — apps
    that survive a deploy + restart cycle expect their disks intact.
    """
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    manifest = _manifest(app_data=True, app_archive=True)
    provision_data(
        manifest.name,
        manifest,
        str(data_dir),
        str(temp_dir),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
    )
    # Drop a marker file under the archive subdir.
    marker = archive_dir / manifest.name / "marker.txt"
    marker.write_text("hello")

    # Second provision must not raise nor wipe the marker.
    provision_data(
        manifest.name,
        manifest,
        str(data_dir),
        str(temp_dir),
        str(archive_dir),
        my_openhost_redirect_domain="my.example.com",
        zone_domain="example.com",
        port=8080,
    )
    assert marker.read_text() == "hello"


# ---------------------------------------------------------------------------
# deprovision_data
# ---------------------------------------------------------------------------


def test_deprovision_data_removes_archive_subdir(tmp_path) -> None:
    """A non-keep_data uninstall must remove the app's archive
    subdirectory along with its app_data and temp dirs.  Without
    this, a subsequent reinstall of the same app would inherit
    stale archive contents."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    # Pre-populate everything as if an app had run.
    app_data_dir = data_dir / "app_data" / "myapp"
    app_temp_dir = temp_dir / "app_temp_data" / "myapp"
    app_archive_dir = archive_dir / "myapp"
    for d in (app_data_dir, app_temp_dir, app_archive_dir):
        d.mkdir(parents=True)
        (d / "marker.txt").write_text("hi")

    deprovision_data("myapp", str(data_dir), str(temp_dir), str(archive_dir))

    assert not app_data_dir.exists()
    assert not app_temp_dir.exists()
    assert not app_archive_dir.exists()


def test_deprovision_data_handles_missing_archive_subdir(tmp_path) -> None:
    """A best-effort deprovision must not raise when the archive
    subdir doesn't exist (e.g. the app never opted in to
    app_archive, or the operator wiped the dir manually).
    Failure to handle this gracefully would block uninstall of
    every non-archive app on an instance with a misconfigured
    archive_dir."""
    data_dir = tmp_path / "persistent"
    temp_dir = tmp_path / "temp"
    archive_dir = tmp_path / "archive"
    for d in (data_dir, temp_dir, archive_dir):
        d.mkdir()

    # No app_archive subdir exists — deprovision must still succeed.
    deprovision_data("myapp", str(data_dir), str(temp_dir), str(archive_dir))


# ---------------------------------------------------------------------------
# Config.app_archive_dir
# ---------------------------------------------------------------------------


def _config(**kwargs) -> DefaultConfig:  # type: ignore[no-untyped-def]
    base = dict(
        zone_domain="example.com",
        my_openhost_redirect_domain="my.example.com",
    )
    base.update(kwargs)
    return DefaultConfig(**base)  # type: ignore[arg-type]


def test_config_archive_dir_defaults_to_local_subdir() -> None:
    """When ``archive_dir_override`` is unset, the archive lives
    on local disk under ``persistent_data_dir``.  Apps still get
    the bind-mount; they just don't get the elastic-S3 backing.
    This is the path operators take when they haven't configured
    JuiceFS yet — apps that opt into app_archive must still
    deploy."""
    cfg = _config(data_root_dir="/opt/openhost")
    assert cfg.archive_dir_override is None
    expected = "/opt/openhost/persistent_data/app_archive"
    assert cfg.app_archive_dir == expected


def test_config_archive_dir_uses_override_when_set() -> None:
    """When the operator points ``archive_dir_override`` at a
    JuiceFS mount (or any other path), the archive tier resolves
    to that path.  This is the JuiceFS-on-S3 path."""
    cfg = _config(
        data_root_dir="/opt/openhost",
        archive_dir_override="/var/lib/openhost/juicefs/mount/app_archive",
    )
    assert cfg.app_archive_dir == "/var/lib/openhost/juicefs/mount/app_archive"


def test_config_make_all_dirs_creates_local_archive_dir(tmp_path) -> None:
    """When the archive backing is local (no override), make_all_dirs
    must create it.  Without this, the first provision_data on a
    fresh instance would race against the mkdir."""
    cfg = _config(data_root_dir=str(tmp_path))
    cfg.make_all_dirs()
    assert (tmp_path / "persistent_data" / "app_archive").is_dir()


def test_config_make_all_dirs_does_not_create_overridden_archive_dir(
    tmp_path,
) -> None:
    """When ``archive_dir_override`` points at an external mount
    (e.g. JuiceFS), the operator-side ansible role is responsible
    for creating it.  Trying to mkdir an external mount path that
    isn't yet mounted would either fail or — worse — succeed by
    creating a local-disk path that shadows the mount when it
    eventually attaches.  Don't.
    """
    override = tmp_path / "external" / "archive"
    # Note: do NOT mkdir(override) — we're asserting that
    # make_all_dirs DOESN'T create it.
    cfg = _config(
        data_root_dir=str(tmp_path),
        archive_dir_override=str(override),
    )
    cfg.make_all_dirs()
    assert not override.exists()
