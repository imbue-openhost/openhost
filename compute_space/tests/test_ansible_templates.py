"""Render-time tests for the ansible jinja2 templates.

These templates are otherwise tested only by running ansible against a
real VM, which is too slow to do per-commit.  Here we render them with
jinja2 directly and assert on the contents — covering the JuiceFS
toggle, the conditional ``Requires=juicefs-mount.service``, and the
bind-mount path the systemd unit hands to ``juicefs mount``.

These tests intentionally skip ansible-isms that aren't valid jinja2
(filters like ``ansible_architecture``) by feeding the templates the
exact variables they expect.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined


def _ansible_bool(value: object) -> bool:
    """Mimic ansible's ``| bool`` filter for our render-time tests.

    Ansible's ``bool`` accepts python bools, the literal strings
    ``"true"``/``"false"``/``"yes"``/``"no"``/``"1"``/``"0"`` (case-
    insensitive), and integers.  We only need the cases the templates
    actually pass through, so anything else falls back to Python's
    ``bool()`` for predictability.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "y", "1", "on"}
    return bool(value)


@pytest.fixture(scope="module")
def templates_env() -> Environment:
    """Jinja2 env pointed at the ansible templates dir.

    StrictUndefined turns any reference to an unset variable into a
    test failure, so we catch typos in the templates rather than
    silently rendering them as empty strings.

    Ansible filters that aren't builtin to jinja2 are shimmed in so
    the templates can render outside of ansible for these tests.
    """
    here = Path(__file__).resolve()
    # compute_space/tests/test_ansible_templates.py -> openhost-core/ansible/templates
    repo_root = here.parents[2]
    templates_dir = repo_root / "ansible" / "templates"
    assert templates_dir.is_dir(), templates_dir
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    env.filters["bool"] = _ansible_bool
    return env


# ---------------------------------------------------------------------------
# config.toml.j2 — archive_dir_override only when juicefs_enabled
# ---------------------------------------------------------------------------


def _render_config_toml(env: Environment, **vars_: object) -> str:
    return env.get_template("config.toml.j2").render(**vars_)


def test_config_toml_no_archive_override_by_default(templates_env: Environment) -> None:
    """JuiceFS off (the default) must not emit ``archive_dir_override``.

    Existing zones reload their config.toml on every deploy; a stray
    override line would shift their archive root unintentionally.
    """
    rendered = _render_config_toml(
        templates_env,
        domain="example.selfhost.imbue.com",
        public_ip="1.2.3.4",
        juicefs_enabled=False,
    )
    assert "archive_dir_override" not in rendered, rendered


def test_config_toml_no_archive_override_when_juicefs_unset(templates_env: Environment) -> None:
    """``juicefs_enabled`` is supplied via -e at deploy time and is
    absent on existing operator workflows that haven't opted in.  The
    template must default to off, not error out on the missing var.
    """
    rendered = _render_config_toml(
        templates_env,
        domain="example.selfhost.imbue.com",
        public_ip="1.2.3.4",
        # juicefs_enabled deliberately omitted
    )
    assert "archive_dir_override" not in rendered, rendered


def test_config_toml_archive_override_when_juicefs_enabled(templates_env: Environment) -> None:
    rendered = _render_config_toml(
        templates_env,
        domain="example.selfhost.imbue.com",
        public_ip="1.2.3.4",
        juicefs_enabled=True,
    )
    assert 'archive_dir_override = "/data/app_archive_juicefs"' in rendered, rendered


def test_config_toml_archive_override_with_string_true(templates_env: Environment) -> None:
    """Ansible's ``-e juicefs_enabled=true`` arrives as the literal
    string ``"true"`` (not a bool).  The template's ``| bool`` filter
    must coerce it correctly so ``-e juicefs_enabled=true`` actually
    enables the feature.
    """
    rendered = _render_config_toml(
        templates_env,
        domain="example.selfhost.imbue.com",
        public_ip="1.2.3.4",
        juicefs_enabled="true",
    )
    assert "archive_dir_override" in rendered, rendered


def test_config_toml_archive_override_off_with_string_false(templates_env: Environment) -> None:
    """Inverse of the above: literal ``"false"`` must NOT enable."""
    rendered = _render_config_toml(
        templates_env,
        domain="example.selfhost.imbue.com",
        public_ip="1.2.3.4",
        juicefs_enabled="false",
    )
    assert "archive_dir_override" not in rendered, rendered


# ---------------------------------------------------------------------------
# openhost.service.j2 — Requires=juicefs-mount.service when enabled
# ---------------------------------------------------------------------------


def _render_openhost_service(env: Environment, **vars_: object) -> str:
    return env.get_template("openhost.service.j2").render(host_uid="1001", **vars_)


