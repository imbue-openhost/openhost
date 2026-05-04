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


# ---------------------------------------------------------------------------
# State serialisation
# ---------------------------------------------------------------------------


def _state_to_response(state: BackendState) -> dict[str, object]:
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


# JuiceFS's ``format`` command requires the volume NAME to match
# this regex (see cmd/format.go validName check in upstream
# juicefs).  Because we map ``s3_prefix`` directly to the JuiceFS
# volume name (which JuiceFS in turn uses as the per-object prefix
# in S3), the prefix must satisfy the same constraint.  The regex
# rejects:
#   - leading/trailing dashes
#   - slashes (so multi-segment prefixes like ``a/b`` are out — the
#     JuiceFS bucket-URL parser breaks on path components, see the
#     long comment on ``_bucket_url`` in core.archive_backend)
#   - uppercase, underscores, dots, NUL, whitespace, anything else
# The 3-63 length window is also JuiceFS's.
_S3_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


def _normalise_s3_prefix(raw: str | None) -> str | None:
    """Validate an operator-supplied S3 prefix.

    Returns the prefix (whitespace-trimmed) when set, ``None`` when
    empty.  Raises ``ValueError`` with a clear message if the prefix
    shape is invalid.

    Stored verbatim in the DB and used as the JuiceFS volume name
    when the switch runs, which is what makes per-zone isolation
    work without breaking the JuiceFS bucket-URL parser.  See the
    long comment on ``_bucket_url`` in ``core.archive_backend`` for
    why we can't store the prefix as a path component on the URL
    instead.
    """
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


# ---------------------------------------------------------------------------
# AppHook wiring
# ---------------------------------------------------------------------------


