from __future__ import annotations

import attr


@attr.s(auto_attribs=True, frozen=True)
class FetchResult:
    state: str


@attr.s(auto_attribs=True, frozen=True)
class DiffCommit:
    sha: str
    message: str


@attr.s(auto_attribs=True, frozen=True)
class DiffResult:
    commits: list[DiffCommit]
    current_ref: str
    remote_ref: str | None


@attr.s(auto_attribs=True, frozen=True)
class RemoteInfo:
    url: str | None
    ref: str


@attr.s(auto_attribs=True, frozen=True)
class MigrationStatus:
    ok: bool
    reason: str
    message: str
    current_host_version: int
    expected_version: int
