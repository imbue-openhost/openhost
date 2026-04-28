"""Unit tests for config module — no network required."""

from pathlib import Path

import pytest
import tomli_w

from compute_space_cli.config import ConfigFileNotFoundError
from compute_space_cli.config import ConfigInvalidError
from compute_space_cli.config import Instance
from compute_space_cli.config import InstanceNotFoundError
from compute_space_cli.config import MultiConfig
from compute_space_cli.config import normalize_url


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    return tmp_path / "config.toml"


def _make_multi(
    instances: dict[str, Instance] | None = None,
    default: str | None = None,
) -> MultiConfig:
    return MultiConfig(
        instances=instances or {},
        default_instance=default,
    )


class TestInstance:
    def test_valid_https(self) -> None:
        inst = Instance(url="https://example.com", token="tok")
        assert inst.url == "https://example.com"
        assert inst.token == "tok"

    def test_valid_http(self) -> None:
        inst = Instance(url="http://localhost:8080", token="tok")
        assert inst.url == "http://localhost:8080"

    def test_missing_protocol_raises(self) -> None:
        with pytest.raises(ValueError, match="URL must include protocol"):
            Instance(url="example.com", token="tok")

    def test_frozen(self) -> None:
        inst = Instance(url="https://a.com", token="t")
        with pytest.raises(AttributeError):
            inst.url = "https://b.com"  # type: ignore[misc]


class TestNormalizeUrl:
    def test_bare_hostname(self) -> None:
        assert normalize_url("example.com") == "https://example.com"

    def test_https_passthrough(self) -> None:
        assert normalize_url("https://example.com") == "https://example.com"

    def test_http_passthrough(self) -> None:
        assert normalize_url("http://localhost:8080") == "http://localhost:8080"


class TestMultiConfigSaveLoad:
    def test_roundtrip(self, tmp_config: Path) -> None:
        original = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="tok-a"),
                "b": Instance(url="https://b.com", token="tok-b"),
            },
            default="a",
        )
        original.save(tmp_config)
        loaded = MultiConfig.load(tmp_config)
        assert loaded.default_instance == "a"
        assert len(loaded.instances) == 2
        assert loaded.instances["a"].url == "https://a.com"
        assert loaded.instances["b"].token == "tok-b"

    def test_load_legacy_format(self, tmp_config: Path) -> None:
        with open(tmp_config, "wb") as f:
            tomli_w.dump({"url": "https://old.com", "token": "old-tok"}, f)
        loaded = MultiConfig.load(tmp_config)
        assert loaded.default_instance == "default"
        assert loaded.instances["default"].url == "https://old.com"
        assert loaded.instances["default"].token == "old-tok"

    def test_load_missing_file(self, tmp_config: Path) -> None:
        with pytest.raises(ConfigFileNotFoundError, match="not found"):
            MultiConfig.load(tmp_config)

    def test_load_empty_file(self, tmp_config: Path) -> None:
        tmp_config.write_bytes(b"")
        loaded = MultiConfig.load(tmp_config)
        assert len(loaded.instances) == 0
        assert loaded.default_instance is None

    def test_load_malformed_instance(self, tmp_config: Path) -> None:
        with open(tmp_config, "wb") as f:
            tomli_w.dump({"instances": {"bad": {"url": 123}}}, f)
        with pytest.raises(ConfigInvalidError, match="malformed"):
            MultiConfig.load(tmp_config)

    def test_load_invalid_toml_syntax(self, tmp_config: Path) -> None:
        tmp_config.write_bytes(b"[invalid toml {{{")
        with pytest.raises(ConfigInvalidError, match="invalid TOML syntax"):
            MultiConfig.load(tmp_config)

    def test_load_instances_not_a_table(self, tmp_config: Path) -> None:
        with open(tmp_config, "wb") as f:
            tomli_w.dump({"instances": "not-a-table"}, f)
        with pytest.raises(ConfigInvalidError, match="malformed"):
            MultiConfig.load(tmp_config)

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "config.toml"
        _make_multi(
            instances={"x": Instance(url="https://x.com", token="t")},
            default="x",
        ).save(path)
        loaded = MultiConfig.load(path)
        assert loaded.instances["x"].url == "https://x.com"


