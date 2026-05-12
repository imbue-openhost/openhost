var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── App List ───
//
// Action buttons are rendered server-side by the dashboard template;
// the polling loop only refreshes the status column. Server-side
// guards (409 on stop/reload/rename of a removing row) make any stray
// click a safe no-op.

function refreshApps() {
  if (!config.apiAppsUrl) return;
  fetch(config.apiAppsUrl)
    .then(function(r) { return r.json(); })
    .then(updateApps)
    .catch(function() {});
}

function appAction(appId, action, formData) {
  var opts = {method: 'POST', credentials: 'same-origin'};
  if (formData) { opts.body = formData; }
  return fetch(action + '/' + appId, opts)
    .then(function(r) {
      return r.json().then(function(d) { return {ok: r.ok, data: d}; },
                          function() { return {ok: r.ok, data: {}}; });
    })
    .then(function(res) {
      if (!res.ok || (res.data && res.data.error)) {
        alert((res.data && res.data.error) || 'Request failed');
      }
      refreshApps();
    })
    .catch(function() {
      alert('Request failed');
      refreshApps();
    });
}

function reloadAndUpdate(appId) {
  var fd = new FormData();
  fd.append('update', '1');
  appAction(appId, 'reload_app', fd);
}

function updateApps(apps) {
  // /api/apps now returns a list of {app_id, name, status, error_message}.
  var byId = {};
  apps.forEach(function(a) { byId[a.app_id] = a; });
  document.querySelectorAll('tr[data-app-id]').forEach(function(row) {
    var appId = row.getAttribute('data-app-id');
    var info = byId[appId];
    if (!info) {
      row.style.display = 'none';
      return;
    }
    row.style.display = '';
    var statusEl = row.querySelector('.app-status');
    statusEl.className = 'app-status status-' + info.status;
    statusEl.textContent = info.status;
  });
}

if (config.apiAppsUrl) {
  refreshApps();
  setInterval(refreshApps, 3000);
}

// ─── API Tokens ───

function loadTokens() {
  fetch(config.tokensListUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(tokens) {
      var tbody = document.getElementById('tokens-body');
      var table = document.getElementById('tokens-table');
      var noTokens = document.getElementById('no-tokens');
      if (!tokens.length) { table.style.display = 'none'; noTokens.style.display = ''; return; }
      table.style.display = ''; noTokens.style.display = 'none';
      tbody.innerHTML = tokens.map(function(t) {
        var style = t.expired ? ' style="color:#888;text-decoration:line-through;"' : '';
        var expiresDisplay = t.expires_at ? t.expires_at : 'Never';
        return '<tr><td' + style + '>' + t.name + '</td>'
          + '<td>' + t.created_at + '</td>'
          + '<td' + (t.expired ? ' style="color:#c00;"' : '') + '>' + expiresDisplay + '</td>'
          + '<td><button class="btn btn-danger" onclick="deleteToken(' + t.id + ')">Delete</button></td></tr>';
      }).join('');
    });
}

function createToken() {
  var fd = new FormData();
  fd.append('name', document.getElementById('token-name').value);
  if (document.getElementById('token-no-expiry').checked) {
    fd.append('expiry_hours', 'never');
  } else {
    fd.append('expiry_hours', document.getElementById('token-expiry').value);
  }
  fetch(config.tokensCreateUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) { alert(data.error); return; }
      document.getElementById('token-value').textContent = data.token;
      document.getElementById('token-created').style.display = '';
      document.getElementById('token-name').value = '';
      loadTokens();
    });
}

function deleteToken(id) {
  if (!confirm('Delete this token? Any agents using it will lose access.')) return;
  fetch(config.tokensListUrl + '/' + id, {method: 'DELETE', credentials: 'same-origin'})
    .then(function() { loadTokens(); });
}

loadTokens();
