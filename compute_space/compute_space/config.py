import os
import tomllib
from pathlib import Path
from typing import Any
from typing import Self

import attr
import cattrs
import tomli_w
import typed_settings
from quart import current_app


@attr.s(auto_attribs=True, frozen=True)
class Config:
    # Server
    zone_domain: str
    host: str
    port: int

    # TLS
    tls_enabled: bool
    acquire_tls_cert_if_missing: bool
    acme_email: str | None
    acme_account_key_path: str | None

    # coredns (only really needed if acquiring TLS certs via DNS-01, or if using NS dns records)
    coredns_enabled: bool
    public_ip: str | None

    start_caddy: bool

    my_openhost_redirect_domain: str

    # Data
    data_root_dir: str
    apps_dir_override: str | None

    # Optional override for where ``app_archive`` bind mounts are
    # backed.  When unset, the archive tier defaults to a local-disk
    # subdirectory under ``persistent_data_dir`` — same backing as
    # ``app_data``, just a separate dir.  When the operator sets up
    # JuiceFS (or any other host-mounted POSIX filesystem) and points
    # this at the mount path, every app that opts into ``app_archive``
    # gets bind-mounts into that filesystem instead.  The app sees the
    # same in-container path either way; only the backing changes.
    archive_dir_override: str | None

    # Minimum free disk space in MB (0 = no enforcement)
    storage_min_free_mb: int

    # Ports
    port_range_start: int
    port_range_end: int

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
        # Where every app's ``/data/app_archive/<name>/`` bind-mount
        # source lives.  Operator override (e.g. a JuiceFS mount path)
        # takes priority; otherwise we fall back to a local-disk
        # subdirectory of ``persistent_data_dir`` so apps that opt
        # into ``app_archive`` work even on instances with no
        # JuiceFS configured — they just don't get the elastic-S3
        # benefit.
        if self.archive_dir_override:
            return self.archive_dir_override
        return os.path.join(self.persistent_data_dir, "app_archive")

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
        return Path(__file__).parent.parent.parent

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

    def make_all_dirs(self) -> None:
        """Make all necessary directories for the config."""
        assert os.path.exists(self.data_root_dir)
        os.makedirs(self.persistent_data_dir, exist_ok=True)
        os.makedirs(self.temporary_data_dir, exist_ok=True)
        # ``app_archive_dir`` may resolve to an external mount the
        # operator already set up (e.g. JuiceFS); we don't try to
        # mkdir it in that case because we likely lack permission and
        # the mount itself was already created by ansible.  Only
        # create it when it's pointing at the local fallback.
        if not self.archive_dir_override:
            os.makedirs(self.app_archive_dir, exist_ok=True)
        os.makedirs(self.apps_dir, exist_ok=True)
        os.makedirs(self.openhost_data_path, exist_ok=True)
        os.makedirs(self.keys_dir, exist_ok=True)


@attr.s(auto_attribs=True, frozen=True)
class DefaultConfig(Config):
    # needs set at runtime, no reasonable default value
    # zone_domain: str

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # coredns (only truly needed if tls_enabled)
    coredns_enabled: bool = False
    public_ip: str | None = None

    # TLS
    tls_enabled: bool = False
    acquire_tls_cert_if_missing: bool = False
    acme_email: str | None = None
    acme_account_key_path: str | None = None

    start_caddy: bool = True

    my_openhost_redirect_domain: str = "my.selfhost.imbue.com"

    # Data
    data_root_dir: str = "/opt/openhost"
    apps_dir_override: str | None = None  # if None, defaults to data_root_dir/apps
    archive_dir_override: str | None = None  # if None, defaults to persistent_data_dir/app_archive

    # Minimum free disk space in MB (0 = no enforcement)
    storage_min_free_mb: int = 0

    # Ports
    port_range_start: int = 9000
    port_range_end: int = 9999


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


def get_config() -> Config:
    """Get the Config object from the current Quart app context.

    This is just a helper to make type checking work,
    vs accessing app.openhost_config directly which would be unytped.
    """
    return current_app.openhost_config  # type: ignore[attr-defined, no-any-return]
