import os
import tomllib
from pathlib import Path
from typing import Any
from typing import Self

import attr
import cattrs
import tomli_w

CONFIG_DIR = Path.home() / ".openhost"
CONFIG_FILE = CONFIG_DIR / "compute_space_cli.toml"


class ConfigFileNotFoundError(Exception):
    pass


class ConfigInvalidError(Exception):
    pass


class InstanceNotFoundError(Exception):
    pass


def normalize_url(url: str) -> str:
    """Ensure a URL has a protocol prefix."""
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


def _validate_url(instance: object, attribute: object, value: str) -> None:
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"URL must include protocol (http:// or https://): {value}")


@attr.s(auto_attribs=True, frozen=True)
class Instance:
    """A single OpenHost compute-space instance."""

    url: str = attr.ib(validator=_validate_url)
    token: str = attr.ib()


@attr.s(auto_attribs=True, frozen=True)
class MultiConfig:
    """Top-level configuration supporting multiple named instances."""

    instances: dict[str, Instance] = attr.ib(factory=dict)
    default_instance: str | None = attr.ib(default=None)

    def evolve(self, **changes: Any) -> Self:  # noqa: ANN401
        """Return a copy with the given fields replaced."""
        return attr.evolve(self, **changes)

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        raw: dict[str, object] = {}
        if self.default_instance:
            raw["default_instance"] = self.default_instance
        instances_raw: dict[str, object] = {}
        for name, inst in self.instances.items():
            entry: dict[str, object] = {"url": inst.url, "token": inst.token}
            instances_raw[name] = entry
        if instances_raw:
            raw["instances"] = instances_raw
        with open(path, "wb") as f:
            tomli_w.dump(raw, f)

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        path = path or CONFIG_FILE
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            raise ConfigFileNotFoundError(f"Config file not found at {path}") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigInvalidError(f"Config file at {path} has invalid TOML syntax: {e}") from None

        try:
            if "instances" in data:
                instances: dict[str, Instance] = {}
                for name, raw_inst in data["instances"].items():
                    instances[name] = cattrs.structure(raw_inst, Instance)
                raw_default = data.get("default_instance")
                if raw_default is not None and not isinstance(raw_default, str):
                    raise TypeError(f"default_instance must be a string, got {type(raw_default).__name__}")
                return cls(
                    instances=instances,
                    default_instance=raw_default,
                )

            if "url" in data and "token" in data:
                inst = cattrs.structure(data, Instance)
                return cls(
                    instances={"default": inst},
                    default_instance="default",
                )
        except (cattrs.ClassValidationError, ValueError, TypeError, AttributeError) as e:
            raise ConfigInvalidError(f"Config file at {path} is malformed: {e}") from None

        # Empty or unrecognized config — return empty MultiConfig.
        return cls()

    def upsert_instance(self, name: str, inst: Instance, *, set_default: bool = False) -> Self:
        """Return a new config with the given instance added or replaced.

        If *set_default* is True, the new instance becomes the default.
        Otherwise the existing default is preserved.
        """
        instances = dict(self.instances)
        instances[name] = inst
        default = name if set_default else self.default_instance
        return self.evolve(instances=instances, default_instance=default)

    def remove_instance(self, name: str) -> Self:
        """Return a new config with the named instance removed.

        Raises *InstanceNotFoundError* if the name does not exist.
        If the removed instance was the default, the first remaining instance
        becomes the new default (or ``None`` if no instances remain).
        """
        self.get_instance(name)  # raises InstanceNotFoundError if missing
        instances = {k: v for k, v in self.instances.items() if k != name}
        default = self.default_instance
        if default == name:
            default = next(iter(instances), None)
        return self.evolve(instances=instances, default_instance=default)

    @property
    def _available_names(self) -> str:
        return ", ".join(sorted(self.instances)) or "(none)"

    def get_instance(self, name: str) -> Instance:
        """Return a named instance or raise."""
        if name not in self.instances:
            raise InstanceNotFoundError(f"Instance '{name}' not found. Available: {self._available_names}")
        return self.instances[name]

    def resolve(self, instance_name: str | None = None) -> Instance:
        """Resolve which instance to use.

        Priority: explicit name > OH_INSTANCE env var > default_instance.
        """
        name = instance_name
        if not name:
            name = os.environ.get("OH_INSTANCE")
        if not name:
            name = self.default_instance

        if not name:
            if not self.instances:
                raise InstanceNotFoundError("No instances configured. Run 'oh instance login' first.")
            raise InstanceNotFoundError(
                "No default instance set. Use --instance <name>, or set a default with:\n"
                "  oh instance set-default <name>\n"
                "Run 'oh instance list' to see configured instances."
            )

        return self.get_instance(name)


def get_multi_config() -> MultiConfig:
    return MultiConfig.load()
