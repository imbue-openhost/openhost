from __future__ import annotations

from pathlib import Path

import pytest
from quart import Quart

import compute_space.web.routes.docs as docs_routes
from compute_space.core.apps import RESERVED_PATHS
from compute_space.web.app import create_app

from .conftest import _make_test_config


class _FakeCfg:
    """Per-test config stub exposing only ``openhost_repo_path``.

    Used in place of mutating the real ``DefaultConfig`` class so
    every test gets an independent stub and no test leaks state
    into another.  Production code path
    (``compute_space.web.routes.docs._docs_book_dir``) reads via
    ``current_app.openhost_config.openhost_repo_path`` so an
    object that supplies that attribute is sufficient.
    """

    def __init__(self, openhost_repo_path: Path) -> None:
        self.openhost_repo_path = openhost_repo_path


def _make_app(repo_root_override: Path):  # noqa: ANN202
    """Build a minimal Quart app with the docs blueprint registered.

    ``repo_root_override`` controls what ``docs/book/`` resolves to
    — by pointing it at a fixture dir, tests choose whether the
    book is built, partially built, or entirely missing.
    """
    app = Quart(__name__)
    app.openhost_config = _FakeCfg(  # type: ignore[attr-defined]
        openhost_repo_path=repo_root_override,
    )
    app.register_blueprint(docs_routes.docs_bp)
    return app


def _populate_fake_book(book_dir: Path) -> None:
    """Drop a minimal valid mdBook output tree at ``book_dir``."""
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "index.html").write_text("<!doctype html><html><body><h1>OpenHost Manual</h1></body></html>")
    (book_dir / "manifest_spec.html").write_text("<!doctype html><html><body><h1>Manifest</h1></body></html>")
    css_dir = book_dir / "css"
    css_dir.mkdir(exist_ok=True)
    (css_dir / "general.css").write_text("body{font-family:sans-serif}")
    (book_dir / "searchindex.json").write_text('{"version": 1}')


@pytest.fixture
def app_with_built_docs(tmp_path: Path):
    """A Quart app pointing at a fake repo root that HAS docs/book/."""
    repo_root = tmp_path / "repo"
    book_dir = repo_root / "docs" / "book"
    _populate_fake_book(book_dir)
    return _make_app(repo_root_override=repo_root)


@pytest.fixture
def app_without_docs(tmp_path: Path):
    """A Quart app pointing at a fake repo root with NO docs/book/.

    Simulates someone running compute_space from a fresh git clone
    without having run ``mdbook build docs/`` first.
    """
    repo_root = tmp_path / "repo-no-docs"
    repo_root.mkdir()
    return _make_app(repo_root_override=repo_root)


# -- happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_index_serves_when_built(app_with_built_docs):
    client = app_with_built_docs.test_client()
    resp = await client.get("/docs/")
    assert resp.status_code == 200
    body = (await resp.get_data()).decode()
    assert "OpenHost Manual" in body
    # Content-Type should be HTML.
    assert "text/html" in resp.headers.get("Content-Type", "")


@pytest.mark.asyncio
async def test_chapter_serves(app_with_built_docs):
    client = app_with_built_docs.test_client()
    resp = await client.get("/docs/manifest_spec.html")
    assert resp.status_code == 200
    assert "Manifest" in (await resp.get_data()).decode()


@pytest.mark.asyncio
async def test_nested_static_asset_serves(app_with_built_docs):
    """Sub-directories of the book (CSS, fonts, JS) must serve too."""
    client = app_with_built_docs.test_client()
    resp = await client.get("/docs/css/general.css")
    assert resp.status_code == 200
    body = (await resp.get_data()).decode()
    assert "sans-serif" in body


@pytest.mark.asyncio
async def test_search_index_serves(app_with_built_docs):
    """The search index JSON powers mdBook's client-side search.

    If this isn't served correctly, the search box on the docs
    page silently returns no results — a UX regression that's
    easy to miss without an explicit test.
    """
    client = app_with_built_docs.test_client()
    resp = await client.get("/docs/searchindex.json")
    assert resp.status_code == 200


# -- redirects ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_slash_redirects_to_slash(app_with_built_docs):
    """``/docs`` must redirect to ``/docs/`` so relative URLs in the
    rendered HTML resolve correctly.  Without this, the index would
    load but every CSS / JS / chapter link would be broken.
    """
    client = app_with_built_docs.test_client()
    resp = await client.get("/docs", follow_redirects=False)
    assert resp.status_code in (301, 302, 308)
    assert resp.headers["Location"].endswith("/docs/")


# -- missing book ---------------------------------------------------


