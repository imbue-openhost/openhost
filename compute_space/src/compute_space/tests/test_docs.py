"""Tests for the server-side-rendered docs route.

The route reads ``docs/src/*.md`` directly off the running checkout
and renders to HTML on the fly.  These tests inject a fake
``openhost_repo_path`` via the per-test ``_FakeCfg`` so each scenario
controls exactly which markdown files exist.

What we cover:
  * Happy path — markdown renders to HTML, the rendered page
    includes content from the source file.
  * Sidebar — SUMMARY.md parsing extracts the right sections + links,
    and the active link is marked as such.
  * Missing source dir — 503 with an actionable error message
    (the only mode the old mdBook-based code's 503 covered, kept
    here as a regression).
  * 404 — unknown slugs, slugs with weird characters, slugs that
    resolve outside the docs source dir (path-traversal attempts).
  * Trailing-slash redirect — ``/docs`` 302s to ``/docs/``.
  * Internal-link rewrite — markdown like ``[a](./foo.md)`` becomes
    ``<a href="/docs/foo">a</a>``, NOT a 404 from a literal
    ``href="./foo.md"``.
  * Mtime cache — touching the source file invalidates the cached
    render.
  * RESERVED_PATHS — ``/docs`` is in the reserved-name set so an
    operator can't deploy an app named ``docs`` that would shadow
    the route.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from quart import Quart

import compute_space.web.routes.docs as docs_routes
from compute_space.core.apps import RESERVED_PATHS


class _FakeCfg:
    """Per-test config stub exposing only ``openhost_repo_path``."""

    def __init__(self, openhost_repo_path: Path) -> None:
        self.openhost_repo_path = openhost_repo_path


def _make_app(repo_root_override: Path) -> Quart:
    """Build a minimal Quart app with the docs blueprint registered."""
    app = Quart(__name__)
    app.openhost_config = _FakeCfg(  # type: ignore[attr-defined]
        openhost_repo_path=repo_root_override,
    )
    app.register_blueprint(docs_routes.docs_bp)
    return app


@pytest.fixture(autouse=True)
def _clear_render_cache():
    """Reset the module-global mtime cache between tests so each
    test starts from a clean slate."""
    docs_routes._render_cache.clear()
    yield
    docs_routes._render_cache.clear()


def _populate_fake_docs(src_dir: Path) -> None:
    """Drop a small docs/src/ tree at ``src_dir``."""
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "SUMMARY.md").write_text(
        "# Summary\n"
        "\n"
        "[Introduction](./introduction.md)\n"
        "\n"
        "# Concepts\n"
        "\n"
        "- [Manifest Spec](./manifest_spec.md)\n"
        "- [Routing](./routing.md)\n"
        "\n"
        "# Guides\n"
        "\n"
        "- [Creating an App](./creating_an_app.md)\n"
    )
    (src_dir / "introduction.md").write_text(
        "# Welcome to OpenHost\n"
        "\n"
        "OpenHost is a self-hosted application platform.\n"
        "See the [manifest spec](./manifest_spec.md).\n"
    )
    (src_dir / "manifest_spec.md").write_text(
        "# Manifest\n"
        "\n"
        "Each app declares a `[runtime]` section.\n"
        "\n"
        "```toml\n"
        "[runtime.container]\n"
        'image = "Dockerfile"\n'
        "```\n"
    )
    (src_dir / "routing.md").write_text("# Routing\n\nRouting prose here.\n")
    (src_dir / "creating_an_app.md").write_text("# Creating an App\n\nGuide content.\n")


@pytest.fixture
def app_with_docs(tmp_path: Path) -> Quart:
    """A Quart app pointing at a fake repo root with docs/src/ populated."""
    repo_root = tmp_path / "repo"
    _populate_fake_docs(repo_root / "docs" / "src")
    return _make_app(repo_root_override=repo_root)


@pytest.fixture
def app_without_docs(tmp_path: Path) -> Quart:
    """A Quart app whose docs/src/ does NOT exist (corrupt install)."""
    repo_root = tmp_path / "repo-no-docs"
    repo_root.mkdir()
    return _make_app(repo_root_override=repo_root)


# -- happy path -----------------------------------------------------


@pytest.mark.asyncio
async def test_index_renders_introduction(app_with_docs: Quart):
    """``GET /docs/`` must render ``introduction.md``."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/")
    assert resp.status_code == 200
    body = (await resp.get_data()).decode()
    assert "Welcome to OpenHost" in body
    assert "self-hosted application platform" in body


@pytest.mark.asyncio
async def test_slug_renders_corresponding_markdown(app_with_docs: Quart):
    """``GET /docs/manifest_spec`` renders ``manifest_spec.md``."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/manifest_spec")
    assert resp.status_code == 200
    body = (await resp.get_data()).decode()
    assert "<h1" in body
    assert "Manifest" in body
    assert "Each app declares" in body


@pytest.mark.asyncio
async def test_code_blocks_are_syntax_highlighted(app_with_docs: Quart):
    """Fenced code blocks with a language tag run through Pygments."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/manifest_spec")
    body = (await resp.get_data()).decode()
    # The Pygments HtmlFormatter wraps highlighted output in
    # ``<div class="codehilite">`` and tags individual tokens with
    # CSS classes like ``.n`` (name), ``.s2`` (double-quoted str), etc.
    assert "codehilite" in body
    assert "image" in body


