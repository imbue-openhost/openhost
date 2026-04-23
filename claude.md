- on first init, read all markdown files in this project to get context.
- on first init, ensure pre-commit hooks are installed (`pre-commit install`). this runs ruff and mypy on commit.
- please ask before doing anything that affects low level system stuff on this machine, or anything using sudo.
- readmes are all human written. any ai-generated docs will be in files like readme_ai_generated.md. the ai-generated docs can be used for context but should *not* be considered necessarily up to date or as hard constraints on how the system should/must be built.

## project structure

```
openhost/
├── compute_space/
│   └── compute_space/    # quart/hypercorn app — routes requests to apps, manages containers
├── self_host_cli/        # `openhost` CLI: up, down, doctor, update
├── compute_space_cli/    # compute space management CLI
├── ansible/              # server deployment (any VPS or bare metal)
├── apps/
│   ├── dau_tracker/      # DAU tracking app
│   ├── partaay/          # event hosting app
├── tests/                # integration and e2e tests
└── docs/                 # design docs and specs
```

## how components connect

1. **compute_space** is a quart app (port 8080). it reads `openhost.toml` manifests from app repos, builds images from each app's `Dockerfile` using rootless podman, and runs each app in its own user namespace.
2. it proxies incoming HTTP requests to the correct app by matching subdomain or URL path prefix (`/{app_name}`).
3. **auth** uses JWT with RS256. apps verify with the public key passed as env var.

## running and testing

- **all lightweight tests**: `uv run --group dev pytest` (from project root)
- **+ container-runtime integration tests**: `uv run --group dev pytest --run-containers`
- **everything**: `uv run --group dev pytest --run-containers`
- **compute_space**: `cd compute_space && python -m compute_space`
- **compute_space tests**: `uv run --group dev pytest compute_space/tests/ -v`

## package manager

use `uv` for all python projects. python 3.12.

## key technical decisions

- JWT auth with RS256. router signs tokens, apps verify with public key.
