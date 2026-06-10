import attr
import httpx

from compute_space.core.logging import logger


@attr.s(auto_attribs=True, frozen=True)
class EABCredential:
    """A single-use External Account Binding credential minted by the cert-api.

    Maps the cert-api ``POST /v1/eab`` response onto what an ACME client needs:
    ``kid``/``hmac_key`` (RFC 8555 externalAccountBinding), the ``directory_url``
    the EAB was minted against (the service is authoritative — prod vs staging
    GTS), and the HMAC algorithm.  ``hmac_key`` is base64url-encoded, as GTS
    returns it, and is passed straight through to the ACME library.
    """

    kid: str
    hmac_key: str
    directory_url: str
    hmac_alg: str = "HS256"


def mint_eab(
    cert_api_url: str,
    domain: str,
    *,
    token: str | None = None,
    timeout: float = 30.0,
    verify_ssl: bool = True,
) -> EABCredential:
    """Mint a single-use EAB credential from the cert-api for this instance.

    Calls ``POST /v1/eab`` on the openhost-cert-api service (wire contract: that
    repo's README).  Called once at bootstrap; renewals reuse the persisted
    account key and never call here again.  ``domain`` is audit-only on the
    service side (an EAB grants no domain authorization; DNS-01 still gates
    issuance).  Auth is a per-instance bearer token provisioned by the operator.
    """
    endpoint = cert_api_url.rstrip("/") + "/v1/eab"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    logger.info(f"Minting single-use EAB credential from cert-api at {endpoint}")

    response = httpx.post(
        endpoint,
        json={"domain": domain},
        headers=headers,
        timeout=timeout,
        verify=verify_ssl,
    )

    if response.status_code != 200:
        raise RuntimeError(f"cert-api EAB mint failed: {_describe_error(response)}")

    payload = response.json()
    required = ("key_id", "b64_mac_key", "directory_url")
    if not isinstance(payload, dict) or any(field not in payload for field in required):
        raise RuntimeError(f"cert-api EAB response missing expected fields {required}: {payload!r}")

    return EABCredential(
        kid=payload["key_id"],
        hmac_key=payload["b64_mac_key"],
        directory_url=payload["directory_url"],
        hmac_alg=payload.get("key_algorithm", "HS256"),
    )


def _describe_error(response: httpx.Response) -> str:
    """Best-effort description of a cert-api error envelope ({error, message})."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = f" {body.get('error')}: {body.get('message')}"
    except ValueError:
        pass
    retry_after = response.headers.get("Retry-After")
    suffix = f" (Retry-After={retry_after}s)" if retry_after else ""
    return f"HTTP {response.status_code}{detail}{suffix}"
