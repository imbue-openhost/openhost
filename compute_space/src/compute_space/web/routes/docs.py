"""Server-side-rendered markdown route serving the OpenHost manual.

We read ``docs/src/*.md`` directly off the running checkout, render
through ``markdown-it-py`` + ``pygments``, and inject into a Jinja
template that matches the OpenHost dashboard's visual language.
There is no build step: ``git pull`` is enough to ship doc changes.

Why server-side render instead of mdBook (or any other static-site
generator)?

  * **No build step.**  ``docs/book/`` and ``mdbook`` binary disappear
    entirely.  The route reads the same ``docs/src/*.md`` files
    that show up in ``git diff``, so the docs an operator sees on
    ``/docs/`` are exactly what's in the commit compute_space is
    running.  This eliminates a whole class of bugs where the
    rendered HTML is stale relative to the running code (operator
    forgot to run ``mdbook build`` after ``git pull``, CI artifact
    is from a different commit than the running version, etc.).
  * **Smaller surface area.**  No ~5 MB Rust binary on every
    instance, no CI workflow to maintain, no ``book.toml`` to keep
    in sync, no theme/CSS-override directory.  Markdown rendering
    is pure-Python and pulls in ``markdown-it-py`` (already a
    transitive dep) plus ``mdit-py-plugins`` and ``pygments``
    (also already present via test deps).
  * **Easier to extend.**  Custom rendering — admonitions, mermaid
    diagrams, cross-references — becomes plain Python rather than
    mdBook preprocessor plugins.
  * **Reasonable performance.**  Each page renders in <20 ms on
    cold cache; subsequent hits are mtime-cached.  Our corpus is
    eight markdown files, so the cache footprint is trivial.

What we lose vs. mdBook (and why it doesn't matter much here):

  * **Client-side full-text search.**  We don't ship a search box.
    Browser Ctrl-F per page is sufficient for an 8-page manual.
    If the corpus grows to 50+ pages we can revisit by building
    a JSON search index at startup; the rendering pipeline already
    has parsed-AST access for free.
  * **Theme picker.**  Always renders in the dashboard's light
    palette + a ``prefers-color-scheme: dark`` media query.  No
    runtime theme switcher.

Path-traversal safety: ``_resolve_doc_path`` requires the resolved
absolute path to live under ``docs/src/`` and refuses anything else.
This is the only security-sensitive surface in the route.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt
from markupsafe import escape as html_escape
from mdit_py_plugins.anchors import anchors_plugin
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from quart import Blueprint
from quart import abort
from quart import redirect
from quart import render_template_string
from quart.typing import ResponseReturnValue

from compute_space.config import get_config

docs_bp = Blueprint("docs", __name__)


# ─── Filesystem layout ──────────────────────────────────────────────


def _docs_src_dir() -> Path:
    """Where the markdown sources live.

    Resolves via ``get_config().openhost_repo_path`` so tests can
    inject a fixture directory by overriding the config.
    """
    return get_config().openhost_repo_path / "docs" / "src"


_DEFAULT_INDEX = "introduction"
_SUMMARY_FILENAME = "SUMMARY.md"


# ─── Markdown engine ────────────────────────────────────────────────


def _build_md() -> MarkdownIt:
    """Construct the shared markdown renderer.

    Configured with:
      * ``commonmark`` baseline + GFM-style tables/strikethrough/
        autolinks (via the ``gfm-like`` preset).
      * ``anchors_plugin`` to auto-link ``<h2>``/``<h3>`` headings
        for deep links from the sidebar / external linkers.
      * ``tasklists_plugin`` so ``- [x]`` markdown checklists
        render as proper checkboxes.
      * ``html=False`` so embedded raw HTML in the markdown
        sources is treated as plain text rather than executed
        (defence-in-depth; nothing in our docs uses raw HTML).
      * A custom code-fence renderer that runs the contents
        through Pygments for syntax highlighting.

    The renderer is stateless and thread-safe — a single shared
    instance per process suffices.
    """
    # ``linkify=False``: bare URLs in the prose are NOT auto-linked.
    # The alternative requires the optional ``linkify-it-py`` dep,
    # and our manual already wraps its handful of URLs in proper
    # ``[text](url)`` syntax — auto-linking would just bring in a
    # new transitive dep for negligible UX gain.
    md = MarkdownIt("gfm-like", {"html": False, "linkify": False, "typographer": True})
    md.use(anchors_plugin, max_level=4, permalink=False)
    md.use(tasklists_plugin, enabled=True)
    md.add_render_rule("fence", _render_fence_with_pygments)
    return md


def _render_fence_with_pygments(
    self: object,  # noqa: ARG001 — markdown-it's render-rule signature passes the renderer as the first arg; we don't need it.
    tokens: list[object],
    idx: int,
    options: dict[str, object],  # noqa: ARG001
    env: object,  # noqa: ARG001
) -> str:
    """Render a ```fenced``` code block through Pygments.

    The info string after ``` becomes the lexer name (e.g. ``toml``,
    ``python``, ``bash``).  Unknown lexers fall back to no
    highlighting — we emit a plain ``<pre><code>...</code></pre>``
    that mdBook-style theme CSS can colour as a generic block.

    The render-rule signature is fixed by markdown-it-py's plugin
    protocol; we ignore the ``self``, ``options``, and ``env``
    args (the only inputs we need are the token list + index).
    """
    token = tokens[idx]
    code = getattr(token, "content", "")
    info = (getattr(token, "info", "") or "").strip().split(None, 1)
    lang = info[0] if info else ""
    if lang:
        try:
            lexer = get_lexer_by_name(lang, stripall=False)
        except ClassNotFound:
            lexer = None
    else:
        lexer = None
    if lexer is None:
        # No language tag (or unrecognised) — emit a plain block.
        # ``html_escape`` (markupsafe) prevents the code contents
        # from being interpreted as HTML markup.
        return f"<pre><code>{html_escape(code)}</code></pre>\n"
    formatter = HtmlFormatter(nowrap=False, cssclass="codehilite")
    # ``pygments.highlight`` is typed ``Any`` upstream; explicit
    # cast keeps mypy strict-no-any-return happy.
    rendered: str = highlight(code, lexer, formatter)
    return rendered


# Pygments CSS — embedded in the response so we don't need a
# separate route for it.  Uses the same colour palette
# (light) as the dashboard's .log-output panel.
PYGMENTS_CSS = HtmlFormatter(style="default").get_style_defs(".codehilite")


_MD = _build_md()


# ─── Sidebar / SUMMARY.md parsing ───────────────────────────────────


@dataclass(frozen=True)
class _SidebarLink:
    """One entry in the rendered sidebar."""

    title: str
    slug: str  # filename without .md, e.g. "manifest_spec"


@dataclass(frozen=True)
class _SidebarSection:
    """A header + the links beneath it."""

    title: str  # may be empty (for the unsectioned intro entry)
    links: tuple[_SidebarLink, ...]


_SUMMARY_LINE_RE = re.compile(r"^\s*[-*]\s+\[(?P<title>[^\]]+)\]\((?P<href>[^)]+)\)\s*$")
_SUMMARY_INTRO_RE = re.compile(r"^\s*\[(?P<title>[^\]]+)\]\((?P<href>[^)]+)\)\s*$")
_SUMMARY_HEADER_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$")


def _parse_summary(summary_text: str) -> tuple[_SidebarSection, ...]:
    """Parse ``SUMMARY.md`` into a list of sections.

    Recognised line shapes:
      ``# Title``             → starts a new section
      ``[Foo](./foo.md)``     → unsectioned intro link
      ``- [Foo](./foo.md)``   → link inside the current section

    Everything else (blank lines, comments, etc.) is ignored.
    Slugs are derived by stripping the leading ``./`` and trailing
    ``.md`` from the link target.  Links pointing at anything other
    than a sibling ``.md`` file are dropped — we don't try to
    follow external URLs from the sidebar.
    """
    sections: list[_SidebarSection] = []
    intro_links: list[_SidebarLink] = []
    current_title: str | None = None
    current_links: list[_SidebarLink] = []

    def _flush() -> None:
        if current_title is None and not current_links:
            return
        if current_title is None:
            return  # intro entries handled below
        sections.append(_SidebarSection(title=current_title, links=tuple(current_links)))

    for raw_line in summary_text.splitlines():
        line = raw_line.rstrip()
        if (
            not line.strip()
            or line.lstrip().startswith("#")
            and line.lstrip().lstrip("#").strip().lower() == "summary"
        ):
            continue
        m_header = _SUMMARY_HEADER_RE.match(line)
        if m_header:
            _flush()
            current_title = m_header.group("title")
            current_links = []
            continue
        m_link = _SUMMARY_LINE_RE.match(line)
        if m_link:
            slug = _slug_from_href(m_link.group("href"))
            if slug:
                current_links.append(_SidebarLink(title=m_link.group("title"), slug=slug))
            continue
        m_intro = _SUMMARY_INTRO_RE.match(line)
        if m_intro and current_title is None:
            slug = _slug_from_href(m_intro.group("href"))
            if slug:
                intro_links.append(_SidebarLink(title=m_intro.group("title"), slug=slug))
            continue

    _flush()
    # Intro section (unsectioned links at the top) prepends to the list.
    if intro_links:
        sections.insert(0, _SidebarSection(title="", links=tuple(intro_links)))
    return tuple(sections)


def _slug_from_href(href: str) -> str | None:
    """Convert a SUMMARY.md href like ``./manifest_spec.md`` into a
    slug like ``manifest_spec``.  Returns None for anything that's
    not a sibling .md file (e.g. external URLs)."""
    href = href.strip()
    if "://" in href or href.startswith("/"):
        return None
    if href.startswith("./"):
        href = href[2:]
    if not href.endswith(".md"):
        return None
    name = href[:-3]
    # Reject anything with a directory component — our docs are flat.
    if "/" in name:
        return None
    return name


# ─── Mtime-keyed render cache ───────────────────────────────────────


_render_cache_lock = threading.Lock()
_render_cache: dict[str, tuple[float, str]] = {}


def _cached_render(slug: str, path: Path) -> str:
    """Render ``path`` to HTML, caching by file mtime so we don't
    re-render unchanged files on every request.

    The cache is process-local and bounded by the number of doc
    files (~tens).  We use mtime rather than a content hash because
    the docs source is on local disk under our control — mtime is
    free, accurate, and survives file replacements via rename.
    """
    mtime = path.stat().st_mtime
    with _render_cache_lock:
        cached = _render_cache.get(slug)
        if cached and cached[0] == mtime:
            return cached[1]
    html = _MD.render(path.read_text(encoding="utf-8"))
    html = _rewrite_internal_links(html)
    with _render_cache_lock:
        _render_cache[slug] = (mtime, html)
    return html


def _rewrite_internal_links(html: str) -> str:
    """Rewrite ``href="./foo.md"`` (and ``foo.md``) in rendered
    HTML to point at our route paths (``/docs/foo``) instead.

    Without this rewrite the sibling-page links inside the
    rendered markdown would 404 — markdown-it doesn't know about
    our URL scheme, so a link in ``introduction.md`` written as
    ``[manifest spec](./manifest_spec.md)`` would emit
    ``href="./manifest_spec.md"`` verbatim.

    We only rewrite hrefs that look like flat ``.md`` files (no
    ``://`` schemes, no leading ``/``).  External links and
    in-page fragments (``#section``) are left alone.
    """

    def _repl(match: re.Match[str]) -> str:
        href = match.group(1)
        anchor = ""
        if "#" in href:
            href, _, anchor = href.partition("#")
            anchor = "#" + anchor
        if "://" in href or href.startswith("/") or not href.endswith(".md"):
            return match.group(0)
        if href.startswith("./"):
            href = href[2:]
        slug = href[:-3]
        if "/" in slug:
            return match.group(0)
        return f'href="/docs/{slug}{anchor}"'

    return re.sub(r'href="([^"]+)"', _repl, html)


# ─── Path resolution + safety ───────────────────────────────────────


_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_doc_path(slug: str) -> Path:
    """Resolve a sidebar slug to an absolute filesystem path,
    refusing anything that doesn't live under ``docs/src/``.

    The slug regex (``[A-Za-z0-9_-]+``) is already strict enough
    to reject anything pathological, but we still resolve the
    final path and assert it's under the docs dir as
    defence in depth.
    """
    if not _SLUG_RE.match(slug):
        abort(404)
    src = _docs_src_dir()
    candidate = (src / f"{slug}.md").resolve()
    try:
        candidate.relative_to(src.resolve())
    except ValueError:
        abort(404)
    if not candidate.is_file():
        abort(404)
    return candidate


# ─── HTML template ──────────────────────────────────────────────────

# Inline Jinja template.  Kept in this module rather than a
# separate file so the docs feature is self-contained: one .py file
# is the entire serving surface.  The CSS matches the dashboard's
# system-font stack, #36c accent, #ddd borders, #f5f5f5 muted bg.
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ page_title }} - OpenHost Manual</title>
  <style>
    :root {
      --bg: #ffffff;
      --fg: #222222;
      --muted: #666666;
      --border: #dddddd;
      --sidebar-bg: #fafafa;
      --sidebar-active: #3366cc;
      --table-header: #f5f5f5;
      --link: #3366cc;
      --code-bg: #1e1e1e;
      --code-fg: #d4d4d4;
      --inline-code-bg: #f5f5f5;
      --inline-code-fg: #c0392b;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #1e1e1e;
        --fg: #d4d4d4;
        --muted: #888888;
        --border: #444444;
        --sidebar-bg: #181818;
        --sidebar-active: #6ea8e6;
        --table-header: #2a2a2a;
        --link: #6ea8e6;
        --code-bg: #161616;
        --code-fg: #d4d4d4;
        --inline-code-bg: #2a2a2a;
        --inline-code-fg: #e89999;
      }
    }
    body {
      font-family: -apple-system, system-ui, BlinkMacSystemFont, "Segoe UI",
                   Roboto, "Helvetica Neue", Arial, sans-serif;
      color: var(--fg);
      background: var(--bg);
      margin: 0;
      line-height: 1.55;
    }
    .layout {
      display: flex;
      max-width: 1200px;
      margin: 0 auto;
      min-height: 100vh;
    }
    aside.sidebar {
      width: 240px;
      flex-shrink: 0;
      background: var(--sidebar-bg);
      border-right: 1px solid var(--border);
      padding: 1.5em 1em;
      box-sizing: border-box;
      font-size: 0.95em;
    }
    aside.sidebar h1 {
      font-size: 1.05em;
      margin: 0 0 1em;
      padding: 0;
    }
    aside.sidebar h1 a { color: var(--fg); text-decoration: none; }
    aside.sidebar .section { margin: 1.2em 0; }
    aside.sidebar .section-title {
      font-size: 0.8em;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin: 0 0 0.4em;
    }
    aside.sidebar ul { list-style: none; margin: 0; padding: 0; }
    aside.sidebar li { margin: 0.25em 0; }
    aside.sidebar li a {
      color: var(--fg);
      text-decoration: none;
      display: block;
      padding: 0.2em 0.4em;
      border-radius: 3px;
    }
    aside.sidebar li a:hover { background: rgba(0,0,0,0.04); }
    aside.sidebar li a.active {
      color: var(--sidebar-active);
      font-weight: 600;
      background: rgba(51,102,204,0.08);
    }
    main.content {
      flex: 1;
      padding: 2em 2.5em;
      box-sizing: border-box;
      min-width: 0;
    }
    main.content h1 { margin-top: 0; font-size: 1.7em; }
    main.content h2 { margin-top: 2em; font-size: 1.3em; }
    main.content h3 { margin-top: 1.5em; font-size: 1.1em; }
    main.content a { color: var(--link); text-decoration: none; }
    main.content a:hover { text-decoration: underline; }
    main.content table {
      border-collapse: collapse;
      width: 100%;
      margin: 1em 0;
    }
    main.content th, main.content td {
      border: 1px solid var(--border);
      padding: 0.5em 0.8em;
      text-align: left;
    }
    main.content th { background: var(--table-header); font-weight: 600; }
    main.content blockquote {
      margin: 1em 0;
      padding: 0.4em 1em;
      border-left: 4px solid var(--border);
      background: var(--sidebar-bg);
      color: var(--muted);
    }
    main.content code {
      background: var(--inline-code-bg);
      color: var(--inline-code-fg);
      padding: 0.1em 0.35em;
      border-radius: 3px;
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.9em;
    }
    main.content pre {
      background: var(--code-bg);
      color: var(--code-fg);
      padding: 1em;
      border-radius: 4px;
      overflow-x: auto;
      font-size: 0.85em;
      line-height: 1.45;
    }
    main.content pre code, main.content pre .codehilite {
      background: transparent;
      color: inherit;
      padding: 0;
      border-radius: 0;
      font-size: 1em;
    }
    main.content .codehilite pre { background: transparent; padding: 0; }
    main.content ul, main.content ol { padding-left: 1.5em; }
    main.content hr { border: 0; border-top: 1px solid var(--border); margin: 2em 0; }
    main.content img { max-width: 100%; }
    .footer-nav {
      display: flex;
      justify-content: space-between;
      margin-top: 3em;
      padding-top: 1em;
      border-top: 1px solid var(--border);
      font-size: 0.9em;
    }
    .footer-nav a { color: var(--link); text-decoration: none; }
    {{ pygments_css }}
  </style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <h1><a href="/docs/">OpenHost Manual</a></h1>
      {% for section in sections %}
        <div class="section">
          {% if section.title %}<div class="section-title">{{ section.title }}</div>{% endif %}
          <ul>
            {% for link in section.links %}
              <li><a href="/docs/{{ link.slug }}"
                     {% if link.slug == current_slug %}class="active"{% endif %}>{{ link.title }}</a></li>
            {% endfor %}
          </ul>
        </div>
      {% endfor %}
    </aside>
    <main class="content">
      {{ content_html | safe }}
      {% if prev_link or next_link %}
        <div class="footer-nav">
          <div>{% if prev_link %}← <a href="/docs/{{ prev_link.slug }}">{{ prev_link.title }}</a>{% endif %}</div>
          <div>{% if next_link %}<a href="/docs/{{ next_link.slug }}">{{ next_link.title }}</a> →{% endif %}</div>
        </div>
      {% endif %}
    </main>
  </div>
</body>
</html>
"""


def _flatten_links(sections: tuple[_SidebarSection, ...]) -> list[_SidebarLink]:
    """Flatten the sidebar into a single ordered list — used for
    prev/next navigation at the bottom of each page."""
    out: list[_SidebarLink] = []
    for section in sections:
        out.extend(section.links)
    return out


def _find_neighbors(slug: str, ordered: list[_SidebarLink]) -> tuple[_SidebarLink | None, _SidebarLink | None]:
    """Locate ``slug`` in ``ordered`` and return its (prev, next)
    siblings.  Both may be ``None`` (first or last page)."""
    for i, link in enumerate(ordered):
        if link.slug == slug:
            prev_l = ordered[i - 1] if i > 0 else None
            next_l = ordered[i + 1] if i < len(ordered) - 1 else None
            return prev_l, next_l
    return None, None


def _read_summary() -> tuple[_SidebarSection, ...]:
    """Read + parse ``SUMMARY.md`` if present.  Falls back to a
    single ungrouped list of every ``.md`` file in ``docs/src/``
    when the SUMMARY file is missing (e.g. tests that drop fixture
    markdown in directly)."""
    src = _docs_src_dir()
    summary_path = src / _SUMMARY_FILENAME
    if summary_path.is_file():
        return _parse_summary(summary_path.read_text(encoding="utf-8"))
    # Fallback: list every .md alphabetically.
    links = []
    for p in sorted(src.glob("*.md")):
        slug = p.stem
        if slug == _SUMMARY_FILENAME.removesuffix(".md"):
            continue
        # Use the first-line "# Title" if present, otherwise the slug.
        first_line = p.read_text(encoding="utf-8").splitlines()[0:1]
        title = (
            first_line[0].lstrip("# ").strip()
            if first_line and first_line[0].startswith("# ")
            else slug.replace("_", " ").title()
        )
        links.append(_SidebarLink(title=title, slug=slug))
    return (_SidebarSection(title="", links=tuple(links)),)


def _page_title(slug: str, path: Path) -> str:
    """Use the page's first ``# H1`` as the title, falling back to
    the slug with underscores converted to spaces."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return slug.replace("_", " ").title()


# ─── Routes ─────────────────────────────────────────────────────────


@docs_bp.route("/docs")
async def docs_index_no_slash() -> ResponseReturnValue:
    """Redirect ``/docs`` to ``/docs/`` so relative links and
    bookmarks behave consistently."""
    return redirect("/docs/")


@docs_bp.route("/docs/")
async def docs_index() -> ResponseReturnValue:
    """The docs landing page, served from ``introduction.md``.

    Falls back to a 503 (with a clear, operator-actionable
    message) if the markdown source dir is missing — that
    shouldn't happen in production but is what tests use to
    verify the missing-dir path.
    """
    return await _render_doc(_DEFAULT_INDEX)


@docs_bp.route("/docs/<slug>")
async def docs_slug(slug: str) -> ResponseReturnValue:
    """Serve ``docs/src/<slug>.md`` rendered to HTML.

    ``slug`` is the markdown filename without extension.  Anything
    not matching the slug regex (alphanumerics + ``-`` + ``_``)
    returns a 404 — protects against path traversal, weird unicode,
    and the implicit ``./``/``../`` shenanigans.
    """
    return await _render_doc(slug)


async def _render_doc(slug: str) -> ResponseReturnValue:
    src = _docs_src_dir()
    if not src.is_dir():
        return (
            "The OpenHost docs source directory is missing on this installation. "
            f"Expected: {src}.  This usually means the OpenHost code checkout is "
            "incomplete; reinstalling the openhost service should fix it.",
            503,
        )
    path = _resolve_doc_path(slug)
    sections = _read_summary()
    content_html = _cached_render(slug, path)
    ordered = _flatten_links(sections)
    prev_l, next_l = _find_neighbors(slug, ordered)
    page_title = _page_title(slug, path)
    return await render_template_string(
        _TEMPLATE,
        sections=sections,
        current_slug=slug,
        content_html=content_html,
        page_title=page_title,
        prev_link=prev_l,
        next_link=next_l,
        pygments_css=PYGMENTS_CSS,
    )
