import os
import tomllib
from pathlib import Path
from typing import Any
from typing import Self

import attr
import cattrs
import tomli_w
import typed_settings

# Cert provider modes (see the `cert_provider` config field).
# "eab_mint": mint a single-use EAB from the cert-api and create this instance's
#   own ACME account, then issue from Google Trust Services (the managed default).
# "byo": operator brings their own acme_account_key_path + acme_directory_url.
CERT_PROVIDER_EAB_MINT = "eab_mint"
CERT_PROVIDER_BYO = "byo"
_VALID_CERT_PROVIDERS = frozenset({CERT_PROVIDER_EAB_MINT, CERT_PROVIDER_BYO})

# Default cert-api EAB minter endpoint (Imbue hosted). The minter is the
# openhost-cert-api service deployed as an OpenHost app (app name
# "openhost-cert-api"), so it lives at that subdomain of the managed zone.
# Self-hosters running their own minter override this via `cert_api_url`.
# The client appends "/v1/eab" (see core/tls/cert_api_client.py).
DEFAULT_CERT_API_URL = "https://openhost-cert-api.selfhost.imbue.com"


def _lowercase(s: str) -> str:
    # mypy can't handle str.lower apparently
    return s.lower()


@attr.s(auto_attribs=True, frozen=True)
class Config:
    ## Server
    # zone_domain is where the compute space is hosted, eg `host.example.com`
    # it can optionally include a non-80/443 port, if necessary.
    zone_domain: str = attr.ib(converter=_lowercase)
    # the local IP to bind the compute space web server to.
    host: str
    # the local port to bind the compute space web server to.
    port: int

    ## TLS
    tls_enabled: bool
    acquire_tls_cert_if_missing: bool
    acme_email: str | None
    # Operator-supplied ACME account key (used only in "byo" cert_provider mode).
    # In "eab_mint" mode the instance generates and persists its own account key
    # at `managed_acme_account_key_path`; this field is ignored.
    acme_account_key_path: str | None
    acme_directory_url: str | None

    # Cert provider mode: "eab_mint" (default) or "byo". See CERT_PROVIDER_* above.
    cert_provider: str = attr.ib(validator=attr.validators.in_(_VALID_CERT_PROVIDERS))
    # cert-api EAB minter endpoint (eab_mint mode only). Override to self-host.
    cert_api_url: str | None
    # Per-instance bearer token for the cert-api minter (eab_mint mode). The
    # operator provisions this; the service requires it under its current auth
    # scheme (missing/invalid -> the mint call fails loudly with HTTP 401).
    cert_api_token: str | None

    ## coredns (only really needed if acquiring TLS certs via DNS-01, or if using NS dns records)
    coredns_enabled: bool
    public_ip: str | None

    start_caddy: bool

    my_openhost_redirect_domain: str

    ## Data
    data_root_dir: str
    apps_dir_override: str | None

    # Minimum free disk space in MB (0 = no enforcement)
    storage_min_free_mb: int

    ## Ports
    port_range_start: int
    port_range_end: int

    # First-boot claim-token gate. When True, /setup rejects any request that
    # doesn't supply a token matching the one in claim_token_path — preventing
    # a MITM from racing the operator to set the owner password. When True but
    # no token file is present, /setup rejects everyone (fail-safe). Set this
    # explicitly to False only when /setup is reachable only by the operator
    # (e.g. loopback-only local dev).
    claim_token_required: bool

    # Apps to deploy at /setup completion (set to [] to opt out).
    # Each entry is either:
    #   - a bare dirname under apps_dir (vendored builtin, e.g. "secrets_v2"), or
    #   - a remote git URL the router will clone on first boot
    #     (e.g. "https://github.com/imbue-openhost/openhost-catalog").
    # Remote URLs are dispatched through the same clone path as
    # /api/add_app and do not need to be present on disk ahead of time.
    default_apps: list[str]

    @property
    def zone_domain_no_port(self) -> str:
        return self.zone_domain.split(":")[0]

    def evolve(self, **kwargs: Any) -> Self:
        return attr.evolve(self, **kwargs)

    def to_toml_str(self) -> str:
        return tomli_w.dumps({"openhost": {k: v for k, v in attr.asdict(self).items() if v is not None}})

    def to_toml(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            tomli_w.dump({"openhost": {k: v for k, v in attr.asdict(self).items() if v is not None}}, f)

    @classmethod
    def from_toml(cls, path: str) -> Self:
        with open(path, "rb") as f:
            d = tomllib.load(f)
        return cattrs.structure(d.get("openhost", d), cls)

    @property
    def persistent_data_dir(self) -> str:
        return os.path.join(self.data_root_dir, "persistent_data")

    @property
    def temporary_data_dir(self) -> str:
        return os.path.join(self.data_root_dir, "temporary_data")

    @property
    def app_archive_dir(self) -> str:
        # JuiceFS FUSE mount; lives under data_root_dir (NOT persistent_data_dir)
        # so restic backups don't double-store bytes that already live in S3.
        # Empty/non-existent until archive_backend.configure_backend has run.
        return os.path.join(self.data_root_dir, "app_archive")

    @property
    def apps_dir(self) -> str:
        # where openhost/apps/ is mounted
        if self.apps_dir_override:
            return self.apps_dir_override
        return os.path.join(self.data_root_dir, "apps")

    @property
    def openhost_data_path(self) -> Path:
        # openhost-specific data, including the sqlite db and TLS certs.
        return Path(self.persistent_data_dir) / "openhost"

    @property
    def openhost_repo_path(self) -> Path:
        # compute_space/src/compute_space/config.py -> openhost repo root
        return Path(__file__).resolve().parent.parent.parent.parent

    @property
    def db_path(self) -> str:
        return str(self.openhost_data_path / "router.db")

    @property
    def tls_cert_path(self) -> Path:
        return self.openhost_data_path / "openhost-tls-cert.pem"

    @property
    def tls_key_path(self) -> Path:
        return self.openhost_data_path / "openhost-tls-key.pem"

    @property
    def managed_acme_account_key_path(self) -> Path:
        # Where "eab_mint" mode persists this instance's own ACME account key so
        # renewals reuse it.  A lost key means a fresh EAB is minted next boot.
        return self.openhost_data_path / "acme-account-key.json"

    @property
    def coredns_corefile_path(self) -> Path:
        return self.openhost_data_path / "Corefile"

    @property
    def coredns_zonefile_path(self) -> Path:
        return self.openhost_data_path / "zonefile"

    @property
    def caddyfile_path(self) -> Path:
        return self.openhost_data_path / "Caddyfile"

    @property
    def keys_dir(self) -> str:
        return str(Path(self.openhost_data_path) / "keys")

    @property
    def claim_token_path(self) -> str:
        return str(Path(self.openhost_data_path) / "claim_token")

    @property
    def default_apps_sentinel_path(self) -> str:
        return str(Path(self.openhost_data_path) / "default_apps.json")

    def make_all_dirs(self) -> None:
        """Make all necessary directories for the config."""
        assert os.path.exists(self.data_root_dir)
        os.makedirs(self.persistent_data_dir, exist_ok=True)
        os.makedirs(self.temporary_data_dir, exist_ok=True)
        # Skip app_archive_dir: a stray local dir at that path would shadow
        # the JuiceFS mount once attach_on_startup brings it up.
        os.makedirs(self.apps_dir, exist_ok=True)
        os.makedirs(self.openhost_data_path, exist_ok=True)
        os.makedirs(self.keys_dir, exist_ok=True)


@attr.s(auto_attribs=True, frozen=True)
class DefaultConfig(Config):
    # needs set at runtime, no reasonable default value
    # zone_domain: str

    # Server
    host: str = "127.0.0.1"
    port: int = 8080

    # coredns (only truly needed if tls_enabled)
    coredns_enabled: bool = False
    public_ip: str | None = None

    # TLS
    tls_enabled: bool = False
    acquire_tls_cert_if_missing: bool = False
    acme_email: str | None = None
    acme_account_key_path: str | None = None
    acme_directory_url: str | None = None

    # Cert provider: default to the managed EAB-mint path (targets GTS).
    # Re-declare with the validator: a subclass override drops the base attr.ib,
    # and DefaultConfig is the class actually loaded at runtime.
    cert_provider: str = attr.ib(default=CERT_PROVIDER_EAB_MINT, validator=attr.validators.in_(_VALID_CERT_PROVIDERS))
    cert_api_url: str | None = DEFAULT_CERT_API_URL
    cert_api_token: str | None = None

    start_caddy: bool = True

    my_openhost_redirect_domain: str = "my.selfhost.imbue.com"

    # Data
    data_root_dir: str = "/opt/openhost"
    apps_dir_override: str | None = None  # if None, defaults to data_root_dir/apps

    # Minimum free disk space in MB (0 = no enforcement)
    storage_min_free_mb: int = 0

    # Fail-safe default: require a claim token at /setup. Callers that want
    # the open-setup behavior (local-dev loopback) must set this False.
    claim_token_required: bool = True

    # Ports
    port_range_start: int = 9000
    port_range_end: int = 9999

    # Apps to auto-deploy at /setup completion.  Entries are either:
    #   - a bare dirname under apps_dir (vendored builtin), or
    #   - a remote git URL cloned on demand (see core/default_apps).
    default_apps: list[str] = attr.Factory(
        lambda: [
            "https://github.com/imbue-openhost/secrets",
            "file_browser",
            "https://github.com/imbue-openhost/openhost-catalog",
        ]
    )


def load_config() -> Config:
    """Load config from OPENHOST_ prefixed env vars, env-selected TOML file, or default config, in that order.

    Prefer ``OPENHOST_ROUTER_CONFIG`` (new CLI name) and fall back to
    ``OPENHOST_CONFIG`` for backward compatibility.
    """
    path = os.environ.get("OPENHOST_ROUTER_CONFIG") or os.environ.get("OPENHOST_CONFIG")
    if path:
        return typed_settings.load(DefaultConfig, appname="openhost", config_files=[path])
    else:
        return typed_settings.load(DefaultConfig, appname="openhost")


_active_config: Config | None = None


def set_active_config(config: Config) -> None:
    """Register the active config for the running web app.

    Called once at app-factory time so ``get_config()`` works framework-neutrally
    (the previous Quart implementation read it from ``current_app``).
    """
    global _active_config
    _active_config = config


def get_config() -> Config:
    """Return the active config registered via ``set_active_config``."""
    if _active_config is None:
        raise RuntimeError("set_active_config() must be called before get_config()")
    return _active_config


def provide_config() -> Config:
    """Litestar dependency: hand the active config to a route or other dep.

    Wraps ``get_config()`` so handlers can take ``config: Config`` instead of
    calling the module-level accessor.  ``get_config()`` stays available for
    non-DI callers (middleware, ``core/`` helpers).

    litestar got confused by returning a DefaultConfig so we convert it back to plain Config.
    """
    active = get_config()
    if type(active) is Config:
        return active
    return Config(**{f.name: getattr(active, f.name) for f in attr.fields(Config)})
