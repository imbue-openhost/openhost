# Litestar migration — handoff notes

Session-handoff doc for the Quart → Litestar migration of `compute_space`.
Written so the next session can pick up cold without re-reading the prior
transcript.  This is an AI-generated working doc (per CLAUDE.md convention,
files like `readme_ai_generated.md` are not authoritative).

## The high-level task

Migrate `compute_space/` from Quart+Hypercorn to Litestar+Hypercorn so that:

- request args/JSON bodies are validated automatically from Python type
  hints / attrs classes in handler signatures
- OpenAPI specs are generated for free
- `attr.asdict(...) → jsonify(...)` boilerplate goes away (Litestar serializes
  attrs natively)
- the per-request DB connection is handed to handlers via Litestar DI
  rather than via a module-level `get_db()`

Hypercorn stays.  Templates (Jinja) stay.  WebSocket proxying stays.

## The plan we settled on

A stack of small, independently-reviewable PRs:

1. **PR #94 (this one)** — replace all the *top-level* web wiring (app
   factory, middleware, ASGI proxy, auth deps) and port **one** route
   module (settings) end-to-end as a worked example.  Tests will fail —
   most blueprints are no longer registered.
2. Subsequent PRs port the remaining ~16 route modules, one per PR.  Each
   adds its router to `create_app()` and any new entries to the templating
   `url_for` shim.
3. Final PR fixes up the test suite (port `app.test_client()` callsites
   to `litestar.testing.AsyncTestClient`, drop Quart-only test fixtures).

The user explicitly approved doing this in stacked PRs after we tried —
and abandoned — a big-bang single-PR approach earlier.

## PR #94: current state

- Branch: `zack/litestar-base-and-settings`
- Pushed two commits:
  1. `Switch app/auth/middleware/proxy to Litestar; port settings routes`
  2. `db: hand connection to routes via Litestar DI`
- `pre-commit` (ruff + mypy) is currently clean.
- The user has left **review feedback that supersedes some of those
  decisions** (see next section).

## Review feedback to address (verbatim, with required action)

All from `zplizzi`, the repo owner.

### 1. `compute_space/src/compute_space/core/auth/__init__.py:1` — "please leave init.py empty."

Currently has re-exports + the `get_current_user(inputs)` definition.  Empty it out.

### 2. `compute_space/src/compute_space/core/auth/cookies.py:1` — "leave this in web/auth/cookies.py"

