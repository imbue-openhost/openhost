"""HTTP API for the operator-controlled archive backend."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from typing import Any

import attr
from quart import Blueprint
from quart import current_app
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.archive_backend import AppHook
from compute_space.core.archive_backend import BackendState
from compute_space.core.archive_backend import BackendSwitchError
from compute_space.core.containers import is_container_running
from compute_space.core.containers import stop_container
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.middleware import login_required

api_archive_backend_bp = Blueprint("api_archive_backend", __name__)


def _state_to_response(state: BackendState) -> dict[str, object]:
    """Project the DB state to a JSON shape with the secret removed."""
    out = attr.asdict(state)
    # Drop entirely rather than masking: the dashboard never needs the
    # secret again after entry, and including it risks leaking to logs.
    out.pop("s3_secret_access_key", None)
    return out


# Constraints come from JuiceFS's volume-name regex (cmd/format.go
# validName); the prefix is mapped directly to the volume name.
_S3_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def _normalise_s3_prefix(raw: str | None) -> str | None:
    """Trim and validate the S3 prefix; return None when empty."""
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not _S3_PREFIX_RE.match(cleaned):
        raise ValueError(
            "s3_prefix must be 3-63 characters of [a-z0-9-] (lowercase only, "
            "no leading/trailing dash) — it doubles as the JuiceFS volume name "
            "and so has to satisfy JuiceFS's name regex.  For multi-zone "
            "isolation under a shared bucket, give each zone a unique "
            "single-segment name (e.g. ``andrew-3`` for zone andrew-3, "
            f"``andrew-1`` for zone andrew-1).  Got: {cleaned!r}"
        )
    return cleaned


def _build_hook(app: Any) -> AppHook:  # ``Any`` to avoid import-cycle on the Quart app type
    """Wire ``AppHook`` callbacks against the live apps table."""
    config = app.openhost_config

    def list_archive_apps() -> list[str]:
        # Use the broader manifest_uses_archive predicate: every app
        # with the archive mount must stop during a switch, including
        # access_all_data apps (vs. the install/reload gate which uses
        # the narrower manifest_requires_archive).
        db = sqlite3.connect(config.db_path)
        try:
            rows = db.execute(
                "SELECT name, manifest_raw, status FROM apps WHERE status IN ('running', 'starting', 'building')"
            ).fetchall()
        finally:
            db.close()
        return [
            name for name, manifest_raw, _status in rows if archive_backend.manifest_uses_archive(manifest_raw or "")
        ]

    def stop_app(name: str) -> None:
        db = sqlite3.connect(config.db_path)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM apps WHERE name = ?", (name,)).fetchone()
            if row is None:
                return
            cid = row["container_id"]
            if cid and is_container_running(cid):
                stop_container(cid)
            db.execute(
                "UPDATE apps SET status='stopped', container_id=NULL WHERE name=?",
                (name,),
            )
            db.commit()
        finally:
            db.close()

    def start_app(name: str) -> None:
        # Local import to break the route<->core import cycle.
        from compute_space.core.apps import start_app_process  # noqa: PLC0415

        db = sqlite3.connect(config.db_path)
        db.row_factory = sqlite3.Row
        try:
            start_app_process(name, db, app.openhost_config)
        finally:
            db.close()

    def set_config(new_cfg: Any) -> None:
        app.openhost_config = new_cfg

    return AppHook(
        list_app_archive_apps=list_archive_apps,
        stop_app=stop_app,
        start_app=start_app,
        set_config=set_config,
    )


@api_archive_backend_bp.route("/api/storage/archive_backend", methods=["GET"])
@login_required
async def get_archive_backend() -> ResponseReturnValue:
    """Return current archive-backend state (secret redacted) plus archive_dir, meta_db_path, meta_dumps."""
    config = get_config()
    db = get_db()
    state = archive_backend.read_state(db)
    response: dict[str, object] = {
        **_state_to_response(state),
        "archive_dir": archive_backend.archive_dir_for_backend(config, state.backend),
        "meta_db_path": archive_backend.juicefs_meta_db_path(config),
        "meta_dumps": None,
    }
    if state.backend == "s3" and state.s3_bucket and state.s3_access_key_id and state.s3_secret_access_key:
        # Off-loop: list_objects_v2 does DNS + TLS + HTTP.
        summary = await asyncio.to_thread(
            archive_backend.list_meta_dumps,
            state.s3_bucket,
            state.s3_region,
            state.s3_endpoint,
            state.s3_access_key_id,
            state.s3_secret_access_key,
            state.s3_prefix,
        )
        if summary is not None:
            response["meta_dumps"] = {
                "count": summary.count,
                "latest_at": summary.latest_at,
                "latest_key": summary.latest_key,
            }
    return jsonify(response)


@api_archive_backend_bp.route("/api/storage/archive_backend/test_connection", methods=["POST"])
@login_required
async def test_connection() -> ResponseReturnValue:
    """Pre-flight S3 reachability/credentials check; doesn't touch the DB or live mount."""
    form = await request.form
    bucket = (form.get("s3_bucket") or "").strip()
    region = (form.get("s3_region") or "").strip() or None
    endpoint = (form.get("s3_endpoint") or "").strip() or None
    access_key = (form.get("s3_access_key_id") or "").strip()
    secret_key = (form.get("s3_secret_access_key") or "").strip()
    # Validate prefix shape early so typos surface here rather than
    # 30s into the eventual JuiceFS format step.
    try:
        _normalise_s3_prefix(form.get("s3_prefix"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"invalid s3_prefix: {exc}"}), 400
    if not (bucket and access_key and secret_key):
        return jsonify({"ok": False, "error": "bucket, access_key_id, and secret_access_key are required"}), 400
    # Off-loop: head_bucket can take seconds (DNS + TLS + HTTP).
    error = await asyncio.to_thread(
        archive_backend.test_s3_credentials,
        bucket,
        region,
        endpoint,
        access_key,
        secret_key,
    )
    if error:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True})


