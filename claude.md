- read the style guide in style_guide.md
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
├── tests/                # integration and e2e tests
└── docs/                 # design docs and specs
└── services/             # specs for certain bundled services
```

## how components connect

1. **compute_space** is a quart app (port 8080). it reads `openhost.toml` manifests from app repos, builds images from each app's `Dockerfile` using rootless podman, and runs each app in its own user namespace.
2. it proxies incoming HTTP requests to the correct app by matching subdomain.
3. **auth** uses JWT with RS256. apps verify with the public key passed as env var.

## running and testing

always run tests with -x to fail quickly.

- **all lightweight tests**: `uv run --group dev pytest -x` (from project root)
- **everything**: `uv run --group dev pytest -x --run-containers`
- **compute_space tests**: `uv run --group dev pytest -x compute_space/tests/`
- **e2e against existing instance**: `uv run --group dev pytest -x tests/test_e2e.py --use-existing-instance NAME`

`NAME` is an `oh` CLI hostname or alias (see `oh instance list`). this will:
1. check that all local commits are pushed
2. sync the instance to the current commit (set_remote + restart)
3. run the full e2e suite with unique app names per run
4. clean up test apps and restore the instance to its prior remote/ref

## package manager

use `uv` or `pixi` for all python projects.

pixi is used for this project's main env on prod. python 3.12.
but for dev, we haven't wired up the `dev` group, and pixi deps aren't all available on mac, so we still use uv.

