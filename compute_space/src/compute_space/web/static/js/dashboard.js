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

function appAction(appId, action, body) {
  var opts = {method: 'POST', credentials: 'same-origin'};
  if (body) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify(body);
  }
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
  appAction(appId, 'reload_app', {update: true});
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
