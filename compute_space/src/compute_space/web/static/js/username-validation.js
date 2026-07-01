// Shared client-side owner-username validation used by both the setup
// form (setup.html) and the settings form (settings.html).
//
// This MIRRORS the server-side rule in
// compute_space/core/auth/auth.py (`_OWNER_USERNAME_RE` /
// `validate_owner_username`). It exists purely to give the operator
// immediate, specific feedback — e.g. when they paste an email or a
// Mastodon-style "@handle" — instead of a round-trip rejection. The
// server remains the authoritative source of truth; if these ever
// drift, the server wins.
(function (global) {
  // Lowercase alphanumeric start, then up to 29 more of lowercase
  // alphanumeric + . _ -, for a total length of 1..30. This matches the
  // server-side _OWNER_USERNAME_RE exactly.
  var USERNAME_RE = /^[a-z0-9][a-z0-9._-]{0,29}$/;

  // Returns an error string for an unacceptable value, or '' when the
  // value is acceptable. An empty value returns '' (callers decide
  // whether empty is allowed: setup treats blank as "use default",
  // settings disables Save on blank).
  function usernameError(value) {
    if (value === '') return '';
    if (value.length > 30) return 'Username must be at most 30 characters.';
    if (USERNAME_RE.test(value)) return '';
    if (/[A-Z]/.test(value)) return 'Username must be lowercase (no uppercase letters).';
    if (value.indexOf('@') !== -1) return 'Username cannot contain "@". Use just the name, e.g. "alice".';
    if (/\s/.test(value)) return 'Username cannot contain spaces.';
    if (/^[._-]/.test(value)) return 'Username must start with a lowercase letter or digit.';
    return 'Username may use only lowercase letters, digits, and . _ -';
  }

  global.OpenHostUsername = { USERNAME_RE: USERNAME_RE, usernameError: usernameError };
})(window);
