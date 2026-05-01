"""HTTP API for the operator-controlled archive backend."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading

import attr

from quart import Blueprint
from quart import current_app
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.archive_backend import (
    AppHook,
    BackendState,
    BackendSwitchError,
)
from compute_space.core.containers import is_container_running
from compute_space.core.containers import stop_container
from compute_space.core.logging import logger
from compute_space.db import get_db
from compute_space.web.middleware import login_required


api_archive_backend_bp = Blueprint("api_archive_backend", __name__)


# ---------------------------------------------------------------------------
# State serialisation
# ---------------------------------------------------------------------------


def _state_to_response(state: BackendState) -> dict:
    """Project the DB state to a JSON-safe shape that's safe to return
    to the dashboard.

    Drops ``s3_secret_access_key`` entirely (rather than masking with
    e.g. ``****``) because the dashboard never needs to see the secret
    again after the operator entered it; including it in any 200
    response would risk it landing in HTTP access logs.
    """
    out = attr.asdict(state)
    out.pop("s3_secret_access_key", None)
    return out


# Permissive ASCII subset for S3 prefix path segments: letters,
# digits, dot, underscore, dash.  Anything outside the set is
# rejected — S3 itself accepts a wider range, but we'd rather have
# a tight allowlist than worry about edge cases (URL-encoding
# semantics, JuiceFS prefix interpretation, weird shell quoting in
# downstream subprocess calls).  ``..`` segments are always
# rejected separately to defuse traversal-style mistakes.
_S3_PREFIX_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_S3_PREFIX_MAX_LEN = 256


def _normalise_s3_prefix(raw: str | None) -> str | None:
    """Validate and normalise an operator-supplied S3 prefix.

    Returns the cleaned prefix (no leading/trailing slashes), or
    None if the input was empty.  Raises ``ValueError`` with a
    clear message if the prefix shape is invalid.

    The cleaned form is what gets stored in the DB and what
    ``_bucket_url`` appends to the bucket URL — keeping the storage
    canonical avoids surprises when the operator's input has a
    rogue leading slash or trailing whitespace.
    """
    if raw is None:
        return None
    cleaned = raw.strip().strip("/")
    if not cleaned:
        return None
    if len(cleaned) > _S3_PREFIX_MAX_LEN:
        raise ValueError(f"s3_prefix must be at most {_S3_PREFIX_MAX_LEN} characters")
    if "\x00" in cleaned:
        raise ValueError("s3_prefix must not contain NUL bytes")
    segments = cleaned.split("/")
    for seg in segments:
        if not seg:
            raise ValueError("s3_prefix must not contain empty path segments (got '//')")
        if seg in (".", ".."):
            raise ValueError("s3_prefix must not contain '.' or '..' path segments")
        if not _S3_PREFIX_SEGMENT_RE.match(seg):
            raise ValueError(
                "s3_prefix path segments must match [A-Za-z0-9._-] only "
                f"(got {seg!r})"
            )
    return cleaned


# ---------------------------------------------------------------------------
# AppHook wiring
# ---------------------------------------------------------------------------


def _build_hook(app) -> AppHook:  # noqa: ANN001  -- Quart app, kept loose to avoid the import cycle
    """Wire ``AppHook`` callbacks against the live apps table.

    The list/stop/start callbacks operate on opted-in apps only —
    apps with ``app_archive=True`` or ``access_all_data=True`` in
    their manifest.  Unaffected apps keep running through the switch.
    """
    config = app.openhost_config

    def list_archive_apps() -> list[str]:
        # Read manifest_raw and look for a real ``app_archive = true``
        # (or ``access_all_data = true``) assignment.  Uses the
        # shared ``manifest_uses_archive`` helper so route-level
        # gating (``rename_app`` / ``reload_app``) and switch-flow
        # enumeration agree on what counts.
        db = sqlite3.connect(config.db_path)
        try:
            rows = db.execute(
                "SELECT name, manifest_raw, status FROM apps "
                "WHERE status IN ('running', 'starting', 'building')"
            ).fetchall()
        finally:
            db.close()
        return [
            name
            for name, manifest_raw, _status in rows
            if archive_backend.manifest_uses_archive(manifest_raw or "")
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
        # Local import to break the route<->core import cycle that
        # ``start_app_process`` would otherwise pull in at module load.
        from compute_space.core.apps import start_app_process  # noqa: PLC0415

        db = sqlite3.connect(config.db_path)
        db.row_factory = sqlite3.Row
        try:
            start_app_process(name, db, app.openhost_config)
        finally:
            db.close()

    def set_config(new_cfg) -> None:  # noqa: ANN001
        # Replace the live Config so the next ``get_config()`` call
        # returns the new ``app_archive_dir``.
        app.openhost_config = new_cfg

    return AppHook(
        list_app_archive_apps=list_archive_apps,
        stop_app=stop_app,
        start_app=start_app,
        set_config=set_config,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@api_archive_backend_bp.route("/api/storage/archive_backend", methods=["GET"])
@login_required
def get_archive_backend() -> ResponseReturnValue:
    """Return the current archive-backend state (with secret redacted).

    Also returns the resolved host-side path, which the dashboard
    surfaces as a debugging aid: operators have asked "where do my
    archive bytes actually live?" enough that giving them a
    copy-pasteable answer is worth the extra two lines.
    """
    config = get_config()
    db = get_db()
    state = archive_backend.read_state(db)
    return jsonify(
        {
            **_state_to_response(state),
            "archive_dir": archive_backend.archive_dir_for_backend(config, state.backend),
        }
    )


@api_archive_backend_bp.route(
    "/api/storage/archive_backend/test_connection", methods=["POST"]
)
@login_required
async def test_connection() -> ResponseReturnValue:
    """Try to reach the supplied S3 bucket with the supplied creds.

    Pure pre-flight: doesn't touch the DB or the live JuiceFS mount.
    """
    form = await request.form
    bucket = (form.get("s3_bucket") or "").strip()
    region = (form.get("s3_region") or "").strip() or None
    endpoint = (form.get("s3_endpoint") or "").strip() or None
    access_key = (form.get("s3_access_key_id") or "").strip()
    secret_key = (form.get("s3_secret_access_key") or "").strip()
    # Pre-flight against the bucket itself, not bucket+prefix —
    # head_bucket is bucket-level and a wrong prefix wouldn't
    # surface here anyway.  We DO normalise the prefix shape so
    # the operator catches typos before the actual switch runs.
    try:
        _normalise_s3_prefix(form.get("s3_prefix"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"invalid s3_prefix: {exc}"}), 400
    if not (bucket and access_key and secret_key):
        return jsonify(
            {"ok": False, "error": "bucket, access_key_id, and secret_access_key are required"}
        ), 400
    # head_bucket can take seconds (DNS + TLS + HTTP round-trip);
    # run it on a worker thread so the asyncio event loop stays
    # responsive to other requests.
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
    """Trigger a backend switch.  Returns 202 + ``{state: 'switching'}``
    immediately; the dashboard polls ``GET`` to see the final state.

    Body fields:
      ``backend``: ``local`` | ``s3``
      ``confirm_data_loss``: ``true`` (required — see below)
      ``s3_bucket``, ``s3_region``, ``s3_endpoint``, ``s3_access_key_id``,
      ``s3_secret_access_key``, ``juicefs_volume_name`` (when target=s3)
      ``s3_prefix``: optional path under the bucket — lets multiple
        OpenHost zones share a single bucket cleanly (each zone
        configured with its own prefix).  Empty / unset means
        "use the bucket root".
      ``delete_source_after_copy``: ``true`` to drop the source-side data
      after the copy succeeds (frees local disk on local->s3, or makes
      the S3 bucket the source of truth on s3->local).

    ``confirm_data_loss`` is required because the switch flow stops
    every opted-in app while the data copy runs, and a slow copy of
    a large archive will drop in-flight uploads.  The dashboard puts
    this behind an explicit "I understand apps will be restarted"
    checkbox.
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

    # Best-effort fast-fail when a switch is obviously already in
    # flight, so the operator's double-click gets a clean 409 instead
    # of a 202 followed by a state_message saying the worker
    # collided with itself.  This is racy by construction (two POSTs
    # can both observe ``state='idle'`` and both pass) — the
    # authoritative gate is the atomic UPDATE-WHERE inside
    # switch_backend, which is the source of truth.  The race
    # window for the duplicate-202 case is narrow and the worker's
    # state_message records the loser's error, so the operator can
    # still see what happened.
    db = get_db()
    state = archive_backend.read_state(db)
    if state.state == "switching":
        return jsonify(
            {"error": "An archive backend switch is already in progress; wait for it to finish."}
        ), 409

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
