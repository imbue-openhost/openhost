"""HTTP API for the operator-controlled archive backend."""

from __future__ import annotations

import asyncio
import re
import sqlite3

import attr
from quart import Blueprint
from quart import jsonify
from quart import request
from quart.typing import ResponseReturnValue

from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendConfigureError
from compute_space.core.archive_backend import BackendState
from compute_space.db import get_db
from compute_space.web.middleware import login_required

api_archive_backend_bp = Blueprint("api_archive_backend", __name__)


def _state_to_response(state: BackendState) -> dict[str, object]:
    out = attr.asdict(state)
    # Drop the secret entirely (the dashboard never needs it back).
    out.pop("s3_secret_access_key", None)
    return out


# JuiceFS volume-name regex (cmd/format.go validName); s3_prefix doubles
# as the volume name and so has to satisfy it.
_S3_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def _normalise_s3_prefix(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    if not _S3_PREFIX_RE.match(cleaned):
        raise ValueError(
            "s3_prefix must be 3-63 characters of [a-z0-9-] (lowercase only, "
            "no leading/trailing dash); it doubles as the JuiceFS volume name."
        )
    return cleaned


@api_archive_backend_bp.route("/api/storage/archive_backend", methods=["GET"])
@login_required
async def get_archive_backend() -> ResponseReturnValue:
    """Return current archive-backend state (secret redacted) plus archive_dir, meta_db_path, meta_dumps."""
    config = get_config()
    db = get_db()
    state = archive_backend.read_state(db)
    response: dict[str, object] = {
        **_state_to_response(state),
        "archive_dir": archive_backend.juicefs_mount_dir(config) if state.backend == "s3" else None,
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
    try:
        _normalise_s3_prefix(form.get("s3_prefix"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"invalid s3_prefix: {exc}"}), 400
    if not (bucket and access_key and secret_key):
        return jsonify({"ok": False, "error": "bucket, access_key_id, and secret_access_key are required"}), 400
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


@api_archive_backend_bp.route("/api/storage/archive_backend/configure", methods=["POST"])
@login_required
async def configure_archive_backend() -> ResponseReturnValue:
    """One-shot configuration: ``backend='disabled'`` -> ``'s3'``.  No re-runs.

    Required: s3_bucket, s3_access_key_id, s3_secret_access_key.
    Optional: s3_region, s3_endpoint, s3_prefix, juicefs_volume_name.
    """
    config = get_config()
    db = get_db()
    state = archive_backend.read_state(db)
    if state.backend != "disabled":
        return jsonify(
            {
                "error": (
                    f"archive backend is already configured (backend={state.backend!r}); "
                    "reconfiguration is not supported"
                )
            }
        ), 409

    form = await request.form
    bucket = (form.get("s3_bucket") or "").strip()
    region = (form.get("s3_region") or "").strip() or None
    endpoint = (form.get("s3_endpoint") or "").strip() or None
    access_key = (form.get("s3_access_key_id") or "").strip()
    secret_key = (form.get("s3_secret_access_key") or "").strip()
    volume_name = (form.get("juicefs_volume_name") or "").strip() or None
    try:
        prefix = _normalise_s3_prefix(form.get("s3_prefix"))
    except ValueError as exc:
        return jsonify({"error": f"invalid s3_prefix: {exc}"}), 400

    missing = []
    if not bucket:
        missing.append("s3_bucket")
    if not access_key:
        missing.append("s3_access_key_id")
    if not secret_key:
        missing.append("s3_secret_access_key")
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    # The format+mount steps can take 10-30s.  Run off-loop so the event
    # loop doesn't block.  ``db`` from ``get_db()`` is request-thread-bound
    # by sqlite3's check_same_thread, so the worker opens its own
    # connection against the same DB file.
    db_path = config.db_path

    def _run() -> None:
        worker_db = sqlite3.connect(db_path)
        try:
            archive_backend.configure_backend(
                config,
                worker_db,
                s3_bucket=bucket,
                s3_region=region,
                s3_endpoint=endpoint,
                s3_prefix=prefix,
                s3_access_key_id=access_key,
                s3_secret_access_key=secret_key,
                juicefs_volume_name=volume_name,
            )
        finally:
            worker_db.close()

    try:
        await asyncio.to_thread(_run)
    except BackendConfigureError as exc:
        # 409 if it was a TOCTOU race against another configure attempt;
        # 500 for genuine bring-up failures.
        if "already configured" in str(exc):
            return jsonify({"error": str(exc)}), 409
        return jsonify({"error": str(exc)}), 500

    state = archive_backend.read_state(db)
    return jsonify(_state_to_response(state)), 200
