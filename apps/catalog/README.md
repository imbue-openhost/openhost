# catalog

OpenHost app discovery and one-click deployment catalog.  Builtin app —
vendored from the standalone repo at
https://github.com/imbue-openhost/openhost-catalog and auto-deployed by
default on first boot via `Config.default_apps`.

Go + HTML template app:

- Aggregates app entries from a configurable list of JSON feed sources
  (schema `openhost.catalog.v1`).
- Renders a server-side catalog UI (no React).
- Publishes apps to OpenHost with a single click.
- Polls deployment status and app logs.
- Falls back to OpenHost's native installer flow when router-token
  access is not configured.

Source-of-truth lives in this directory; the standalone
`imbue-openhost/openhost-catalog` repo will be archived once the
vendored copy is in production for a few zones.  Until then, treat
this directory as the canonical fork — sync changes back to the
standalone repo if you make them here, or vice versa.

For end-user docs (feed format, publish flow, etc.) see the
standalone repo's README.
