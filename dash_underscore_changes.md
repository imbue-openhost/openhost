# Dash/Underscore Standardization — Pending Decisions

Decision not yet made. Options:
- **All underscores everywhere** (Python-only convention, simpler)
- **Dashes for CLI subcommands and HTTP routes, underscores for Python** (standard cross-ecosystem convention)

Python module/directory names (`openhost_system_agent/`) **must** stay underscores regardless — language constraint.

---

## Change locations

### 1. CLI subcommand names
`show_diff`, `set_remote`, `get_remote` → `show-diff`, `set-remote`, `get-remote` (if dashes chosen)

These two files must change together — `system_agent.py` passes the exact subcommand strings to subprocess:

- `openhost_system_agent/src/openhost_system_agent/cli.py` — defines the subcommands
- `compute_space/src/compute_space/core/system_agent.py` — lines 66, 71, 76, 81, 86, 91 — subprocess calls

### 2. HTTP route paths
`/api/settings/get_remote`, `/api/settings/set_remote` → `/api/settings/get-remote`, `/api/settings/set-remote` (if dashes chosen)

These two files must change together:

- `compute_space/src/compute_space/web/routes/api/settings.py` — route definitions
- `compute_space/src/compute_space/web/templates/settings.html` — lines 104, 136, 192, 223 — fetch() calls

### 3. Package metadata name (cosmetic, safe either way)
- `openhost_system_agent/pyproject.toml` line 2 — `name = "openhost-system-agent"` → `"openhost_system_agent"`
  - Note: PyPI normalizes dashes/underscores as equivalent; this sub-package is not independently installed on servers

### 4. Documentation / prose
- `openhost_system_agent/README.md` line 19 — usage example shows `openhost-system-agent`, should be `openhost_system_agent`
- `compute_space/src/compute_space/core/apps.py` lines 833, 844 — prose comments say `system-agent`

---

## What does NOT need to change

- `src/openhost_system_agent/` directory and all Python imports — must stay underscores (Python identifiers)
- Python function names (`system_agent_fetch`, `fetch_updates`, etc.) — must stay underscores
- `attrs` field names in `protocol.py` and JSON output keys — must stay underscores
- Ansible variable `system_agent_path` — must stay underscores
- Binary name `/usr/local/bin/openhost_system_agent` — already underscore, no change
- Tests in `test_settings_host_prep.py` — mock Python function names, not CLI strings or URLs

---

## Blast radius summary

| Change | Files affected | Coordinated? |
|---|---|---|
| CLI subcommand names | `cli.py` + `system_agent.py` | Yes — both at once |
| HTTP route paths | `settings.py` + `settings.html` | Yes — both at once |
| Package `name =` | `pyproject.toml` only | No — isolated |
| README/prose | `README.md`, `apps.py` | No — isolated |

No external callers found in the repo. The `oh` CLI, bundled apps, and tests do not call these CLI subcommands or HTTP routes directly.