@pytest.mark.asyncio
async def test_missing_book_returns_503_with_hint(app_without_docs):
    """When ``docs/book/`` doesn't exist, the response must clearly
    explain *what* to do — not just 503, but a 503 that tells the
    operator to run ``mdbook build docs/``."""
    client = app_without_docs.test_client()
    resp = await client.get("/docs/")
    assert resp.status_code == 503
    body = (await resp.get_data()).decode()
    assert "mdbook build" in body


@pytest.mark.asyncio
async def test_missing_book_subpath_also_503(app_without_docs):
    """A request for ``/docs/manifest_spec.html`` against a missing
    book also returns the 503 hint, not a bare 404.  Helps the
    operator notice the misconfiguration whether they land on the
    index or jump straight to a chapter via bookmark."""
    client = app_without_docs.test_client()
    resp = await client.get("/docs/manifest_spec.html")
    assert resp.status_code == 503
    assert "mdbook build" in (await resp.get_data()).decode()


@pytest.mark.asyncio
async def test_empty_book_dir_returns_503_on_subpath(tmp_path: Path):
    """The book directory exists but is empty (e.g., the mdbook build
    started, failed, and left the dir behind).  Both index and chapter
    requests should return the same 503 + build hint so the operator
    gets a consistent diagnostic regardless of which URL they hit.
    """
    repo_root = tmp_path / "repo-empty-book"
    book_dir = repo_root / "docs" / "book"
    book_dir.mkdir(parents=True)
    # NOTE: don't write index.html — empty book dir.
    app = _make_app(repo_root_override=repo_root)
    client = app.test_client()

    resp = await client.get("/docs/")
    assert resp.status_code == 503
    assert "mdbook build" in (await resp.get_data()).decode()

    resp = await client.get("/docs/manifest_spec.html")
    assert resp.status_code == 503, (
        "subpath into an empty book dir should also return the 503 build hint, matching what /docs/ returns"
    )
    assert "mdbook build" in (await resp.get_data()).decode()


# -- path traversal -------------------------------------------------


@pytest.mark.asyncio
async def test_path_traversal_blocked(tmp_path: Path):
    """``send_from_directory`` must reject any path that escapes
    the book root via ``..`` segments.  We drop sensitive-looking
    files at every level above the book dir and confirm none
    of them are served — even after following any URL-normalisation
    redirects Quart's router may issue.

    The book and sentinel files share the SAME ``tmp_path`` tree
    (this test constructs the app itself rather than using the
    ``app_with_built_docs`` fixture) so traversal targets are
    guaranteed to be at real filesystem paths that ``..`` from
    the book root would reach.

    Book layout under tmp_path:
        tmp_path/repo/docs/book/index.html       ← book root
        tmp_path/repo/docs/secret-docs.txt       ← 1 up
        tmp_path/repo/secret-repo.txt            ← 2 up
        tmp_path/secret-tmp.txt                  ← 3 up

    Possible outcomes per evil_path, all of which are acceptable
    AS LONG AS NO sentinel ever appears in the final body:
      * 400 — the URL parser rejects the input
      * 404 — the route resolves but the file isn't found
      * 30x → eventual 404 — Quart normalises the path then
                              404s when the normalised path
                              doesn't exist
    """
    repo_root = tmp_path / "repo"
    book_dir = repo_root / "docs" / "book"
    _populate_fake_book(book_dir)
    app = _make_app(repo_root_override=repo_root)

    sentinels = {
        # path → unique sentinel string we'd see if the file
        # leaked into a response body.
        tmp_path / "repo" / "docs" / "secret-docs.txt": "ZONE-SECRET-DOCS-LEVEL",
        tmp_path / "repo" / "secret-repo.txt": "ZONE-SECRET-REPO-LEVEL",
        tmp_path / "secret-tmp.txt": "ZONE-SECRET-TMP-LEVEL",
    }
    for path, content in sentinels.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    client = app.test_client()
    # Note: the path-traversal segments below cover the three
    # depths above book/ corresponding to the three sentinels:
    #   ../  → 1 up (docs/)
    #   ../../  → 2 up (repo/)
    #   ../../../  → 3 up (tmp_path/)
    # plus several alternate encodings.
    for evil_path in (
        "/docs/../secret-docs.txt",
        "/docs/../../secret-repo.txt",
        "/docs/../../../secret-tmp.txt",
        "/docs/..%2Fsecret-docs.txt",
        "/docs/..%2F..%2Fsecret-repo.txt",
        "/docs/css/../../secret-docs.txt",
        "/docs/css/../../../secret-repo.txt",
        "/docs//../secret-docs.txt",
    ):
        resp = await client.get(evil_path, follow_redirects=True)
        body = (await resp.get_data()).decode()
        for sentinel in sentinels.values():
            assert sentinel not in body, f"PATH TRAVERSAL: {evil_path} leaked {sentinel}"
        # Final response must not be a 200 — a 200 would mean we
        # successfully read SOMETHING outside the book dir, which
        # we never want regardless of whether the body matched
        # one of our sentinels.
        assert resp.status_code != 200, (
            f"PATH TRAVERSAL: {evil_path} resolved to a 200 "
            f"(final status, contents not in sentinel set but "
            f"still served outside book dir)"
        )


