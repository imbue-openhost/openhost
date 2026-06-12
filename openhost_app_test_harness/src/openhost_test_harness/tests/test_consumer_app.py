from pathlib import Path

from compute_space.core.manifest import parse_manifest
from openhost_test_harness.consumer_app import write_consumer_app_dir


def test_generated_consumer_app_is_valid(tmp_path: Path) -> None:
    write_consumer_app_dir(
        tmp_path / "consumer",
        name="my-consumer",
        service="github.com/example/echo",
        shortname="echo",
        version=">=0.1.0",
        grants=["read", {"key": "FOO"}],
    )
    assert (tmp_path / "consumer" / "Dockerfile").exists()
    assert (tmp_path / "consumer" / "server.py").exists()

    manifest = parse_manifest(str(tmp_path / "consumer"))
    assert manifest.name == "my-consumer"
    assert manifest.hidden is True
    consume = manifest.consumes_services_v2[0]
    assert consume.service == "github.com/example/echo"
    assert consume.shortname == "echo"
    assert consume.version == ">=0.1.0"
    assert consume.grants == ["read", {"key": "FOO"}]
