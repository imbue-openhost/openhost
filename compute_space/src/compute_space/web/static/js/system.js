var config = JSON.parse(document.getElementById('page-config').textContent);

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = (s == null) ? '' : String(s);
  return d.innerHTML;
}

function formatBytes(bytes) {
  if (bytes >= 1099511627776) return (bytes / 1099511627776).toFixed(1) + ' TiB';
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GiB';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MiB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return bytes + ' B';
}

// ─── Listening Ports ───

function updateListeningPorts() {
  fetch(config.listeningPortsUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var body = document.getElementById('ports-body');
      if (!data || data.enumeration_failed) {
        body.innerHTML = '<tr><td colspan="4" class="error">Could not enumerate listening ports.</td></tr>';
        return;
      }
      var ports = data.ports || [];
      if (!ports.length) {
        body.innerHTML = '<tr><td colspan="4" class="muted">No externally exposed ports.</td></tr>';
        return;
      }
      body.innerHTML = ports.map(function(p) {
        var cls = '';
        var label = escHtml(p.label);
        if (p.classification === 'unexpected') {
          cls = ' class="status-error"';
          label = '<strong>' + label + '</strong>';
        } else if (p.classification === 'secure') {
          cls = ' class="status-running"';
        }
        return '<tr><td><code>' + escHtml(p.port) + '</code></td>'
          + '<td><code>' + escHtml(p.address) + '</code></td>'
          + '<td' + cls + '>' + escHtml(p.classification) + '</td>'
          + '<td>' + label + '</td></tr>';
      }).join('');
    });
}

// ─── Storage Status ───

function toggleStorageGuard(pause) {
  fetch(config.toggleStorageGuardUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({paused: !!pause}),
  }).then(function() { updateStorageStatus(); });
}

function perAppBarsHtml(perApp) {
  var names = Object.keys(perApp).sort(function(a, b) { return perApp[b] - perApp[a]; });
  var maxBytes = names.length ? (perApp[names[0]] || 1) : 1;
  return names.map(function(name) {
    var pct = Math.max(1, Math.round((perApp[name] / maxBytes) * 100));
    return '<div class="usage-bar-row">'
      + '<span class="usage-bar-name">' + escHtml(name) + '</span>'
      + '<div class="usage-bar-track"><div class="usage-bar-fill" style="width:' + pct + '%"></div></div>'
      + '<span class="usage-bar-size">' + escHtml(formatBytes(perApp[name])) + '</span>'
      + '</div>';
  }).join('');
}

function updateStorageStatus() {
  fetch(config.storageStatusUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var disk = data.disk || {};
      var hasMinFree = data.storage_min_free_bytes != null;
      var isLow = !!data.storage_low;
      var guardPaused = !!data.guard_paused;

      var rows = '';
      var freeText = formatBytes(disk.free_bytes || 0) + ' / ' + formatBytes(disk.total_bytes || 0);
      if (hasMinFree) {
        freeText += ' (min ' + formatBytes(data.storage_min_free_bytes) + ' required)';
      }
      var freeCls = (hasMinFree && isLow) ? ' class="status-error"' : '';
      rows += '<tr><th>Disk free</th><td' + freeCls + '>' + escHtml(freeText) + '</td></tr>';
      rows += '<tr><th>OpenHost data</th><td>' + escHtml(formatBytes(data.openhost_data_used_bytes || 0)) + '</td></tr>';
      var buildCache = (data.build_cache_bytes == null)
        ? '<span class="muted">unavailable</span>'
        : escHtml(formatBytes(data.build_cache_bytes));
      rows += '<tr><th>Build cache</th><td>' + buildCache + '</td></tr>';
      rows += '<tr><th>App data</th><td>' + escHtml(formatBytes(data.app_data_used_bytes || 0)) + '</td></tr>';

      var perApp = data.per_app || {};
      if (Object.keys(perApp).length > 0) {
        rows += '<tr><th>Per app</th><td>' + perAppBarsHtml(perApp) + '</td></tr>';
      }

      if (hasMinFree) {
        var guardText = guardPaused ? 'Paused' : (isLow ? 'Active (low storage)' : 'Active');
        var guardCls = (guardPaused || isLow) ? ' class="status-error"' : '';
        rows += '<tr><th>Storage guard</th><td' + guardCls + '>' + escHtml(guardText) + '</td></tr>';
      }
      document.getElementById('storage-body').innerHTML = rows;

      // Guard toggle button (separate row below the table for clarity)
      var guardRow = document.getElementById('storage-guard-row');
      if (hasMinFree && guardPaused) {
        guardRow.innerHTML = '<div class="control-row"><button class="btn" onclick="toggleStorageGuard(false)">Resume Guard</button>'
          + '<span class="hint">Apps will not be stopped while paused.</span></div>';
      } else if (hasMinFree && isLow) {
        guardRow.innerHTML = '<div class="control-row"><button class="btn" onclick="toggleStorageGuard(true)">Pause Guard</button>'
          + '<span class="hint">Pause to start an app for cleanup.</span></div>';
      } else {
        guardRow.innerHTML = '';
      }
    });
}

// ─── Logs ───

function fetchLogs() {
  var logEl = document.getElementById('cs-logs');
  fetch('/api/compute_space_logs', {credentials: 'same-origin'})
    .then(function(r) { return r.text(); })
    .then(function(text) {
      var sel = window.getSelection();
      if (sel && !sel.isCollapsed && logEl.contains(sel.anchorNode)) return;
      var wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 50;
      logEl.textContent = text || 'No log output available.';
      if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
    });
}

// ─── Init ───

updateListeningPorts();
setInterval(updateListeningPorts, 10000);

updateStorageStatus();
setInterval(updateStorageStatus, 5000);

fetchLogs();
setInterval(fetchLogs, 3000);
