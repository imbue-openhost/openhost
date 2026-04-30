

## Cloud E2E Tests

Cloud E2E tests run on an ephemeral GCE instance via the `gcp-e2e` GitHub Actions workflow.
See `tests/gcp/` for infrastructure setup scripts.

### Running against an existing instance

You can run the e2e suite against any instance configured in the `oh` CLI:

    uv run --group dev pytest -x tests/test_e2e.py --use-existing-instance NAME

where `NAME` is a hostname or alias from `oh instance list`.

This will automatically:

1. Verify all local commits are pushed
2. Sync the instance to your current commit (via `set_remote` + restart)
3. Run tests with unique app names (so they don't collide with existing apps)
4. Clean up test apps afterward (best effort)
5. Restore the instance to its prior remote and ref

## Local Full-Stack Tests

Local full-stack tests exercise the router via `test_full_stack.py`. These currently
still use the legacy multiuser_provider + QEMU VM harness and need reworking to
start the router directly (see TODO at top of `test_full_stack.py`).

    cd tests && uv run pytest test_full_stack.py -v -s
