from pathlib import Path
from typing import Any

from schemathesis import Case
from schemathesis.checks import CHECKS
from schemathesis.checks import CheckFunction
from schemathesis.checks import load_all_checks
from schemathesis.openapi import from_path

SPEC_PATH = Path(__file__).resolve().parents[5] / "services" / "oauth" / "openapi.yaml"

load_all_checks()
_check = CHECKS.get_one("positive_data_acceptance")
# The registry can also hold check classes; excluded_checks accepts functions only.
assert not isinstance(_check, type)
positive_data_acceptance: CheckFunction = _check

schema = from_path(str(SPEC_PATH))


@schema.parametrize()
def test_openapi_conformance(case: Case[Any], oauth_app_url: str) -> None:
    case.call_and_validate(
        base_url=f"{oauth_app_url}/oauth_service",
        excluded_checks=[positive_data_acceptance],
    )
