import attr


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedAccessor:
    pass


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedUser(AuthenticatedAccessor):
    username: str


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedAPIKey(AuthenticatedAccessor):
    pass


@attr.s(auto_attribs=True, frozen=True)
class AuthenticatedApp(AuthenticatedAccessor):
    app_id: str


def _try_refresh(connection: _AnyConnection, db: sqlite3.Connection) -> AuthenticatedUser | None:
    """Authenticate by validating the refresh-token cookie + the (allowed-expired) JWT cookie.

    Pure check: no side effects. ``AuthRefreshMiddleware`` separately decides whether to mint a
    fresh access cookie based on the same conditions.
    """
    if not (refresh_tok := connection.cookies.get(COOKIE_REFRESH)):
        return None
    if not (expired_jwt := connection.cookies.get(COOKIE_ACCESS)):
        return None
    if (expired_claims := decode_access_token_allow_expired(expired_jwt)) is None:
        return None

    refresh_tok_hash = hashlib.sha256(refresh_tok.encode()).hexdigest()
    rt = db.execute(
        "SELECT expires_at FROM refresh_tokens WHERE token_hash = ? AND revoked = 0",
        (refresh_tok_hash,),
    ).fetchone()
    if rt is None or datetime.fromisoformat(rt["expires_at"]) < datetime.now(UTC):
        return None

    return AuthenticatedUser(username=expired_claims["sub"])