@api_archive_backend_bp.route("/api/storage/archive_backend", methods=["POST"])
@login_required
async def post_archive_backend() -> ResponseReturnValue:
    """Trigger a backend switch. Returns 202 + ``{state: 'switching'}``; poll GET for final state.

    Required form fields: ``backend`` (``local``|``s3``), ``confirm_data_loss=true``.
    When ``backend=s3``: ``s3_bucket``, ``s3_access_key_id``, ``s3_secret_access_key``
    (required) and ``s3_region``, ``s3_endpoint``, ``s3_prefix``,
    ``juicefs_volume_name`` (optional). ``delete_source_after_copy`` drops the
    source after a successful copy. ``disabled`` is not a valid target.
    """
    form = await request.form
    target = (form.get("backend") or "").strip()
    confirm = (form.get("confirm_data_loss") or "").strip().lower() in ("1", "true", "yes")

    if target not in ("local", "s3"):
        return jsonify({"error": "backend must be 'local' or 's3'"}), 400
    if not confirm:
        return jsonify(
            {"error": "confirm_data_loss=true is required; the switch stops affected apps and copies data"}
        ), 400

    s3_kwargs: dict[str, str | None] = {}
    if target == "s3":
        try:
            normalised_prefix = _normalise_s3_prefix(form.get("s3_prefix"))
        except ValueError as exc:
            return jsonify({"error": f"invalid s3_prefix: {exc}"}), 400
        s3_kwargs = {
            "s3_bucket": (form.get("s3_bucket") or "").strip() or None,
            "s3_region": (form.get("s3_region") or "").strip() or None,
            "s3_endpoint": (form.get("s3_endpoint") or "").strip() or None,
            "s3_prefix": normalised_prefix,
            "s3_access_key_id": (form.get("s3_access_key_id") or "").strip() or None,
            "s3_secret_access_key": (form.get("s3_secret_access_key") or "").strip() or None,
            "juicefs_volume_name": (form.get("juicefs_volume_name") or "").strip() or None,
        }
        missing = [k for k in ("s3_bucket", "s3_access_key_id", "s3_secret_access_key") if not s3_kwargs.get(k)]
        if missing:
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    delete_source = (form.get("delete_source_after_copy") or "").strip().lower() in ("1", "true", "yes")

    # Best-effort double-click fast-fail; the authoritative gate is
    # the atomic UPDATE-WHERE inside switch_backend.
    db = get_db()
    state = archive_backend.read_state(db)
    if state.state == "switching":
        return jsonify({"error": "An archive backend switch is already in progress; wait for it to finish."}), 409

    config = get_config()
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    hook = _build_hook(app)

    def _run() -> None:
        worker_db = sqlite3.connect(config.db_path)
        try:
            archive_backend.switch_backend(
                config,
                worker_db,
                hook,
                target_backend=target,
                delete_source_after_copy=delete_source,
                **s3_kwargs,
            )
        except BackendSwitchError as exc:
            logger.warning("archive backend switch failed: %s", exc)
        except Exception:
            logger.exception("archive backend switch crashed")
        finally:
            worker_db.close()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"state": "switching"}), 202
