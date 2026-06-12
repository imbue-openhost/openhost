# openhost_test_harness

Test scaffolding for OpenHost apps, **running the real OpenHost router locally** — no mocks.
The harness starts an HTTP-only router on a `*.localhost` zone (resolves to loopback on
Linux and macOS, no DNS setup), deploys your app through the real install path with rootless
podman, and gives your tests authenticated access. Routing, auth, identity env vars, and the
v2 service interface behave exactly as on a real server.

This supersedes the mock-router harness at `imbue-openhost/openhost-app-test-harness`.

## Install (in an app repo)

```toml
[dependency-groups]
dev = [
    "openhost[test-harness] @ git+https://github.com/imbue-openhost/openhost@main",
]
```

Requirements:
- python 3.12 (the openhost package pins `==3.12.*`)
- rootless podman (on macOS, a running `podman machine`)
- Linux only: the `openhost0` dummy interface + `host_containers_internal_ip`
  containers.conf override, so app containers can reach the router for service calls —
  see `ansible/tasks/containers.yml`, or the minimal version in `.github/workflows/ci.yml`.
  macOS needs nothing (gvproxy maps `host.containers.internal` to the host loopback).

## Use

```python
# tests/conftest.py
import pytest
from openhost_test_harness import OpenhostStack

@pytest.fixture(scope="session")
def stack():
    with OpenhostStack() as s:  # app_dir found by walking up from the cwd to the nearest openhost.toml
        yield s
```

```python
# tests/test_thing.py
def test_index(stack):
    r = stack.owner.get(f"{stack.url}/")
    assert r.status_code == 200
```

- `stack.url` — your app through the router (subdomain routing, real auth)
- `stack.owner` — a `requests.Session` authenticated as the zone owner; its cookie is scoped
  to the zone domain so it works on `stack.url` and every other app URL
- `stack.app_url` — direct to your app's container, bypassing the router (for tests that
  forge `X-OpenHost-*` headers or check unauthenticated behavior)
- `stack.router_url` — the router itself (dashboard, owner APIs)

Because auth is real, unauthenticated requests to `stack.url` redirect to `/login` — use
`stack.owner` (this is the main migration step from the mock harness, which injected the
owner header into every request).

## Testing the service interface

Your app **consumes** a service: deploy a real provider next to it and grant permissions.

```python
def test_my_app_reads_secrets(stack):
    stack.deploy_app("https://github.com/imbue-openhost/secrets")
    stack.grant(stack.app_id, "github.com/imbue-openhost/openhost/services/secrets", {"key": "DB_URL"})
    ...  # exercise your app; its service calls go through the real router proxy
```

(`OpenhostStack(grant_manifest_permissions=True)` is the default, so grants declared in your
app's `[[services.v2.consumes]]` are approved at install; pass `False` to test the
permission-denied flow.)

Your app **provides** a service: deploy a synthetic consumer and call through the real proxy.

```python
def test_my_service(stack):
    consumer = stack.deploy_service_consumer("github.com/me/my-service", shortname="svc", version=">=0.1.0")
    result = consumer.call("get", payload={"keys": ["FOO"]})   # routed via /api/services/v2/call/svc/get
    assert result.status == 403                                 # no grant yet — provider-side denial
    stack.grant(consumer.app_id, "github.com/me/my-service", {"key": "FOO"})
    assert consumer.call("get", payload={"keys": ["FOO"]}).status == 200
```

The consumer is a generated stdlib-only app; the router injects `X-OpenHost-Permissions` and
`X-OpenHost-Consumer-*` headers into proxied calls exactly as in production.

## Differences from the mock harness

- Real router: real owner auth, real subdomain routing, real `OPENHOST_*` env provisioning,
  real service proxy with permissions. `extra_env` / `health_path` / `zone_domain` overrides
  are gone — the router provisions apps from `openhost.toml` like production does.
- `stack.url` requires auth (`stack.owner`); `stack.app_url` is still direct and header-forgeable.
- Startup builds your app's image via the router (~seconds when cached) and runs a router
  subprocess (~3-5s).

## Self-tests

```
pixi run -e dev pytest openhost_app_test_harness -x --run-containers
```
