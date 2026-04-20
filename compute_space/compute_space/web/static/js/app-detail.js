var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── Rename ───

function editName() {
  document.getElementById('name-display').style.display = 'none';
  document.getElementById('name-edit').style.display = '';
  document.getElementById('name-input').focus();
  document.getElementById('name-error').textContent = '';
}

function cancelName() {
  document.getElementById('name-edit').style.display = 'none';
  document.getElementById('name-display').style.display = '';
}

function saveName() {
  var input = document.getElementById('name-input');
  var errEl = document.getElementById('name-error');
  var fd = new FormData();
  fd.append('name', input.value);
  fetch(config.renameAppUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok) { errEl.textContent = res.data.error; return; }
      window.location.href = config.appDetailUrlTemplate.replace('__NAME__', res.data.name);
    });
}

// ─── App Actions (stop, reload, remove) ───

function appAction(url, data, isRemove) {
  var fd = new FormData();
  if (data) Object.keys(data).forEach(function(k) { fd.append(k, data[k]); });
  fetch(url, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json(); })
    .then(function(res) {
      if (res.error) { alert(res.error); return; }
      if (isRemove) { window.location.href = '/dashboard'; }
      else { location.reload(); }
    })
    .catch(function() { alert('Request failed'); });
}

// ─── Permissions ───

function permAction(app, key, action, btn) {
  btn.disabled = true;
  fetch('/api/permissions/' + action, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({app: app, permissions: [key]})
  })
    .then(function(r) { return r.json(); })
    .then(function() { location.reload(); })
    .catch(function() { alert('Request failed'); btn.disabled = false; });
}

// ─── Toast ───

function showToast(message, actions) {
    var existing = document.querySelector('.toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'toast';
    var p = document.createElement('p');
    p.textContent = message;
    toast.appendChild(p);
    var actionsDiv = document.createElement('div');
    actionsDiv.className = 'toast-actions';
    actions.forEach(function(a) {
        var btn = document.createElement('button');
        btn.className = 'btn' + (a.primary ? ' btn-primary' : '');
        btn.textContent = a.label;
        btn.onclick = function() { toast.remove(); a.onClick(); };
        actionsDiv.appendChild(btn);
    });
    toast.appendChild(actionsDiv);
    document.body.appendChild(toast);
    return toast;
}

// Accept both the new and legacy error-kind strings so a browser that
// picked up an older router's JSON response still shows the toast.
function isCacheCorruptKind(kind) {
    return kind === 'build_cache_corrupt' || kind === 'cache_corrupt';
}

function clearCacheAndReload() {
    showToast('Clearing build cache...', []);
    fetch(config.dropDockerCacheUrl, {method: 'POST', credentials: 'same-origin'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.ok) { alert('Failed to clear cache: ' + (data.error || 'unknown error')); return; }
            appAction(config.reloadAppUrl);
        })
        .catch(function() { alert('Failed to clear cache'); });
}

// ─── Logs & Status Polling ───

(function() {
    var logEl = document.getElementById('app-logs');
    var statusEl = document.getElementById('app-status');
    var appStatus = config.appStatus;
    var nextUrl = config.nextUrl;
    var toastKey = 'cache-toast-shown-' + config.appStatusUrl;

    function isNearBottom(el) {
        return el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    }

    function updateLog(el, text) {
        var sel = window.getSelection();
        if (sel && !sel.isCollapsed && el.contains(sel.anchorNode)) return;
        var wasAtBottom = isNearBottom(el);
        el.textContent = text || 'No log output available.';
        if (wasAtBottom) el.scrollTop = el.scrollHeight;
    }

    function fetchLogs() {
        fetch(config.appLogsUrl)
            .then(function(r) { return r.text(); })
            .then(function(text) { updateLog(logEl, text); });
    }

    function showCacheCorruptToast() {
        if (sessionStorage.getItem(toastKey)) return;
        sessionStorage.setItem(toastKey, '1');
        showToast(
            'Container build cache is corrupted. Clear it and rebuild?',
            [
                { label: 'Clear Cache & Rebuild', primary: true, onClick: clearCacheAndReload },
                { label: 'Dismiss', primary: false, onClick: function() {} }
            ]
        );
    }

    function pollStatus() {
        fetch(config.appStatusUrl)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.status !== appStatus) {
                    appStatus = data.status;
                    statusEl.textContent = appStatus;
                    statusEl.className = 'status-' + appStatus;
                }
                if (appStatus === 'running' && nextUrl) {
                    window.location.href = nextUrl;
                }
                if (appStatus === 'error' && isCacheCorruptKind(data.error_kind)) {
                    showCacheCorruptToast();
                }
            });
    }

    // Check on initial load too (for when you navigate to an already-errored app)
    if (appStatus === 'error') {
        fetch(config.appStatusUrl)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (isCacheCorruptKind(data.error_kind)) showCacheCorruptToast();
            });
    }

    // Scroll to bottom on load
    logEl.scrollTop = logEl.scrollHeight;

    // Poll while app is active — faster during builds for streaming output
    if (appStatus === 'running' || appStatus === 'starting' || appStatus === 'building') {
        var interval = (appStatus === 'building') ? 1000 : 3000;
        setInterval(fetchLogs, interval);
        setInterval(pollStatus, interval);
    }
})();
