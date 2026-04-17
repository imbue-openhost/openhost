

## Cloud E2E Tests

Cloud E2E tests run on an ephemeral GCE instance via the `gcp-e2e` GitHub Actions workflow.
See `tests/gcp/` for infrastructure setup scripts.

## Local Full-Stack Tests

Local full-stack tests exercise the router via `test_full_stack.py`. These currently
still use the legacy multiuser_provider + QEMU VM harness and need reworking to
start the router directly (see TODO at top of `test_full_stack.py`).

    cd tests && uv run pytest test_full_stack.py -v -s
