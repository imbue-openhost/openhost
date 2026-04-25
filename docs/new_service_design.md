### example usages

- general passwords / secrets, that apps can request access to
- oauth token provider - eg to get a token to access the user’s google account
- integrations with the user’s accounts at external services - to enable IFTTT kinda things
    - integrations with messaging applications - one app has your login+handles the complicated API for some messaging service, another app can just send or receive messages over a simple API.
- email server ↔ email client. IMAP or JMAP.
- jellyfin+sonar+radar+seer (seems mostly for piracy tho..?)
- overseer LLM interacts with an app-building app to make an app.
- health data provider
- matthew adds:
    - internal search indexes content from several private notes apps.
    - wrap and tweak some other service (mirroring permissions and/or passing them through?)

### requirements / constraints

- existing app APIs should be able to be exposed as services - don't wanna have to write a whole new API when one already exists. can be very complex too.
- we don't wanna have to gatekeep the service directory

### questions + rejected ideas

- strictly mandating openAPI specs and oauth scopes for permissions
    - oauth scopes don’t support resource-level scopes, just “actions”. these feel important to me.
    - mandating openAPI specs could work, just doesn’t seem critical rn, and could be limiting.
- how fine-grained/data-dependent should scopes be
    - just grant full access to a given service, no finer. clearly doesn’t work eg for secrets.
    - pre-defined keys with no/static parameters. doesn’t work to grant eg email reads for a specific email address.
    - string-only scopes. too limiting for fine-grained things.
        - i’m not sure about this one tbh.
        - eg github abandoned oauth scopes for fine-grained tokens for this reason.
        - i think the problem was the permuation of (granted_repos, granted_actions) is quite large?
- who enforces permissions
    - router. seems hard/annoying.
- who stores permissions
    - providing app. makes it so that you can’t see what permissions are granted to any given app (without querying all other apps?), which feels wrong. also makes global-scoped permissions not work.
- who hosts the “permission grant” page.
    - just the router - doesn’t work easily bc some permissions are data-scoped (eg based on available accounts, or based on data).
- what’s the “identifier” for a service
    - (settled on git repo url, altho it’s not ideal)
        - the reason for choosing it is that it’s guaranteed unique, and it also comes with a way to include metadata (docs, manifest, etc).
        - see go modules for a good implementation of this idea
    - static name / ID. unclear how these would be deconflicted, unless we maintain a central registry, which i don’t wanna do.
    - “duck typing” - don’t require a centralized definition, just somehow figure it out based on functionality. not clear at all how this would work - syntax (eg openapi spec) vs semantics.
    - use something like a package repository (eg pypi). packages names are guaranteed unique due to central repository. repository has its own API and format for packages, eg to handle versions and holding files/metadata/etc.
        - go modules use git repos instead of central repo
        - pypi also allows installing from git

### proposed new design

- a service is defined by a git repo url, eg `github.com/user/repo` that has a openhost_service.toml manifest file.
    - it can also be a subdirectory, `github.com/user/repo/sub/dir`
- a service is a collection of HTTP/websocket routes, that additionally validate permissions from the request header + handle missing permissions in a specific way. a service is provided by some app(s), and used by other app(s).
    - if you need bare TCP, wrap it in a websocket using eg wstunnel.
- versioning
    - (generally following the go modules design here)
    - in the service spec git repo, versions are labeled with semvar git tags (vX.Y.Z). x is breaking changes. y is new backwards-compat features. z is patches.).
        - `sub/dir/v1.1.1` syntax if the service lives at a subdirectory.
    - providers specify the exact version of the service they provide
    - consumers would use pip-style syntax. `~=1.2` means anything compatible with 1.2., ie `>1.2, <2.0`
    - provider app can host multiple major versions of a service, during migrations due to breaking changes.

- permissions table looks like
    - requesting app (or api token)
    - service id, as a git URL.
        - major (?) version is included
    - arbitrary json payload, defining details of the grant
    - scope - specific providing app vs any app implementing that service.
    - potentially an expiration
- in requesting app
    - in manifest
        - can specify specific global (service, key) pairs to grant at install time
            - `{service_url: URL, scope: "global", grant: ["key/SOME_SECRET"]}`
        - maybe a way to specify a need for app-scoped perms too
            - eg: i need access to email. don’t know the email address yet. request could be like:
            - `{service_url: URL, scope: "app", grant: ["{EMAIL_ADDRESS}/read", "{EMAIL_ADDRESS}/send]}`
            - {`EMAIL_ADDRESS`} is really just a placeholder so the reader of the request knows that it’ll get scoped to a specific address
    - at runtime
        - optionally perform lookup: what apps provide service_id. returns (app_id, app_current_name).
        - makes request to router, for (service_id, providing_app or “default”).
        - if permission is missing, gets a redirect_url. the user must be taken to this page. page can be either in the router (for simple global keys) or in the provider app (for app-scoped perms).
- in providing app
    - in manifest
        - defines the service it provides (by URL), and the endpoint for this (eg `api/`)
            - different (major) versions can be hosted at different endpoints. these will show up as different services.
    - routes
        - validates request against granted permissions. if insufficient, either returns required keys, or gives a URL to a fine-grained permissions grant page.
        - fine-grained permissions grant page (accessible to user-in-browser only). after grant, sends the granted perms to the router, as an app-scoped permission. cannot grant global scoped permissions from here.
    - at install
        - add a flag to enable/disable exposing the service. and a toggle at runtime.
- in router
    - proxies request to app, validating source and adding permissions headers relevant to that service.
    - a settings page where you can specify default providers for services that you have multiple providers of.
- in service repo
    - manifest file (openhost_service.toml)
        - not sure what is in here tbh.
        - encourage including an openAPI spec?
    - docs defining how the service works (and/or openAPI spec)