Don't extract framework-neutral cookie helpers into `core/auth/`.  Delete
`core/auth/cookies.py`, keep cookie code in `web/auth/cookies.py`,
Litestar-typed.  (This **overrides** the earlier "core/ must be framework-
neutral" extraction we did — the web-layer concern stays in web.)

### 3. `compute_space/src/compute_space/core/auth/__init__.py:38` (the `get_current_user(inputs)` function) — "leave this wherever it was, probs in web. just port to litestar."

Move the function back to `web/auth/middleware.py` (or wherever it lived
before this PR — verify against `main`).  Don't keep the framework-neutral
`AuthInputs` / `get_current_user(inputs: AuthInputs)` indirection.  Just
have a Litestar-typed function that takes a Litestar `ASGIConnection`.

### 4. `compute_space/src/compute_space/web/auth/cookies.py:1` — "drop docstring"

Drop the `"""Auth cookie helpers ..."""` module docstring.  (Style guide
says no file-level docstrings unless the file is genuinely confusing.)

### 5. `compute_space/src/compute_space/web/auth/middleware.py:40` — "drop all quart stuff from here. it's ok if mypy doesn't pass."

Delete the Quart-flavored helpers I kept for the unmigrated route files
(`get_current_user_from_request(QuartRequest|QuartWebsocket)`,
`_wants_json`, `_app_action_response`, `_ensure_async`, `_try_refresh()`
using `g`/`quart_request`, `_app_from_origin(QuartRequest|QuartWebsocket)`,
`app_auth_required` decorator, `login_required` decorator).  Keep only
the Litestar dependencies (`provide_user`, `provide_app_id`,
`login_required_redirect`, and their helpers).

> **Important:** mypy will start failing for unmigrated route files that
> import these.  The user has explicitly said that's fine.  Don't
> re-introduce Quart shims to keep mypy happy.

### 6. `compute_space/src/compute_space/web/routes/api/settings.py:39` — "i don't think we need a whole attrs class for very simple request/return types."

Drop the `GetRemoteResponse`, `SetRemoteRequest`, `SetRemoteResponse`,
`HostPrep`, `CheckForUpdatesResponse`, `UpdateRepoStateResponse`,
`OkResponse` attrs classes.  Use simpler types in the handler signatures
where reasonable.  This **softens** the earlier policy of "attrs classes
for complex types, simple Python types for simple types, no
`dict[str, Any]`" — apparently `dict[str, Any]` is OK for simple
JSON shapes here.  Use judgment; don't bikeshed.

### 7. `compute_space/src/compute_space/web/app.py:46` (the `_ROUTE_NAME_TO_PATH` and `_url_for` shim) — "i don't want any code that won't be used in the final litestar version. it's ok if things are broken rn."

Delete `_ROUTE_NAME_TO_PATH` and `_url_for`.  Delete entries in
`_template_globals` that pre-populate `url_for` for unmigrated routes.
Templates that reference `url_for("apps.dashboard")` etc. will fail at
render time — that's fine, they'll be fixed as their pages get migrated.

This **overrides** the "templates must keep rendering plausibly during
the stack" goal — broken templates are acceptable in the intermediate
PRs.

### 8. `compute_space/src/compute_space/config.py:225` — "let's provide this via dependency injection also instead of calling get_config() in routes."

Add a `provide_config()` Litestar dependency.  Register it in `app.py`:
`"config": Provide(provide_config, sync_to_thread=False)`.  Update
handlers/deps that currently call `get_config()` to take
`config: Config` instead.  Same pattern as the `db` DI we just added.

`set_active_config()` / `get_config()` can stay for non-DI callers
(middleware, `core/` helpers) — analogous to how `get_db()` stays for
non-DI use even though `provide_db` exists.  Or revisit if the user wants
those gone too.

## Underlying themes from the feedback

These are inferences, not direct quotes — apply with judgment:

- **Stop trying to keep Quart and Litestar both working.**  This is a
  one-way migration.  Each PR should leave the codebase in a state
  where unmigrated routes are *broken* but in-scope routes are
  *clean Litestar*, with no compatibility shims.
- **Don't extract framework-neutral abstractions for things that don't
  need to leave the web layer.**  `core/` layering still holds (no
  framework imports in `core/`), but that doesn't mean every web-layer
  helper should be split into a neutral version in `core/` plus a
  framework adapter in `web/`.  Keep web concerns in `web/`.
- **Code only what you need right now.**  No scaffolding for future
  PRs (the `_ROUTE_NAME_TO_PATH` was speculative).  No attrs classes
  for trivial JSON shapes.  No broken-state preservation hacks.
- **mypy / pre-commit aren't sacred during the stack.**  It's OK if
  mypy fails on the unmigrated route files — they're going away soon.

## Architectural decisions still in force

These were established earlier and have NOT been reversed:

- **Big-bang within a single PR was rejected** in favor of stacked PRs.
- `db/connection.py` uses `contextvars.ContextVar` (not `quart.g`); the
  Litestar `provide_db` dep is a thin wrapper over `get_db()`.  This is
  fine and shouldn't change.
- **Per-request, not pooled** for SQLite connections.
- `web/middleware/subdomain_proxy.py` is raw ASGI middleware (replacing
  the deleted `web/routes/proxy.py` `before_app_request` hook), and
  must `close_db()` in a `finally` because it short-circuits the
  Litestar router (no `after_request` hook fires for the proxied path).
- `web/middleware/auth_refresh.py` is raw ASGI middleware that wraps
  `send` to attach refreshed `Set-Cookie` headers when a dep stashes
  new tokens in `scope["state"]`.
- `web/proxy.py` is ASGI-native (`proxy_request(scope, receive, ...)`,
  `ws_proxy(target_port, ws: litestar.WebSocket, ...)`).  The
  `proxy_request_quart` / `ws_proxy_quart` adapters were added for
  unmigrated `services_v2.py` — they may need to go too if feedback
  item #5's spirit ("no Quart shims") applies, but `services_v2.py`
  is a non-trivial migration so we may want to keep the adapters
  until that PR.
- Static files served via `create_static_files_router(path="/static", ...)`.
- OpenAPI mounted at `/openapi` (not the default `/schema`).

## Recommended order of operations for next session

1. Re-read this file.  Re-read CLAUDE.md and `style_guide.md`.
2. Check out `zack/litestar-base-and-settings`.
3. Address feedback in roughly this order (each is independent):
   1. (#1, #2, #3) — collapse the `core/auth/` extractions back into
      `web/`.  Empty out `core/auth/__init__.py`.  Delete
      `core/auth/cookies.py` and `core/auth/inputs.py`.  Move the
      cookie helpers + `AuthInputs`/`get_current_user` back to
      `web/auth/`.  Litestar-type them (no Quart, no neutral
      abstraction).  Update import sites in `web/`.
   2. (#5) — drop Quart helpers from `web/auth/middleware.py`.  Mypy
      will start failing on unmigrated route files that import
      `login_required` etc.  That's fine.  Don't add shims.
   3. (#7) — delete `_ROUTE_NAME_TO_PATH` and `_url_for` from
      `web/app.py`.  Don't pre-populate Jinja globals for unmigrated
      endpoints.
   4. (#6) — simplify settings routes.  Drop the attrs classes for
      trivial JSON shapes.
   5. (#4) — drop the `web/auth/cookies.py` module docstring.
   6. (#8) — add `provide_config` Litestar dep, register on app,
      thread `config: Config` through the handlers/deps that need it.
4. Smoke-test with `litestar.testing.TestClient` again (settings
   routes should still work).  pre-commit may not pass on unmigrated
   files — confirm with the user before doing anything to silence it.
5. Push as a new commit (don't rebase / squash unless asked).
6. Reply to each PR comment with what you did, then ping the user.

## Files to look at first

- This file.
- `compute_space/src/compute_space/web/app.py` — Litestar app factory.
- `compute_space/src/compute_space/web/auth/middleware.py` — has both
  Quart helpers (to delete) and Litestar deps (to keep).
- `compute_space/src/compute_space/web/auth/cookies.py` — currently a
  thin Quart wrapper over `core/auth/cookies.py` builders; needs to
  become Litestar-typed and own the cookie code.
- `compute_space/src/compute_space/web/auth/inputs.py` — adapter; will
  fold back into wherever `get_current_user` lives.
- `compute_space/src/compute_space/core/auth/__init__.py`,
  `cookies.py`, `inputs.py` — to be removed/emptied.
- `compute_space/src/compute_space/web/routes/api/settings.py` — drop
  attrs classes for trivial shapes.
- `compute_space/src/compute_space/db/connection.py` — provide_db pattern
  works; mirror it for `provide_config`.
- `compute_space/src/compute_space/config.py` — add `provide_config`.
