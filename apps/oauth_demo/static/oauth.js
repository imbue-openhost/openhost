/**
 * Client-side OAuth helper library.
 *
 * Talks to the router's V2 service endpoints via cross-origin fetch:
 *   POST /_services_v2/<encoded-oauth-url>/accounts  — list connected accounts
 *   POST /_services_v2/<encoded-oauth-url>/token     — get access token
 *
 * Usage:
 *   const oauth = new OAuthClient({
 *     scopes: { google: ['https://...gmail...'], github: ['repo'] },
 *     appName: 'oauth-demo',
 *     zoneDomain: 'user.host.imbue.com',
 *   });
 *   const accounts = await oauth.getAccounts('google');
 *   const token = await oauth.getToken('google', 'user@gmail.com');
 *   await oauth.connect('google');  // must be called from a click handler
 */

var OAUTH_SERVICE_URL = "github.com/imbue-openhost/openhost/services/oauth";
var ENCODED_OAUTH_URL = encodeURIComponent(OAUTH_SERVICE_URL);

class OAuthClient {
  constructor({ scopes, appName, zoneDomain }) {
    this.scopes = scopes;
    this.appName = appName;
    this.zoneDomain = zoneDomain;
    this._popup = null;
  }

  _scopesFor(provider) {
    var s = this.scopes[provider];
    if (!s) throw new OAuthError("Unknown provider: " + provider);
    return s;
  }

  _returnTo() {
    return "//" + this.appName + "." + this.zoneDomain + "/client/oauth-complete";
  }

  /** POST to a V2 service endpoint with credentials. */
  _serviceFetch(endpoint, body) {
    var url = "//" + this.zoneDomain + "/_services_v2/" + ENCODED_OAUTH_URL +
      "/" + endpoint + "?version=>=0.1.0";
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: typeof body === "string" ? body : JSON.stringify(body),
    });
  }

  /**
   * Parse an error response. Returns:
   *   { type: "permission", url } for 403 permission_required (grant_url or approve_url)
   *   { type: "oauth", authorize_url } for 401 authorization_required
   *   { type: "error", message } for other errors
   *   null if response is ok
   */
  async _parseResponse(resp) {
    if (resp.ok) return null;
    var data;
    try { data = await resp.clone().json(); } catch (e) {
      return { type: "error", message: "Request failed: " + resp.status };
    }
    if (data.error === "permission_required" && data.grants_needed) {
      var grant = data.grants_needed[0] || {};
      var url = grant.grant_url || grant.approve_url || "";
      return { type: "permission", url: url };
    }
    if (data.approve_url) return { type: "permission", url: data.approve_url };
    if (data.authorize_url) return { type: "oauth", authorize_url: data.authorize_url };
    return { type: "error", message: data.message || "Request failed: " + resp.status };
  }

  /**
   * List connected accounts for a provider.
   * Throws OAuthPermissionError if permissions are needed.
   * Returns empty array on other errors.
   */
  async getAccounts(provider) {
    var resp = await this._serviceFetch("accounts", {
      provider: provider,
      scopes: this._scopesFor(provider),
    });
    var err = await this._parseResponse(resp);
    if (!err) return (await resp.json()).accounts;
    if (err.type === "permission") throw new OAuthPermissionError(err.url);
    return [];
  }

  /**
   * Get an OAuth access token. Must be called from a click handler.
   * Throws OAuthPermissionError if permissions need granting first.
   * Handles OAuth consent via popup automatically.
   */
  async getToken(provider, account = "default") {
    var resp = await this._serviceFetch("token", {
      provider: provider,
      scopes: this._scopesFor(provider),
      return_to: this._returnTo(),
      account: account,
    });
    var err = await this._parseResponse(resp);
    if (!err) return (await resp.json()).access_token;
    if (err.type === "permission") throw new OAuthPermissionError(err.url);
    if (err.type === "oauth") {
      await this.openPopup(err.authorize_url);
      // Retry after OAuth
      resp = await this._serviceFetch("token", {
        provider: provider,
        scopes: this._scopesFor(provider),
        return_to: this._returnTo(),
        account: account,
      });
      if (resp.ok) return (await resp.json()).access_token;
    }
    throw new OAuthError(err.message || "Token request failed");
  }

  /**
   * Connect a new account via popup. Must be called from a click handler.
   * Throws OAuthPermissionError if permissions need granting first.
   */
  async connect(provider, account = "new") {
    var resp = await this._serviceFetch("token", {
      provider: provider,
      scopes: this._scopesFor(provider),
      return_to: this._returnTo(),
      account: account,
    });
    if (resp.ok) return;
    var err = await this._parseResponse(resp);
    if (err.type === "permission") throw new OAuthPermissionError(err.url);
    if (err.type === "oauth") {
      await this.openPopup(err.authorize_url);
      return;
    }
    throw new OAuthError(err.message || "Connect failed");
  }

  /** Open a URL in a popup and wait for an auth_complete postMessage. */
  openPopup(url) {
    return new Promise(function (resolve, reject) {
      if (this._popup && !this._popup.closed) this._popup.close();
      this._popup = window.open(url, "auth_popup");

      if (!this._popup) {
        reject(new OAuthError("Popup blocked. Please allow popups and try again."));
        return;
      }

      var popup = this._popup;
      var cleanup = function () { window.removeEventListener("message", onMsg); clearInterval(poll); };
      var onMsg = function (e) {
        if (e.data && e.data.type === "auth_complete") { cleanup(); resolve(); }
      };
      var poll = setInterval(function () {
        if (popup.closed) { cleanup(); resolve(); }
      }, 500);
      window.addEventListener("message", onMsg);
    }.bind(this));
  }
}

class OAuthError extends Error {
  constructor(message) { super(message); this.name = "OAuthError"; }
}

class OAuthPermissionError extends Error {
  constructor(url) {
    super("Permissions required");
    this.name = "OAuthPermissionError";
    this.url = url;
  }
}
