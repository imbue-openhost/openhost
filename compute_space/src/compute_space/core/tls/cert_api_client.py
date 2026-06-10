import attr
import httpx

from compute_space.core.logging import logger


@attr.s(auto_attribs=True, frozen=True)
class EABCredential:
    """A single-use External Account Binding credential minted by the cert-api.

    Per RFC 8555 externalAccountBinding: ``kid`` identifies the binding and
    ``hmac_key`` is the base64url-encoded MAC key used to HMAC-sign the newAccount
    request.  The credential only enables creating ONE ACME account; it does not
    grant cert issuance (DNS-01 control still gates that).
    """

    kid: str
    hmac_key: str


def mint_eab(
    cert_api_url: str,
    zone_domain: str,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    verify_ssl: bool = True,
) -> EABCredential:
    """Mint a single-use EAB credential from the cert-api for this instance's zone.

    Called once at bootstrap when no ACME account key is persisted yet.  Renewals
    reuse the persisted account key and never call here again.

    TODO(cert-api contract): the request/response wire format is owned by the
    separate ~/openhost-cert-api service and is not yet finalized.  This codes to
    a clean, documented seam — adjust the endpoint path / field names here once
    the contract lands:

        POST {cert_api_url}/eab
          headers: Authorization: Bearer <token>   (only if a token is configured)
          json:    {"zone_domain": <zone_domain>}
        -> 200 {"kid": "...", "hmac_key": "<base64url>"}
    """
    endpoint = cert_api_url.rstrip("/") + "/eab"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info(f"Minting single-use EAB credential from cert-api at {endpoint} for zone {zone_domain}")

    response = httpx.post(
        endpoint,
        json={"zone_domain": zone_domain},
        headers=headers,
        timeout=timeout,
        verify=verify_ssl,
    )
    response.raise_for_status()
    payload = response.json()

    # The response is external JSON of a not-yet-pinned shape; pull it into a
    # typed credential immediately and fail loudly if the fields are missing.
    if not isinstance(payload, dict) or "kid" not in payload or "hmac_key" not in payload:
        raise RuntimeError(f"cert-api EAB response missing expected fields (kid, hmac_key): {payload!r}")
    return EABCredential(kid=payload["kid"], hmac_key=payload["hmac_key"])
