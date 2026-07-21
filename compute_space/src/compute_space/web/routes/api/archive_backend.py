"""HTTP API for the operator-controlled archive backend."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Annotated

import attr
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post
from litestar.params import Body

from compute_space.config import Config
from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendConfigureError
from compute_space.core.archive_backend import BackendState
from compute_space.web.auth.auth import require_owner_auth


@attr.s(auto_attribs=True, frozen=True)
class MetaDumpsSummary:
    count: int
    latest_at: str | None
    latest_key: str | None


@attr.s(auto_attribs=True, frozen=True)
class BackendStateResponse:
    """``BackendState`` for the dashboard — secret redacted, plus mount-derived paths."""

    backend: str
    s3_bucket: str | None
    s3_region: str | None
    s3_endpoint: str | None
    s3_prefix: str | None
    s3_access_key_id: str | None
    juicefs_volume_name: str
    configured_at: str | None
    state_message: str | None
    archive_dir: str | None
    meta_db_path: str
    meta_dumps: MetaDumpsSummary | None
    # On backend='local': the apps that currently have data in the local
    # archive dir.  Surfaced so the dashboard can tell the operator exactly
    # whose data an S3 upgrade will migrate.  Empty/omitted for other backends.
    local_archive_apps: list[str] = attr.Factory(list)


@attr.s(auto_attribs=True, frozen=True)
class TestConnectionOk:
    ok: bool  # always True


@attr.s(auto_attribs=True, frozen=True)
class TestConnectionError:
    ok: bool  # always False
    error: str


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str


def _state_to_response(
    state: BackendState,
    archive_dir: str | None,
    meta_db_path: str,
    meta_dumps: MetaDumpsSummary | None,
    local_archive_apps: list[str] | None = None,
) -> BackendStateResponse:
    return BackendStateResponse(
        backend=state.backend,
        s3_bucket=state.s3_bucket,
        s3_region=state.s3_region,
        s3_endpoint=state.s3_endpoint,
        s3_prefix=state.s3_prefix,
        s3_access_key_id=state.s3_access_key_id,
        juicefs_volume_name=state.juicefs_volume_name,
        configured_at=state.configured_at,
        state_message=state.state_message,
        archive_dir=archive_dir,
        meta_db_path=meta_db_path,
        meta_dumps=meta_dumps,
        local_archive_apps=local_archive_apps or [],
    )


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


@attr.s(auto_attribs=True, frozen=True)
class TestConnectionRequest:
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = ""


@attr.s(auto_attribs=True, frozen=True)
class ConfigureArchiveRequest:
    s3_bucket: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = ""
    juicefs_volume_name: str = ""
    # When the current backend is 'local' and apps have written archive
    # data, the operator must explicitly acknowledge the local->S3
    # migration.  The migration copies + verifies the data into S3 and is
    # fail-open (local data is kept if anything goes wrong), but the switch
    # to S3 is one-way and the local copy is removed afterwards, so we
    # require an explicit opt-in rather than doing it silently.
    confirm_migrate_local: bool = False


@get("/api/storage/archive_backend", guards=[require_owner_auth])
async def get_archive_backend(db: sqlite3.Connection, config: Config) -> BackendStateResponse:
    """Return current archive-backend state (secret redacted) plus archive_dir, meta_db_path, meta_dumps."""
    state = archive_backend.read_state(db)
    # The archive tier is always the JuiceFS mountpoint (local file backend or
    # S3); only the legacy 'disabled' state has no mount.
    if state.backend in ("s3", "local"):
        archive_dir = archive_backend.juicefs_mount_dir(config)
    else:
        archive_dir = None
    meta_db_path = archive_backend.juicefs_meta_db_path(config)

    meta_dumps: MetaDumpsSummary | None = None
    if state.backend == "s3" and state.s3_bucket and state.s3_access_key_id and state.s3_secret_access_key:
        # Off-loop: list_objects_v2 does DNS + TLS + HTTP.
        summary = await asyncio.to_thread(
            archive_backend.list_meta_dumps,
            state.s3_bucket,
            state.s3_region,
            state.s3_endpoint,
            state.s3_access_key_id,
            state.s3_secret_access_key,
            state.juicefs_volume_name,
        )
        if summary is not None:
            meta_dumps = MetaDumpsSummary(
                count=summary.count,
                latest_at=summary.latest_at,
                latest_key=summary.latest_key,
            )
    local_apps = archive_backend.local_archive_apps_with_data(config, db) if state.backend == "local" else []
    return _state_to_response(state, archive_dir, meta_db_path, meta_dumps, local_apps)


@post("/api/storage/archive_backend/test_connection", status_code=200, guards=[require_owner_auth])
async def test_connection(
    data: Annotated[TestConnectionRequest, Body(media_type=MediaType.JSON)],
) -> Response[TestConnectionOk] | Response[TestConnectionError]:
    """Pre-flight S3 reachability/credentials check; doesn't touch the DB or live mount."""
    try:
        _normalise_s3_prefix(data.s3_prefix or None)
    except ValueError as exc:
        return Response(content=TestConnectionError(ok=False, error=f"invalid s3_prefix: {exc}"), status_code=400)
    error = await asyncio.to_thread(
        archive_backend.test_s3_credentials,
        data.s3_bucket,
        data.s3_region.strip() or None,
        data.s3_endpoint.strip() or None,
        data.s3_access_key_id,
        data.s3_secret_access_key,
    )
    if error:
        return Response(content=TestConnectionError(ok=False, error=error), status_code=400)
    return Response(content=TestConnectionOk(ok=True), status_code=200, media_type=MediaType.JSON)