# -- public access (no auth) ----------------------------------------


@pytest.mark.asyncio
async def test_no_auth_required(app_with_built_docs):
    """The docs are deliberately public so invited users / external
    readers can consult them.  We verify the route returns 200
    without any auth headers / cookies / session.
    """
    client = app_with_built_docs.test_client()
    resp = await client.get("/docs/")
    assert resp.status_code == 200
    # And the response body doesn't redirect to /login.
    body = (await resp.get_data()).decode()
    assert "/login" not in body


# -- reserved path entry --------------------------------------------


def test_docs_in_reserved_paths():
    """``/docs`` must be in ``RESERVED_PATHS`` so the deploy flow
    rejects an app literally named ``docs``.  If it isn't, a user
    could deploy a "docs" app and shadow the path-based fallback
    routing the dashboard uses, even though the subdomain route
    (docs.<zone>) would still work.  The two-line route registration
    in ``app.py`` is necessary but not sufficient on its own — this
    test catches the case where someone adds another ``RESERVED_PATHS``
    entry and accidentally removes ours via a merge.
    """
    assert "/docs" in RESERVED_PATHS, (
        "/docs must be in RESERVED_PATHS to prevent an app named "
        "'docs' from being deployed (would conflict with the built-in "
        "/docs route registered by compute_space.web.routes.docs)"
    )


# -- pre-setup access (full create_app) -----------------------------


@pytest.mark.asyncio
async def test_docs_accessible_before_owner_setup(tmp_path: Path):
    """The full ``create_app()`` application's ``_require_owner``
    before-request hook normally redirects every path to ``/setup``
    when the zone has no owner yet.  Docs must be exempt from this
    redirect because operators usually consult docs BEFORE finishing
    setup.  This is an integration test against a real
    ``create_app()`` instance with an empty owner table.

    Builds a self-contained book under tmp_path so the test is
    fully isolated from whatever ``docs/book/`` state the local
    repo might or might not have (e.g., a partial / stale local
    mdbook build that's missing manifest_spec.html).
    """
    cfg = _make_test_config(tmp_path, port=20600)
    # Override openhost_repo_path on this specific cfg instance
    # so the docs route resolves to OUR fake book, never the
    # repo-on-disk one.  We assign onto an instance dict (not
    # the class) so this doesn't leak across tests.  The Config
    # class uses @property, which can't be shadowed by setattr
    # on an instance, so we have to swap a per-instance
    # subclass instead.
    fake_repo = tmp_path / "fake-repo"
    _populate_fake_book(fake_repo / "docs" / "book")

    class _IsolatedCfg(type(cfg)):  # type: ignore[misc]
        @property
        def openhost_repo_path(self) -> Path:
            return fake_repo

    # Rebind cfg to an instance of the isolated subclass.  We
    # copy the original cfg's attrs over so the rest of the
    # config (db path, ports, etc.) is unchanged.
    isolated = _IsolatedCfg.__new__(_IsolatedCfg)
    isolated.__dict__.update(cfg.__dict__)
    isolated.make_all_dirs()

    app = create_app(isolated)
    client = app.test_client()

    # Confirm the owner table is empty (otherwise this test would
    # check the wrong code path).
    resp = await client.get("/")
    assert resp.status_code in (301, 302, 303, 307, 308), (
        f"expected pre-setup redirect from /; test fixture is misconfigured (got {resp.status_code})"
    )
    assert "/setup" in resp.headers.get("Location", "")

    # The actual assertion: /docs/ must NOT redirect.
    resp = await client.get("/docs/", follow_redirects=False)
    assert resp.status_code == 200, (
        f"/docs/ should return 200 pre-setup, got {resp.status_code} "
        f"(location: {resp.headers.get('Location', '<none>')})"
    )
    # And a chapter sub-path that we DEFINITELY put in our fake book:
    resp = await client.get("/docs/manifest_spec.html", follow_redirects=False)
    assert resp.status_code == 200, f"/docs/<chapter> should return 200 pre-setup, got {resp.status_code}"
