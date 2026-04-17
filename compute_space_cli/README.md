# oh — OpenHost CLI

Command-line tool for managing apps on your OpenHost compute space.

## Install

```bash
uv tool install "oh @ git+https://github.com/imbue-ai/openhost.git#subdirectory=compute_space_cli"
```

## Setup

```bash
oh login                     # add default instance
oh login --name staging      # add a named instance
```

This will prompt you for your compute space URL and walk you through creating an API token. Credentials are saved to `~/.openhost/compute_space_cli.toml`.

For development, use an editable install so changes take effect immediately:

```bash
cd compute_space_cli && uv tool install --editable .
```

## Usage

```bash
oh status                                    # check if compute space is reachable

oh app list                                  # list apps and status
oh app deploy https://github.com/you/myapp   # deploy from git repo
oh app deploy https://github.com/you/myapp --name cool-app  # custom name
oh app status cool-app                       # check status
oh app logs cool-app                         # view logs
oh app logs cool-app --follow                # tail logs
oh app reload cool-app                       # rebuild + restart
oh app reload cool-app --update              # git pull, then rebuild
oh app stop cool-app                         # stop app
oh app remove cool-app                       # remove app + data
oh app remove cool-app --keep-data           # remove but keep data
oh app rename cool-app new-name              # rename app

oh tokens list                               # list API tokens
oh tokens create --name "ci" --expiry-hours 72
oh tokens delete 3                           # delete by token ID
```

Deploy and reload accept `--wait` to block until the app is running (or errors).

## Multi-instance support

The CLI supports managing multiple named instances.

### Instance management

```bash
oh instance list                             # list all instances
oh instance add prod https://prod.host.com TOKEN --set-default
oh instance add staging https://s.host.com TOKEN
oh instance remove staging
oh instance set-default prod
```

### Targeting instances

```bash
oh --instance staging app list               # target a specific instance
OH_INSTANCE=staging oh app list              # same, via env var
```

Resolution order: `--instance` flag > `OH_INSTANCE` env var > default instance. If none of these are set and only one instance is configured, it is selected automatically.

## Update

```bash
uv tool upgrade oh
```