@post("/api/storage/archive_backend/configure", status_code=200, guards=[require_owner_auth])
async def configure_archive_backend(
    data: Annotated[ConfigureArchiveRequest, Body(media_type=MediaType.JSON)],
    db: sqlite3.Connection,
    config: Config,
) -> Response[BackendStateResponse] | Response[ErrorResponse]:
    """One-shot upgrade to S3.  Allowed from ``'local'`` (the default —
    migrates local archive data into the bucket) or the legacy ``'disabled'``
    state.  Refused once ``backend='s3'``."""
    state = archive_backend.read_state(db)
    if state.backend == "s3":
        return Response(
            content=ErrorResponse(
                error=("archive backend is already configured to S3; reconfiguration is not supported")
            ),
            status_code=409,
        )

    # Guard the local->S3 migration behind an explicit acknowledgement when
    # there is actually local data to migrate.  The dashboard shows which
    # apps have data (from local_archive_apps in the GET state) and the
    # fail-open/one-way semantics before ticking the confirm box; the API
    # refuses to proceed (409, listing the apps) until the operator confirms.
    local_apps_with_data = archive_backend.local_archive_apps_with_data(config, db) if state.backend == "local" else []
    if local_apps_with_data:
        if not data.confirm_migrate_local:
            apps = local_apps_with_data
            return Response(
                content=ErrorResponse(
                    error=(
                        "The archive tier currently uses LOCAL disk and these "
                        f"apps have data on it: {', '.join(apps)}.  Configuring "
                        "S3 will migrate that data into the bucket and then "
                        "remove the local copy.  This switch is one-way.  "
                        "Re-submit with confirm_migrate_local=true to proceed."
                    )
                ),
                status_code=409,
            )

    try:
        prefix = _normalise_s3_prefix(data.s3_prefix or None)
    except ValueError as exc:
        return Response(content=ErrorResponse(error=f"invalid s3_prefix: {exc}"), status_code=400)

    region = data.s3_region.strip() or None
    endpoint = data.s3_endpoint.strip() or None
    volume_name = data.juicefs_volume_name.strip() or None

    # The format+mount steps can take 10-30s.  Run off-loop so the event
    # loop doesn't block.  ``db`` from ``provide_db()`` is request-thread-bound
    # by sqlite3's check_same_thread, so the worker opens its own
    # connection against the same DB file.
    db_path = config.db_path

    def _run() -> None:
        worker_db = sqlite3.connect(db_path)
        worker_db.row_factory = sqlite3.Row
        try:
            was_local = archive_backend.read_state(worker_db).backend == "local"
            # Imported lazily to avoid a core<-web import cycle.
            from compute_space.core.apps import start_apps_by_id  # noqa: PLC0415
            from compute_space.core.apps import stop_running_archive_apps  # noqa: PLC0415

            # For a local->S3 migration the JuiceFS mount must be restarted
            # (juicefs config re-points the volume to S3).  A running app
            # container holding the mount open would make the unmount time out,
            # so we STOP archive-using apps right before the remount and record
            # which ones, then RESTART them afterwards so they re-open the
            # now-S3-backed archive.  The quiesce callback runs inside
            # configure_backend just before the remount.
            quiesced: list[str] = []

            def _quiesce() -> None:
                quiesced.extend(stop_running_archive_apps(worker_db, config))

            try:
                archive_backend.configure_backend(
                    config,
                    worker_db,
                    s3_bucket=data.s3_bucket,
                    s3_region=region,
                    s3_endpoint=endpoint,
                    s3_prefix=prefix,
                    s3_access_key_id=data.s3_access_key_id,
                    s3_secret_access_key=data.s3_secret_access_key,
                    juicefs_volume_name=volume_name,
                    quiesce_archive_apps=_quiesce if was_local else None,
                )
            finally:
                # Whether the migration succeeded (mount now S3-backed) or
                # failed-open (mount restored to local), restart any apps we
                # stopped so they re-open the archive.
                if quiesced:
                    start_apps_by_id(quiesced, worker_db, config)
        finally:
            worker_db.close()

    try:
        await asyncio.to_thread(_run)
    except BackendConfigureError as exc:
        # 409 if it was a TOCTOU race against another configure attempt;
        # 500 for genuine bring-up failures.
        if "already configured" in str(exc):
            return Response(content=ErrorResponse(error=str(exc)), status_code=409)
        return Response(content=ErrorResponse(error=str(exc)), status_code=500)

    state = archive_backend.read_state(db)
    archive_dir = archive_backend.juicefs_mount_dir(config) if state.backend == "s3" else None
    meta_db_path = archive_backend.juicefs_meta_db_path(config)
    return Response(content=_state_to_response(state, archive_dir, meta_db_path, meta_dumps=None), status_code=200)


api_archive_backend_routes = Router(
    path="/",
    route_handlers=[get_archive_backend, test_connection, configure_archive_backend],
)
