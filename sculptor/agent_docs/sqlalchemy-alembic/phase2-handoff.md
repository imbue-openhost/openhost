# Phase 2 Handoff — CI-Passing SHA (REQ-TEST-4)

## CI-passing commit

**SHA:** `9f07156` — `refactor(compute_space/db): drop legacy get_db/close_db helpers`

This is the first commit at which the full query-layer conversion lands end-to-end
with no legacy helpers remaining and the test suite green. It is the recommended
handoff point for any follow-up work that assumes phase 2 is complete.

## How this was verified

Checked out the commit locally and ran the full non-Docker suite:

```
git checkout 9f07156
uv run --group dev pytest
```

Result: **201 passed, 30 skipped** (~5 s, tests from `compute_space/tests/` and
`self_host_cli/tests/`).

## Follow-up: deferred raw-SQL test rewrites

`test_storage.py` and `test_migrations.py` still contain raw-SQL / sqlite3
patterns that the phase-2 spec (REQ-TEST-3) explicitly allowed to defer. They
pass at `9f07156` and were not rewritten as part of phase 2 — migrating them to
the ORM/AsyncSession fixture style is the scope of the follow-up test-rewrite
pass.