class TestMultiConfigResolve:
    def test_explicit_name(self) -> None:
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        assert multi.resolve(instance_name="b").url == "https://b.com"

    def test_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        monkeypatch.setenv("OH_INSTANCE", "b")
        assert multi.resolve().url == "https://b.com"

    def test_default_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OH_INSTANCE", raising=False)
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        assert multi.resolve().url == "https://a.com"

    def test_no_default_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OH_INSTANCE", raising=False)
        multi = _make_multi(
            instances={"only": Instance(url="https://only.com", token="t")},
        )
        with pytest.raises(InstanceNotFoundError, match="No default instance set"):
            multi.resolve()

    def test_nonexistent_name_raises(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
            default="a",
        )
        with pytest.raises(InstanceNotFoundError, match="not found"):
            multi.resolve(instance_name="nope")

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        monkeypatch.setenv("OH_INSTANCE", "a")
        assert multi.resolve(instance_name="b").url == "https://b.com"

    def test_env_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        monkeypatch.setenv("OH_INSTANCE", "b")
        assert multi.resolve().url == "https://b.com"


class TestMultiConfigEvolve:
    def test_evolve_default(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
            default="a",
        )
        evolved = multi.evolve(default_instance="b")
        assert evolved.default_instance == "b"
        assert multi.default_instance == "a"  # original unchanged

    def test_evolve_instances(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
        )
        new_instances = dict(multi.instances)
        new_instances["b"] = Instance(url="https://b.com", token="t2")
        evolved = multi.evolve(instances=new_instances)
        assert "b" in evolved.instances
        assert "b" not in multi.instances  # original unchanged


class TestUpsertInstance:
    def test_add_to_empty_no_default(self) -> None:
        multi = _make_multi()
        result = multi.upsert_instance("a", Instance(url="https://a.com", token="t"))
        assert "a" in result.instances
        assert result.default_instance is None

    def test_set_default_true(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
            default="a",
        )
        result = multi.upsert_instance("b", Instance(url="https://b.com", token="t"), set_default=True)
        assert result.default_instance == "b"

    def test_preserves_existing_default(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
            default="a",
        )
        result = multi.upsert_instance("b", Instance(url="https://b.com", token="t"))
        assert result.default_instance == "a"
        assert "b" in result.instances

    def test_replaces_existing_instance(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="old")},
            default="a",
        )
        result = multi.upsert_instance("a", Instance(url="https://a.com", token="new"))
        assert result.instances["a"].token == "new"

    def test_original_unchanged(self) -> None:
        multi = _make_multi()
        multi.upsert_instance("a", Instance(url="https://a.com", token="t"))
        assert "a" not in multi.instances


class TestRemoveInstance:
    def test_remove_non_default(self) -> None:
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        result = multi.remove_instance("b")
        assert "b" not in result.instances
        assert result.default_instance == "a"

    def test_remove_default_auto_selects_new(self) -> None:
        multi = _make_multi(
            instances={
                "a": Instance(url="https://a.com", token="t"),
                "b": Instance(url="https://b.com", token="t"),
            },
            default="a",
        )
        result = multi.remove_instance("a")
        assert "a" not in result.instances
        assert result.default_instance == "b"

    def test_remove_last_instance(self) -> None:
        multi = _make_multi(
            instances={"only": Instance(url="https://only.com", token="t")},
            default="only",
        )
        result = multi.remove_instance("only")
        assert len(result.instances) == 0
        assert result.default_instance is None

    def test_remove_nonexistent_raises(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
        )
        with pytest.raises(InstanceNotFoundError, match="nope"):
            multi.remove_instance("nope")

    def test_original_unchanged(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
            default="a",
        )
        multi.remove_instance("a")
        assert "a" in multi.instances


class TestGetInstance:
    def test_found(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
        )
        assert multi.get_instance("a").url == "https://a.com"

    def test_not_found(self) -> None:
        multi = _make_multi(
            instances={"a": Instance(url="https://a.com", token="t")},
        )
        with pytest.raises(InstanceNotFoundError, match="nope"):
            multi.get_instance("nope")

    def test_not_found_lists_available(self) -> None:
        multi = _make_multi(
            instances={
                "alpha": Instance(url="https://a.com", token="t"),
                "beta": Instance(url="https://b.com", token="t"),
            },
        )
        with pytest.raises(InstanceNotFoundError, match="alpha, beta"):
            multi.get_instance("nope")
