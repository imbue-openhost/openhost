"""HTTP API for the operator-controlled archive backend."""

from __future__ import annotations

import asyncio
import re
import sqlite3

import attr
from litestar import MediaType
from litestar import Response
from litestar import Router
from litestar import get
from litestar import post

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


@attr.s(auto_attribs=True, frozen=True)
class TestS3ConnectionRequest:
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = ""


@attr.s(auto_attribs=True, frozen=True)
class ConfigureBackendRequest:
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_prefix: str = ""
    juicefs_volume_name: str = ""


def _state_to_response(
    state: BackendState, archive_dir: str | None, meta_db_path: str, meta_dumps: MetaDumpsSummary | None
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


@get("/api/storage/archive_backend", guards=[require_owner_auth])
async def get_archive_backend(db: sqlite3.Connection, config: Config) -> BackendStateResponse:
    """Return current archive-backend state (secret redacted) plus archive_dir, meta_db_path, meta_dumps."""
    state = archive_backend.read_state(db)
    archive_dir = archive_backend.juicefs_mount_dir(config) if state.backend == "s3" else None
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
            state.s3_prefix,
        )
        if summary is not None:
            meta_dumps = MetaDumpsSummary(
                count=summary.count,
                latest_at=summary.latest_at,
                latest_key=summary.latest_key,
            )
    return _state_to_response(state, archive_dir, meta_db_path, meta_dumps)


@post("/api/storage/archive_backend/test_connection", status_code=200, guards=[require_owner_auth])
async def test_connection(
    data: TestS3ConnectionRequest,
) -> Response[TestConnectionOk] | Response[TestConnectionError]:
    """Pre-flight S3 reachability/credentials check; doesn't touch the DB or live mount."""
    bucket = data.s3_bucket.strip()
    access_key = data.s3_access_key_id.strip()
    secret_key = data.s3_secret_access_key.strip()
    region = data.s3_region.strip() or None
    endpoint = data.s3_endpoint.strip() or None
    try:
        _normalise_s3_prefix(data.s3_prefix)
    except ValueError as exc:
        return Response(content=TestConnectionError(ok=False, error=f"invalid s3_prefix: {exc}"), status_code=400)
    if not (bucket and access_key and secret_key):
        return Response(
            content=TestConnectionError(ok=False, error="bucket, access_key_id, and secret_access_key are required"),
            status_code=400,
        )
    error = await asyncio.to_thread(
        archive_backend.test_s3_credentials,
        bucket,
        region,
        endpoint,
        access_key,
        secret_key,
    )
    if error:
        return Response(content=TestConnectionError(ok=False, error=error), status_code=400)
    return Response(content=TestConnectionOk(ok=True), status_code=200, media_type=MediaType.JSON)


@post("/api/storage/archive_backend/configure", status_code=200, guards=[require_owner_auth])
async def configure_archive_backend(
    data: ConfigureBackendRequest, db: sqlite3.Connection, config: Config
) -> Response[BackendStateResponse] | Response[ErrorResponse]:
    """One-shot configuration: ``backend='disabled'`` -> ``'s3'``.  No re-runs.

    Required: s3_bucket, s3_access_key_id, s3_secret_access_key.
    Optional: s3_region, s3_endpoint, s3_prefix, juicefs_volume_name.
    """
    state = archive_backend.read_state(db)
    if state.backend != "disabled":
        return Response(
            content=ErrorResponse(
                error=(
                    f"archive backend is already configured (backend={state.backend!r}); "
                    "reconfiguration is not supported"
                )
            ),
            status_code=409,
        )

    bucket = data.s3_bucket.strip()
    access_key = data.s3_access_key_id.strip()
    secret_key = data.s3_secret_access_key.strip()
    region = data.s3_region.strip() or None
    endpoint = data.s3_endpoint.strip() or None
    volume_name = data.juicefs_volume_name.strip() or None
    try:
        prefix = _normalise_s3_prefix(data.s3_prefix)
    except ValueError as exc:
        return Response(content=ErrorResponse(error=f"invalid s3_prefix: {exc}"), status_code=400)

    missing = []
    if not bucket:
        missing.append("s3_bucket")
    if not access_key:
        missing.append("s3_access_key_id")
    if not secret_key:
        missing.append("s3_secret_access_key")
    if missing:
        return Response(content=ErrorResponse(error=f"Missing required fields: {', '.join(missing)}"), status_code=400)

    # The format+mount steps can take 10-30s.  Run off-loop so the event
    # loop doesn't block.  ``db`` from ``provide_db()`` is request-thread-bound
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
