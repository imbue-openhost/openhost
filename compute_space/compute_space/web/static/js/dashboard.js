var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── App List ───

function appAction(name, action, formData) {
  var opts = {method: 'POST', credentials: 'same-origin'};
  if (formData) { opts.body = formData; }
  fetch(action + '/' + name, opts);
}

function reloadAndUpdate(name) {
  var fd = new FormData();
  fd.append('update', '1');
  appAction(name, 'reload_app', fd);
}

function renderActions(name, status) {
  var detailsLink = '<a href="app_detail/' + name + '">Details</a> ';
  var btns = '';
  btns = '<button class="btn" onclick="appAction(\'' + name + '\', \'reload_app\')">Reload</button> '
       + '<button class="btn" onclick="reloadAndUpdate(\'' + name + '\')">Reload &amp; Update</button> '
       + '<button class="btn btn-danger" onclick="if(confirm(\'Remove ' + name + ' and delete all data permanently?\')) appAction(\'' + name + '\', \'remove_app\')">Remove</button> ';
  return detailsLink + btns;
}

function updateApps(data) {
  document.querySelectorAll('tr[data-app]').forEach(function(row) {
    var name = row.getAttribute('data-app');
    var info = data[name];
    if (!info) { row.style.display = 'none'; return; }
    row.style.display = '';
    var statusEl = row.querySelector('.app-status');
    var actionsEl = row.querySelector('.app-actions');

    statusEl.className = 'app-status status-' + info.status;
    statusEl.textContent = info.status;

    actionsEl.innerHTML = renderActions(name, info.status);
  });
}

if (config.apiAppsUrl) {
  fetch(config.apiAppsUrl).then(function(r) { return r.json(); }).then(updateApps);
  setInterval(function() {
    fetch(config.apiAppsUrl).then(function(r) { return r.json(); }).then(updateApps);
  }, 3000);
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
