"""HTTP API for the operator-controlled archive backend.

Three endpoints, all owner-authenticated:

- ``GET  /api/storage/archive_backend`` — current state.
- ``POST /api/storage/archive_backend`` — switch backends.
- ``POST /api/storage/archive_backend/test_connection`` — pre-flight
  check used by the dashboard's "Test connection" button before the
  operator commits to a switch.

The backend-switch flow runs in a worker thread because it does
file copies + (potentially) a JuiceFS download/format/mount, none
of which should run on the asyncio event loop.  The endpoint returns
immediately with ``status: "switching"`` and the dashboard polls
``GET`` for the final state.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict

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


def _redact(state: BackendState) -> dict:
    """Project the DB state to a JSON-safe shape with the secret access
    key dropped.  The dashboard never needs to see the secret again
    after the operator entered it (and a 200 response that includes it
    would log it in any access log).
    """
    out = asdict(state)
    out.pop("s3_secret_access_key", None)
    return out


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
        # Read straight from the manifest_raw column rather than
        # parsing every manifest live — simpler, fast enough, and
        # robust to schema-format drift in the manifest parser.
        db = sqlite3.connect(config.db_path)
        try:
            rows = db.execute(
                "SELECT name, manifest_raw, status FROM apps "
                "WHERE status IN ('running', 'starting', 'building')"
            ).fetchall()
        finally:
            db.close()
        names: list[str] = []
        for name, manifest_raw, _status in rows:
            raw = manifest_raw or ""
            # Cheap textual heuristic — the manifest parser is the
            # canonical source of truth, but for the purposes of
            # "should we stop this app for the switch?" matching the
            # raw text is fine and avoids a hard dependency on the
            # manifest module from the api layer.
            if "app_archive" in raw and "true" in raw or "access_all_data" in raw and "true" in raw:
                names.append(name)
        return names

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
            **_redact(state),
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
    if not (bucket and access_key and secret_key):
        return jsonify(
            {"ok": False, "error": "bucket, access_key_id, and secret_access_key are required"}
        ), 400
    error = archive_backend.test_s3_credentials(
        s3_bucket=bucket,
        s3_region=region,
        s3_endpoint=endpoint,
        s3_access_key_id=access_key,
        s3_secret_access_key=secret_key,
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
        s3_kwargs = {
            "s3_bucket": (form.get("s3_bucket") or "").strip() or None,
            "s3_region": (form.get("s3_region") or "").strip() or None,
            "s3_endpoint": (form.get("s3_endpoint") or "").strip() or None,
            "s3_access_key_id": (form.get("s3_access_key_id") or "").strip() or None,
            "s3_secret_access_key": (form.get("s3_secret_access_key") or "").strip() or None,
            "juicefs_volume_name": (form.get("juicefs_volume_name") or "").strip() or None,
        }
        missing = [k for k in ("s3_bucket", "s3_access_key_id", "s3_secret_access_key") if not s3_kwargs.get(k)]
        if missing:
            return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400

    delete_source = (form.get("delete_source_after_copy") or "").strip().lower() in ("1", "true", "yes")

    # Refuse if a switch is already running so two concurrent POSTs
    # don't fight.  We rely on the same DB-level state check inside
    # switch_backend, but rejecting here gives a cleaner 409 vs the
    # 500 we'd otherwise raise.
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
                **{k: v for k, v in s3_kwargs.items() if v is not None},
            )
        except BackendSwitchError as exc:
            logger.warning("archive backend switch failed: %s", exc)
        except Exception:
            logger.exception("archive backend switch crashed")
        finally:
            worker_db.close()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"state": "switching"}), 202
