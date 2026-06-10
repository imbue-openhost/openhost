import json
import os
from pathlib import Path

import attr
from acme import client
from acme import messages
from cryptography.hazmat.primitives.asymmetric import rsa
from josepy import JWKRSA  # type: ignore[attr-defined]

from compute_space.config import CERT_PROVIDER_BYO
from compute_space.config import CERT_PROVIDER_EAB_MINT
from compute_space.core.logging import logger
from compute_space.core.tls.cert_api_client import EABCredential
from compute_space.core.tls.cert_api_client import mint_eab
from compute_space.core.tls.util import load_account_key

_USER_AGENT = "openhost-router/0.1"


@attr.s(auto_attribs=True, frozen=True)
class ResolvedAccount:
    """The account key to issue with, plus the ACME directory it belongs to.

    In eab_mint mode the cert-api dictates the directory (prod vs staging GTS),
    so issuance must use the same one the account was created against.
    """

    account_key: JWKRSA
    directory_url: str


def _generate_account_key() -> JWKRSA:
    """Generate this instance's OWN ACME account key (RSA-2048, certbot JWK format)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return JWKRSA(key=private_key)


def persist_account_key(account_key: JWKRSA, path: Path) -> None:
    """Persist an ACME account key as a certbot JWK JSON file with 0600 perms.

    Persisting lets renewals reuse the account key without ever calling the
    cert-api again.  A lost key simply means a fresh EAB is minted next boot.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(account_key.to_json(), f)
    os.chmod(path, 0o600)


def _register_account_with_eab(
    *,
    account_key: JWKRSA,
    directory_url: str,
    eab_credential: EABCredential,
    acme_email: str | None,
    verify_ssl: bool,
) -> messages.RegistrationResource:
    """Register a NEW ACME account bound to a single-use EAB credential (RFC 8555 §7.3.4)."""
    net = client.ClientNetwork(account_key, user_agent=_USER_AGENT, timeout=30, verify_ssl=verify_ssl)
    directory = messages.Directory.from_json(net.get(directory_url).json())
    acme_client = client.ClientV2(directory, net)

    eab = messages.ExternalAccountBinding.from_data(
        account_public_key=account_key.public_key(),
        kid=eab_credential.kid,
        hmac_key=eab_credential.hmac_key,
        directory=directory,
        hmac_alg=eab_credential.hmac_alg,
    )
    if acme_email:
        reg = messages.NewRegistration.from_data(
            email=acme_email, external_account_binding=eab, terms_of_service_agreed=True
        )
    else:
        reg = messages.NewRegistration.from_data(external_account_binding=eab, terms_of_service_agreed=True)

    account = acme_client.new_account(reg)
    logger.info(f"Created EAB-bound ACME account: {account.uri}")
    return account


def ensure_account_key(
    *,
    mode: str,
    account_key_path: Path,
    directory_url: str,
    cert_api_url: str | None,
    cert_api_token: str | None,
    zone_domain: str,
    acme_email: str | None = None,
    verify_ssl: bool = True,
) -> ResolvedAccount:
    """Return the ACME account key (+ its directory), minting one via EAB if needed.

    This is the only step that differs between cert provider modes; the DNS-01
    issuance core is shared.  Behaviour:

    - If a key is already persisted at ``account_key_path``: load it and pair it
      with the caller's ``directory_url``.  This covers BOTH renewals (eab_mint)
      and bring-your-own — and never contacts the cert-api.  (On renewal the
      directory comes from config; keep ``acme_directory_url`` aligned with what
      the cert-api used — they match for the prod-GTS default.)
    - ``byo`` mode with no key: fail loudly (operator must supply one).
    - ``eab_mint`` mode with no key (first boot): mint a single-use EAB from the
      cert-api, generate this instance's OWN account key, register a new ACME
      account bound to that EAB against the directory the cert-api returns,
      persist the key, and return it paired with that directory.
    """
    if account_key_path.exists():
        logger.info(f"Using existing ACME account key at {account_key_path}")
        return ResolvedAccount(account_key=load_account_key(account_key_path), directory_url=directory_url)

    if mode == CERT_PROVIDER_BYO:
        raise RuntimeError(
            f"cert_provider='byo' requires an existing ACME account key at {account_key_path}, but none was found. "
            f"Generate and register one (see scripts/generate_acme_key.py)."
        )
    if mode != CERT_PROVIDER_EAB_MINT:
        raise RuntimeError(f"Unknown cert_provider mode: {mode!r}")

    if not cert_api_url:
        raise RuntimeError("cert_provider='eab_mint' requires cert_api_url to be set")

    logger.info("No persisted ACME account key found; minting EAB and registering a new account (eab_mint mode)")
    eab_credential = mint_eab(cert_api_url, zone_domain, token=cert_api_token, verify_ssl=verify_ssl)
    # The cert-api is authoritative about which directory the EAB is valid for.
    effective_directory_url = eab_credential.directory_url
    account_key = _generate_account_key()
    _register_account_with_eab(
        account_key=account_key,
        directory_url=effective_directory_url,
        eab_credential=eab_credential,
        acme_email=acme_email,
        verify_ssl=verify_ssl,
    )
    persist_account_key(account_key, account_key_path)
    logger.info(f"Registered new EAB-bound ACME account and persisted key to {account_key_path}")
    return ResolvedAccount(account_key=account_key, directory_url=effective_directory_url)
