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

function setActionsBusy(label) {
  var container = document.getElementById('app-action-buttons');
  if (!container) return null;
  var buttons = container.querySelectorAll('button');
  buttons.forEach(function(b) { b.disabled = true; });
  var msg = document.getElementById('app-action-msg');
  if (msg) {
    msg.style.color = '#d97706';
    msg.textContent = label + '\u2026';
  }
  return function clear(errText) {
    buttons.forEach(function(b) { b.disabled = false; });
    if (msg) {
      if (errText) {
        msg.style.color = '#dc3545';
        msg.textContent = errText;
      } else {
        msg.textContent = '';
      }
    }
  };
}

function appAction(url, data, opts) {
  // opts: {isRemove: bool, label: string}. For backward compat, opts may be passed
  // as a boolean meaning isRemove (the old signature used `appAction(url, data, true)`).
  if (opts === true || opts === false) opts = {isRemove: opts};
  opts = opts || {};
  var label = opts.label || (opts.isRemove ? 'Removing' : 'Working');
  var fd = new FormData();
  if (data) Object.keys(data).forEach(function(k) { fd.append(k, data[k]); });
  var clear = setActionsBusy(label);
  fetch(url, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok || (res.data && res.data.error)) {
        var msg = (res.data && res.data.error) || 'Request failed';
        if (clear) clear(msg);
        alert(msg);
        return;
      }
      if (opts.isRemove) { window.location.href = '/dashboard'; }
      else { location.reload(); }
    })
    .catch(function() {
      if (clear) clear('Request failed');
      alert('Request failed');
    });
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

function clearCacheAndReload() {
    showToast('Clearing build cache...', []);
    fetch(config.dropBuildCacheUrl, {method: 'POST', credentials: 'same-origin'})
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.ok) { alert('Failed to clear cache: ' + (data.error || 'unknown error')); return; }
            appAction(config.reloadAppUrl, null, {label: 'Reloading'});
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

    // Reflect server status='removing' in the page chrome: disable the
    // action buttons + show "Removing…" beside them. We don't redirect
    // immediately on 'removing' because the row still exists; we wait
    // for the row to disappear (404) and then redirect to /dashboard.
    var removingApplied = false;
    function applyRemovingChrome() {
        if (removingApplied) return;
        removingApplied = true;
        setActionsBusy('Removing');
    }

    function pollStatus() {
        fetch(config.appStatusUrl)
            .then(function(r) {
                if (r.status === 404) {
                    // App removal completed — bounce to the dashboard.
                    window.location.href = '/dashboard';
                    return null;
                }
                return r.json();
            })
            .then(function(data) {
                if (!data) return;
                if (data.status !== appStatus) {
                    appStatus = data.status;
                    statusEl.textContent = appStatus;
                    statusEl.className = 'status-' + appStatus;
                }
                if (appStatus === 'removing') {
                    applyRemovingChrome();
                }
                if (appStatus === 'running' && nextUrl) {
                    window.location.href = nextUrl;
                }
                if (appStatus === 'error' && data.error_kind === 'build_cache_corrupt') {
                    showCacheCorruptToast();
                }
            });
    }

    // Check on initial load too (for when you navigate to an already-errored app)
    if (appStatus === 'error') {
        fetch(config.appStatusUrl)
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.error_kind === 'build_cache_corrupt') showCacheCorruptToast();
            });
    }

    // If the page loads while the app is already in the middle of a
    // removal (user reloaded the tab, or opened the detail page in a
    // second window), reflect that immediately so the buttons are
    // disabled before the first poll completes.
    if (appStatus === 'removing') {
        applyRemovingChrome();
    }

    // Scroll to bottom on load
    logEl.scrollTop = logEl.scrollHeight;

    // Poll while app is active — faster during builds for streaming output.
    // 'removing' is included so the page learns when the app row goes
    // away (transition to 404 → redirect to /dashboard).
    if (
        appStatus === 'running' ||
        appStatus === 'starting' ||
        appStatus === 'building' ||
        appStatus === 'removing'
    ) {
        var interval = (appStatus === 'building') ? 1000 : 3000;
        setInterval(fetchLogs, interval);
        setInterval(pollStatus, interval);
    }
})();