def test_openhost_service_no_juicefs_dep_by_default(templates_env: Environment) -> None:
    rendered = _render_openhost_service(templates_env, juicefs_enabled=False)
    # The After= line shouldn't list the juicefs unit when off.
    after_line = next(line for line in rendered.splitlines() if line.startswith("After="))
    assert "juicefs-mount.service" not in after_line, after_line
    assert "Requires=juicefs-mount.service" not in rendered


def test_openhost_service_juicefs_dep_when_enabled(templates_env: Environment) -> None:
    rendered = _render_openhost_service(templates_env, juicefs_enabled=True)
    # Requires= must be present and the After= line must include the unit.
    assert "Requires=juicefs-mount.service" in rendered, rendered
    after_line = next(line for line in rendered.splitlines() if line.startswith("After="))
    assert "juicefs-mount.service" in after_line, after_line


def test_openhost_service_no_dep_when_juicefs_unset(templates_env: Environment) -> None:
    """An operator who never passes -e juicefs_enabled gets no juicefs deps."""
    rendered = _render_openhost_service(templates_env)
    assert "juicefs-mount.service" not in rendered, rendered


# ---------------------------------------------------------------------------
# juicefs-mount.service.j2 — argv shape, no creds on the command line
# ---------------------------------------------------------------------------


def test_juicefs_mount_uses_env_for_creds(templates_env: Environment) -> None:
    """ACCESS_KEY/SECRET_KEY must come from EnvironmentFile, not argv.

    Putting credentials on argv leaks them via ``ps`` and any process
    listing.  This test pins down that pattern.
    """
    rendered = templates_env.get_template("juicefs-mount.service.j2").render()
    assert "EnvironmentFile=/etc/openhost/juicefs/s3.env" in rendered
    # The ExecStart line must NOT carry --access-key / --secret-key:
    exec_line = next(
        line for line in rendered.splitlines() if line.startswith("ExecStart=")
    )
    # Subsequent continuation lines also need scrutiny.
    cont_lines = []
    in_exec = False
    for line in rendered.splitlines():
        if line.startswith("ExecStart="):
            in_exec = True
            cont_lines.append(line)
            continue
        if in_exec:
            if line.endswith("\\"):
                cont_lines.append(line)
            else:
                cont_lines.append(line)
                break
    blob = "\n".join(cont_lines)
    assert "--access-key" not in blob, blob
    assert "--secret-key" not in blob, blob


def test_juicefs_mount_target_path(templates_env: Environment) -> None:
    """Mount target must match what config.toml.j2's archive_dir_override
    points at, and what containers.py expects as the host-side parent
    of per-app subdirs.
    """
    rendered = templates_env.get_template("juicefs-mount.service.j2").render()
    assert "/data/app_archive_juicefs" in rendered


def test_juicefs_mount_orders_before_openhost(templates_env: Environment) -> None:
    """openhost.service has Requires=juicefs-mount.service when JuiceFS
    is on, so the mount unit must declare itself ``Before=openhost.service``
    (or have the corresponding ordering) for systemd to start it first.
    """
    rendered = templates_env.get_template("juicefs-mount.service.j2").render()
    assert "Before=openhost.service" in rendered


def test_juicefs_mount_restart_on_failure(templates_env: Environment) -> None:
    """A dropped mount must surface as a failed unit rather than apps
    silently writing to the underlying empty mount-point.
    """
    rendered = templates_env.get_template("juicefs-mount.service.j2").render()
    assert "Restart=on-failure" in rendered


# ---------------------------------------------------------------------------
# juicefs-meta-dump — daily timer + atomic rename
# ---------------------------------------------------------------------------


def test_meta_dump_writes_under_persistent_data_dir(templates_env: Environment) -> None:
    """The dump must land under persistent_data_dir/openhost/ so the
    existing restic-based openhost-backup app picks it up.
    """
    rendered = templates_env.get_template("juicefs-meta-dump.service.j2").render()
    expected_path = (
        "/home/host/.openhost/local_compute_space/persistent_data/openhost/"
        "juicefs-metadata-dump.json"
    )
    assert expected_path in rendered, rendered


def test_meta_dump_uses_atomic_rename(templates_env: Environment) -> None:
    """Writing then renaming makes the dump file consistent for any
    backup snapshot taken concurrent with the timer firing.  A direct
    overwrite would leave a half-written file readable by restic.
    """
    rendered = templates_env.get_template("juicefs-meta-dump.service.j2").render()
    # ``mv X.tmp X`` must be present.  ``re.DOTALL`` lets the regex span
    # across the systemd line-continuation backslashes that split the
    # mv command across two lines.
    assert re.search(r"mv\s+\S+\.tmp\s.+\S+\.json", rendered, re.DOTALL), rendered


def test_meta_dump_timer_runs_daily(templates_env: Environment) -> None:
    rendered = templates_env.get_template("juicefs-meta-dump.timer.j2").render()
    assert "OnUnitActiveSec=24h" in rendered
    # Persistent= covers the case where the VM was off when the timer
    # was due to fire — systemd runs it at next boot.
    assert "Persistent=true" in rendered