def _build_hook(app: Any) -> AppHook:  # ``Any`` because typing the Quart app would pull in import-cycle imports
    """Wire ``AppHook`` callbacks against the live apps table.

    The list/stop/start callbacks operate on opted-in apps only —
    apps with ``app_archive=True`` or ``access_all_data=True`` in
    their manifest.  Unaffected apps keep running through the switch.
    """
    config = app.openhost_config

    def list_archive_apps() -> list[str]:
        # Read manifest_raw and look for a real ``app_archive = true``
        # (or ``access_all_data = true``) assignment.  Uses the
        # shared ``manifest_uses_archive`` helper — the *broader*
        # of the two predicates, because during a backend switch we
        # must stop every app whose container has the archive mount,
        # which includes ``access_all_data`` apps that see the parent
        # archive directory.  This deliberately differs from the
        # install/reload gate's ``manifest_requires_archive`` (only
        # ``app_archive=true``), which is about hard runtime
        # dependence rather than mount-handle invalidation.
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
        # Local import to break the route<->core import cycle that
        # ``start_app_process`` would otherwise pull in at module load.
        from compute_space.core.apps import start_app_process  # noqa: PLC0415

        db = sqlite3.connect(config.db_path)
        db.row_factory = sqlite3.Row
        try:
            start_app_process(name, db, app.openhost_config)
        finally:
            db.close()

    def set_config(new_cfg: Any) -> None:  # ``Any`` for the same reason as ``_build_hook(app)``
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
async def get_archive_backend() -> ResponseReturnValue:
    """Return the current archive-backend state (with secret redacted).

    Surfaces three derived/operator-visible fields on top of the raw
    DB state:

    - ``archive_dir``: where ``app_archive`` data lives on the host
      RIGHT NOW (the FUSE mount when backend=s3, the local-disk path
      when backend=local).  Operators have asked "where do my
      archive bytes actually live" enough that giving them a
      copy-pasteable answer is worth the few extra lines.
    - ``meta_db_path``: where the JuiceFS metadata DB lives.  The
      one file an operator must back up to survive disk loss when
      backend=s3.  Always present in the response (renders as a
      no-op when backend=local — the path doesn't exist yet, but
      surfacing it consistently lets the dashboard show "this is
      where it WILL live" before a switch).
    - ``meta_dumps``: summary of the JuiceFS auto-meta-backup
      objects in the bucket.  ``None`` when backend=local OR when
      we can't reach S3 to list them; an object with ``count`` +
      ``latest_at`` + ``latest_key`` otherwise.  JuiceFS writes one
      every hour by default (see ``--backup-meta=1h`` upstream),
      so a healthy zone shows ``latest_at`` within the last hour.
    """
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
        # Only list when we have everything we need to authenticate.
        # Run on a worker thread because list_objects_v2 does DNS +
        # TLS + HTTP and would otherwise block the event loop on a
        # cold cache.
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
    """Try to reach the supplied S3 bucket with the supplied creds.

    Pure pre-flight: doesn't touch the DB or the live JuiceFS mount.
    """
    form = await request.form
    bucket = (form.get("s3_bucket") or "").strip()
    region = (form.get("s3_region") or "").strip() or None
    endpoint = (form.get("s3_endpoint") or "").strip() or None
    access_key = (form.get("s3_access_key_id") or "").strip()
    secret_key = (form.get("s3_secret_access_key") or "").strip()
    # head_bucket is bucket-level; the prefix doesn't enter into
    # the reachability check at all.  But we still validate the
    # prefix shape so the operator catches typos before the actual
    # switch runs and trips the JuiceFS name regex 30 s deep into
    # the format-volume step instead.
    try:
        _normalise_s3_prefix(form.get("s3_prefix"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": f"invalid s3_prefix: {exc}"}), 400
    if not (bucket and access_key and secret_key):
        return jsonify({"ok": False, "error": "bucket, access_key_id, and secret_access_key are required"}), 400
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
      ``backend``: ``local`` | ``s3``.  The ``disabled`` state cannot
        be selected here — it's the seed state for fresh zones, and
        once an operator has picked a backend they can switch
        between local and s3 but never back to disabled (which
        would orphan the on-disk / in-bucket archive bytes).
      ``confirm_data_loss``: ``true`` (required — see below)
      ``s3_bucket``, ``s3_region``, ``s3_endpoint``, ``s3_access_key_id``,
      ``s3_secret_access_key``, ``juicefs_volume_name`` (when target=s3)
      ``s3_prefix``: optional single-segment lowercase name (3-63
        chars of ``[a-z0-9-]``).  Lets multiple OpenHost zones share
        a single bucket cleanly: each zone runs with its own prefix
        and JuiceFS namespaces every chunk it writes under
        ``<bucket>/<prefix>/...``.  Implemented by mapping the
        prefix to the JuiceFS volume name (which JuiceFS already
        uses as a per-object prefix internally — see the comment on
        ``_bucket_url`` in core.archive_backend for why we can't put
        the prefix in the bucket URL instead).  Empty / unset means
        "use the volume name 'openhost'", which is the historical
        default and what a single-zone deploy gets.
      ``delete_source_after_copy``: ``true`` to drop the source-side data
      after the copy succeeds (frees local disk on local->s3, or makes
      the S3 bucket the source of truth on s3->local).

    ``confirm_data_loss`` is required because the switch flow stops
    every opted-in app while the data copy runs, and a slow copy of
    a large archive will drop in-flight uploads.  The dashboard puts
    this behind an explicit "I understand apps will be restarted"
    checkbox.

    The disabled→local / disabled→s3 transitions don't actually lose
    data (no archive-using app can have been installed while
    disabled), but ``confirm_data_loss=true`` is still required for
    consistency with the local↔s3 transitions — operators get one
    invariant set of expectations regardless of starting state.
    """
    form = await request.form
    target = (form.get("backend") or "").strip()
    confirm = (form.get("confirm_data_loss") or "").strip().lower() in ("1", "true", "yes")

    if target not in ("local", "s3"):
        # ``disabled`` is intentionally not a valid target: see the
        # docstring + ``switch_backend``'s rejection of *→disabled
        # transitions.  Operators land at disabled at zone-init; the
        # only way out is to pick local or s3.
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
