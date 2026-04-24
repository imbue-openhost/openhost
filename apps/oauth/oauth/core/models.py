import attr

# ─── Requests ───


@attr.s(auto_attribs=True, frozen=True)
class TokenRequest:
    provider: str
    scopes: list[str]
    account: str = "default"
    return_to: str = ""


@attr.s(auto_attribs=True, frozen=True)
class AccountsRequest:
    provider: str
    scopes: list[str]


@attr.s(auto_attribs=True, frozen=True)
class RevokeRequest:
    provider: str
    scopes: list[str]
    account: str


@attr.s(auto_attribs=True, frozen=True)
class MockProviderUrlData:
    provider: str
    redirect_uri: str
    authorize_url: str = ""
    device_url: str = ""
    token_url: str = ""
    revoke_url: str = ""
    userinfo_url: str = ""
    userinfo_field: str = ""


# ─── Responses ───


@attr.s(auto_attribs=True, frozen=True)
class TokenResponse:
    access_token: str
    token_type: str = "Bearer"
    expires_at: str | None = None


@attr.s(auto_attribs=True, frozen=True)
class AccountsResponse:
    accounts: list[str]


@attr.s(auto_attribs=True, frozen=True)
class GrantPayload:
    provider: str
    scopes: list[str]


@attr.s(auto_attribs=True, frozen=True)
class RequiredGrant:
    grant_payload: GrantPayload
    scope: str
    grant_url: str


@attr.s(auto_attribs=True, frozen=True)
class PermissionDeniedResponse:
    error: str
    required_grant: RequiredGrant


@attr.s(auto_attribs=True, frozen=True)
class AuthRequiredResponse:
    status: str
    authorize_url: str


@attr.s(auto_attribs=True, frozen=True)
class ErrorResponse:
    error: str
    message: str = ""
    provider: str = ""


@attr.s(auto_attribs=True, frozen=True)
class OkResponse:
    ok: bool = True


@attr.s(auto_attribs=True, frozen=True)
class CredentialsRequiredResponse:
    error: str
    message: str


@attr.s(auto_attribs=True, frozen=True)
class TokenInfo:
    id: int
    provider: str
    scopes: str
    account: str
    expires_at: str | None
    created_at: str
    updated_at: str


@attr.s(auto_attribs=True, frozen=True)
class TokenListResponse:
    tokens: list[TokenInfo]


@attr.s(auto_attribs=True, frozen=True)
class DevicePollResponse:
    status: str
    error: str = ""
