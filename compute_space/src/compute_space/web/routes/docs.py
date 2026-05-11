from __future__ import annotations

from pathlib import Path

from quart import Blueprint
from quart import redirect
from quart import send_from_directory
from quart.typing import ResponseReturnValue

from compute_space.config import get_config

# Serves the OpenHost manual at /docs/ (public; no auth).  The
# pre-rendered mdBook output lives in <repo>/docs/book/.  Routes
# fall back to a 503 with a build hint when the book hasn't been
# rendered yet (fresh `git clone` without `mdbook build`).
docs_bp = Blueprint("docs", __name__)


def _docs_book_dir() -> Path:
    """Return the absolute path to the rendered docs directory.

    Resolves via ``get_config().openhost_repo_path`` so tests can
    point the config at a fixture directory.
    """
    return get_config().openhost_repo_path / "docs" / "book"


def _missing_book_response() -> tuple[str, int]:
    """503 message used when ``docs/book/`` is missing or unbuilt."""
    return (
        "The docs have not been built on this OpenHost installation. "
        "From the imbue-openhost/openhost repo root, run: "
        "mdbook build docs/  (This usually happens automatically as "
        "part of the release build; see .github/workflows/docs.yml "
        "for the CI step.)",
        503,
    )


def _book_is_built(book_dir: Path) -> bool:
    """Sentinel check: do we have a usable rendered book?

    ``index.html`` is always present in a successful mdBook
    build, so its presence is the cleanest single-file
    indicator of "the build ran and produced output."  Used by
    both routes so the missing/empty-book diagnostic is
    consistent regardless of which URL the operator hits.
    """
    return (book_dir / "index.html").is_file()


@docs_bp.route("/docs/<path:filename>")
async def docs_file(filename: str) -> ResponseReturnValue:
    """Serve any file under ``docs/book/``.

    ``send_from_directory`` does the path-traversal check
    internally — anything resolving outside ``book_dir`` (via
    ``..`` segments or absolute paths) raises ``NotFound``.
    """
    book_dir = _docs_book_dir()
    if not _book_is_built(book_dir):
        return _missing_book_response()
    return await send_from_directory(book_dir, filename)


@docs_bp.route("/docs/")
async def docs_index() -> ResponseReturnValue:
    """Serve the docs landing page (``docs/book/index.html``).

    Delegates to ``docs_file("index.html")`` so the same guard +
    serve logic runs in one place.
    """
    return await docs_file("index.html")


@docs_bp.route("/docs")
async def docs_index_no_slash() -> ResponseReturnValue:
    """Redirect ``/docs`` → ``/docs/`` so relative URLs in the
    mdBook output (which are all relative to ``/docs/``) resolve
    correctly.  Without this redirect, a request to ``/docs``
    would serve the index but every ``<link href="css/...">``
    would resolve to ``/css/...`` (root), not ``/docs/css/...``.
    """
    return redirect("/docs/")
