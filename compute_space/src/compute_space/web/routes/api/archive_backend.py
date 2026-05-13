import asyncio
import re
import sqlite3
from typing import Annotated
from typing import Any

import attr
from litestar import Response
from litestar import get
from litestar import post
from litestar.enums import RequestEncodingType
from litestar.params import Body

from compute_space.config import get_config
from compute_space.core import archive_backend
from compute_space.core.archive_backend import BackendConfigureError
from compute_space.core.archive_backend import BackendState
from compute_space.db import get_db


@attr.s(auto_attribs=True, frozen=True)
class ArchiveBackendForm:
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_prefix: str = ""
    juicefs_volume_name: str = ""


def _state_to_response(state: BackendState) -> dict[str, Any]:
    out = attr.asdict(state)
    out.pop("s3_secret_access_key", None)
    return out


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


@get("/api/storage/archive_backend")
async def get_archive_backend(user: dict[str, Any]) -> dict[str, Any]:
    config = get_config()
    db = get_db()
    state = archive_backend.read_state(db)
    response: dict[str, Any] = {
        **_state_to_response(state),
        "archive_dir": archive_backend.juicefs_mount_dir(config) if state.backend == "s3" else None,
        "meta_db_path": archive_backend.juicefs_meta_db_path(config),
        "meta_dumps": None,
    }
    if state.backend == "s3" and state.s3_bucket and state.s3_access_key_id and state.s3_secret_access_key:
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
    return response


@post("/api/storage/archive_backend/test_connection", status_code=200)
async def test_connection(
    data: Annotated[ArchiveBackendForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> Response[dict[str, Any]]:
    bucket = (data.s3_bucket or "").strip()
    region = (data.s3_region or "").strip() or None
    endpoint = (data.s3_endpoint or "").strip() or None
    access_key = (data.s3_access_key_id or "").strip()
    secret_key = (data.s3_secret_access_key or "").strip()
    try:
        _normalise_s3_prefix(data.s3_prefix or None)
    except ValueError as exc:
        return Response(content={"ok": False, "error": f"invalid s3_prefix: {exc}"}, status_code=400)
    if not (bucket and access_key and secret_key):
        return Response(
            content={"ok": False, "error": "bucket, access_key_id, and secret_access_key are required"},
            status_code=400,
        )
    error = await asyncio.to_thread(
        archive_backend.test_s3_credentials, bucket, region, endpoint, access_key, secret_key
    )
    if error:
        return Response(content={"ok": False, "error": error}, status_code=400)
    return Response(content={"ok": True})


@post("/api/storage/archive_backend/configure", status_code=200)
async def configure_archive_backend(
    data: Annotated[ArchiveBackendForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
    user: dict[str, Any],
) -> Response[dict[str, Any]]:
    config = get_config()
    db = get_db()
    state = archive_backend.read_state(db)
    if state.backend != "disabled":
        return Response(
            content={
                "error": (
                    f"archive backend is already configured (backend={state.backend!r}); "
                    "reconfiguration is not supported"
                )
            },
            status_code=409,
        )

    bucket = (data.s3_bucket or "").strip()
    region = (data.s3_region or "").strip() or None
    endpoint = (data.s3_endpoint or "").strip() or None
    access_key = (data.s3_access_key_id or "").strip()
    secret_key = (data.s3_secret_access_key or "").strip()
    volume_name = (data.juicefs_volume_name or "").strip() or None
    try:
        prefix = _normalise_s3_prefix(data.s3_prefix or None)
    except ValueError as exc:
        return Response(content={"error": f"invalid s3_prefix: {exc}"}, status_code=400)

    missing = []
    if not bucket:
        missing.append("s3_bucket")
    if not access_key:
        missing.append("s3_access_key_id")
    if not secret_key:
        missing.append("s3_secret_access_key")
    if missing:
        return Response(content={"error": f"Missing required fields: {', '.join(missing)}"}, status_code=400)

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
        if "already configured" in str(exc):
            return Response(content={"error": str(exc)}, status_code=409)
        return Response(content={"error": str(exc)}, status_code=500)

    state = archive_backend.read_state(db)
    return Response(content=_state_to_response(state))


api_archive_backend_routes = [get_archive_backend, test_connection, configure_archive_backend]