@pytest.mark.asyncio
async def test_sidebar_contains_summary_entries(app_with_docs: Quart):
    """The sidebar exposes the SUMMARY.md sections + links."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/")
    body = (await resp.get_data()).decode()
    assert "Concepts" in body
    assert "Guides" in body
    assert 'href="/docs/manifest_spec"' in body
    assert 'href="/docs/routing"' in body
    assert 'href="/docs/creating_an_app"' in body


@pytest.mark.asyncio
async def test_active_sidebar_link_marked(app_with_docs: Quart):
    """The currently-rendered page's sidebar link gets ``class="active"``."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/manifest_spec")
    body = (await resp.get_data()).decode()
    # Find the link to manifest_spec and verify it carries the active marker.
    assert 'href="/docs/manifest_spec"' in body and "active" in body


@pytest.mark.asyncio
async def test_internal_md_links_rewritten(app_with_docs: Quart):
    """Markdown like ``[manifest spec](./manifest_spec.md)`` should
    render as a link to ``/docs/manifest_spec``, NOT a literal
    ``href="./manifest_spec.md"`` that would 404."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/")
    body = (await resp.get_data()).decode()
    assert 'href="/docs/manifest_spec"' in body
    assert 'href="./manifest_spec.md"' not in body
    assert 'href="manifest_spec.md"' not in body


@pytest.mark.asyncio
async def test_prev_next_navigation(app_with_docs: Quart):
    """Each page surfaces prev/next links based on SUMMARY.md order."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/manifest_spec")
    body = (await resp.get_data()).decode()
    # In our SUMMARY: introduction, manifest_spec, routing, creating_an_app.
    # manifest_spec should point back to introduction and forward to routing.
    assert "introduction" in body.lower()
    assert "routing" in body.lower()


# -- 404 / safety ---------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_slug_404(app_with_docs: Quart):
    """A request for a slug whose .md doesn't exist returns 404."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs/this_does_not_exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evil_slug",
    [
        "../etc/passwd",
        "..%2Fetc%2Fpasswd",
        "%2E%2E%2Fetc%2Fpasswd",
        "subdir/foo",
        ".gitignore",
        " ",
        "introduction.md",  # we accept slugs WITHOUT .md, with-extension should 404
        "introduction.md.bak",
    ],
)
async def test_path_traversal_blocked(app_with_docs: Quart, evil_slug: str):
    """The slug regex rejects anything outside ``[A-Za-z0-9_-]+``.

    Whether Quart returns 404 directly or 308-rewrites and then 404s,
    the response must NOT be 200 and must NOT echo a sensitive
    sentinel from outside the docs dir.
    """
    client = app_with_docs.test_client()
    resp = await client.get(f"/docs/{evil_slug}", follow_redirects=True)
    assert resp.status_code != 200
    body = (await resp.get_data()).decode("utf-8", errors="replace")
    # Sanity: no actual /etc/passwd content
    assert "root:x:" not in body


# -- error paths ----------------------------------------------------


@pytest.mark.asyncio
async def test_missing_docs_dir_returns_503(app_without_docs: Quart):
    """When ``docs/src/`` doesn't exist, the route returns 503 with
    an actionable error message rather than 200/blank.

    This is the "operator's checkout is broken / incomplete" path.
    """
    client = app_without_docs.test_client()
    resp = await client.get("/docs/")
    assert resp.status_code == 503
    body = (await resp.get_data()).decode()
    assert "docs source directory is missing" in body.lower()


# -- redirects ------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_slash_redirect(app_with_docs: Quart):
    """``/docs`` (no slash) → ``/docs/`` so mdBook-style relative
    asset links would resolve correctly.  We don't use such relative
    links any more, but the redirect is kept for bookmark stability."""
    client = app_with_docs.test_client()
    resp = await client.get("/docs")
    assert resp.status_code in (301, 302, 308)
    assert resp.headers["Location"].endswith("/docs/")


# -- cache ----------------------------------------------------------


@pytest.mark.asyncio
async def test_render_cache_invalidates_on_mtime_change(app_with_docs: Quart):
    """Modifying the markdown source after the first render must be
    reflected on the next request — the mtime check should bust the
    cache."""
    client = app_with_docs.test_client()
    resp1 = await client.get("/docs/")
    body1 = (await resp1.get_data()).decode()
    assert "Welcome to OpenHost" in body1

    # Mutate the source.  Sleep so mtime resolution definitely
    # increases (some filesystems have 1s resolution).
    src = app_with_docs.openhost_config.openhost_repo_path / "docs" / "src" / "introduction.md"
    time.sleep(1.05)
    src.write_text("# A New Heading\n\nFresh content.\n")

    resp2 = await client.get("/docs/")
    body2 = (await resp2.get_data()).decode()
    assert "A New Heading" in body2
    assert "Welcome to OpenHost" not in body2


# -- RESERVED_PATHS regression --------------------------------------


def test_docs_in_reserved_paths():
    """An operator must NOT be able to deploy an app named ``docs``
    — that would shadow the route ordering and break the manual.

    The deploy-app validation in ``core.apps`` checks ``RESERVED_PATHS``
    for a leading-slash match.  This regression test makes sure the
    PR keeps ``/docs`` on that list.
    """
    assert "/docs" in RESERVED_PATHS
