"""HTTP client for the openhost-cert-api broker.

The broker holds the ACME account and issues certs for a CSR after the instance
proves DNS control.  The instance never sees ACME account credentials — it only
sends a CSR and publishes the DNS-01 TXT records the broker hands back.

See the broker contract: github.com/imbue-openhost/openhost-cert-api.
"""

from __future__ import annotations

from types import TracebackType

import attr
import httpx

# finalize_order status values returned by the broker.
FINALIZE_STATUS_VALID = "valid"
FINALIZE_STATUS_PENDING = "pending"


class CertApiError(RuntimeError):
    """Base error for any unexpected broker response."""


class CertApiBadRequest(CertApiError):
    """The broker rejected the request body (HTTP 400)."""


class CertApiUnauthorized(CertApiError):
    """The per-instance bearer token was missing or rejected (HTTP 401)."""


class CertApiNotFound(CertApiError):
    """The referenced order does not exist (HTTP 404)."""


@attr.s(auto_attribs=True, frozen=True)
class CertChallenge:
    """A single DNS-01 challenge the instance must publish, verbatim, via CoreDNS."""

    domain: str
    # FQDN to create the TXT record at, e.g. "_acme-challenge.app.example.com".
    record_name: str
    # TXT value to publish.  Computed by the broker against ITS account key, so
    # it must be used exactly as given.
    record_value: str


@attr.s(auto_attribs=True, frozen=True)
class CreateOrderResult:
    order_id: str
    challenges: list[CertChallenge]


@attr.s(auto_attribs=True, frozen=True)
class FinalizeResult:
    # FINALIZE_STATUS_VALID or FINALIZE_STATUS_PENDING.
    status: str
    # PEM certificate chain when status is valid, otherwise None (still pending).
    certificate: str | None


_STATUS_TO_ERROR: dict[int, type[CertApiError]] = {
    400: CertApiBadRequest,
    401: CertApiUnauthorized,
    404: CertApiNotFound,
}


def _raise_for_unexpected(response: httpx.Response, expected: set[int]) -> None:
    """Raise a mapped CertApiError if the response status is not one we expect."""
    if response.status_code in expected:
        return
    message = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = f"{body.get('error', 'error')}: {body.get('message', response.text)}"
    error_cls = _STATUS_TO_ERROR.get(response.status_code, CertApiError)
    raise error_cls(message)


@attr.s(auto_attribs=True, frozen=True)
class CertApiClient:
    """Thin synchronous client over the broker's REST API.

    Construct via ``CertApiClient.create(base_url, token)`` in production; tests
    inject an ``httpx.Client`` backed by a MockTransport.
    """

    http_client: httpx.Client

    @classmethod
    def create(cls, base_url: str, token: str, timeout: float = 30.0) -> CertApiClient:
        http_client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        return cls(http_client=http_client)

    def __enter__(self) -> CertApiClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.http_client.close()

    def health(self) -> bool:
        """Return True if the broker reports healthy.  Does not require auth."""
        response = self.http_client.get("/health")
        if response.status_code != 200:
            return False
        body = response.json()
        return isinstance(body, dict) and body.get("status") == "ok"

    def create_order(self, csr_pem: str) -> CreateOrderResult:
        """Submit a CSR and receive the DNS-01 challenge record(s) to publish."""
        response = self.http_client.post("/v1/orders", json={"csr": csr_pem})
        _raise_for_unexpected(response, {200})
        body = response.json()
        challenges = [
            CertChallenge(
                domain=challenge["domain"],
                record_name=challenge["record_name"],
                record_value=challenge["record_value"],
            )
            for challenge in body["challenges"]
        ]
        return CreateOrderResult(order_id=body["order_id"], challenges=challenges)

    def finalize_order(self, order_id: str) -> FinalizeResult:
        """Poll the order.  200 -> issued (certificate present); 202 -> still pending."""
        response = self.http_client.post(f"/v1/orders/{order_id}/finalize")
        _raise_for_unexpected(response, {200, 202})
        if response.status_code == 202:
            return FinalizeResult(status=FINALIZE_STATUS_PENDING, certificate=None)
        body = response.json()
        return FinalizeResult(status=body["status"], certificate=body.get("certificate"))
