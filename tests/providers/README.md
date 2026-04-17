# Cloud Provider Interface

Each provider script (`gcp.sh`, `ec2.sh`, etc.) implements two functions:

## Required functions

### `provider_create`
Create a cloud instance and set `PROVIDER_PUBLIC_IP` to its public IP.

Expected environment variables are provider-specific (see each script).
The function must:
- Create the instance
- Wait for it to reach a running state
- Set `PROVIDER_PUBLIC_IP` to the public IP (do **not** print it to stdout)
- Set any internal state needed by `provider_env_vars` and `provider_teardown`

**Important**: `provider_create` is called in the current shell (not a subshell)
so that internal variables are preserved for `provider_env_vars`.

### `provider_teardown`
Delete the cloud instance. Must be idempotent.

Environment variables from the env file (written during setup) are available.

### `provider_env_vars`
Print provider-specific `export VAR=value` lines to stdout.
These are appended to the env file for use during teardown.

## Adding a new provider

1. Create `tests/providers/<name>.sh`
2. Implement the three functions above
3. Add a workflow in `.github/workflows/<name>-e2e.yml` (or add a matrix entry)
4. Set required secrets in GitHub
