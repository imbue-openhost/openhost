from pathlib import Path

from schemathesis.checks import CHECKS
from schemathesis.checks import load_all_checks
from schemathesis.openapi import from_path

SPEC_PATH = Path(__file__).resolve().parents[3] / "services" / "oauth" / "openapi.yaml"

load_all_checks()
positive_data_acceptance = CHECKS.get_one("positive_data_acceptance")

schema = from_path(str(SPEC_PATH))


@schema.parametrize()
def test_openapi_conformance(case, oauth_app_url: str) -> None:
    case.call_and_validate(
        base_url=f"{oauth_app_url}/oauth_service",
        excluded_checks=[positive_data_acceptance],
    )
