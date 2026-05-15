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
