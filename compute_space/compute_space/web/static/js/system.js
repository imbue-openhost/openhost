var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── Security Audit ───

fetch(config.securityAuditUrl).then(function(r) { return r.json(); }).then(function(data) {
  var el = document.getElementById('security-status');
  if (data.secure) {
    el.innerHTML = '<div style="background: #d4edda; border: 1px solid #28a745; padding: 0.8em 1em; border-radius: 4px;">'
      + '<strong>&#x2705; Security audit passed</strong> &mdash; all checks OK</div>';
  } else {
    var failed = Object.entries(data.checks).filter(function(e) { return !e[1].ok; });
    var details = failed.map(function(e) { return '<li><strong>' + e[0] + '</strong>: ' + e[1].detail + '</li>'; }).join('');
    el.innerHTML = '<div style="background: #f8d7da; border: 1px solid #dc3545; padding: 0.8em 1em; border-radius: 4px;">'
      + '<strong>&#x26A0;&#xFE0F; Security audit failed</strong><ul style="margin: 0.5em 0 0 1em;">' + details + '</ul></div>';
  }
});

// ─── Storage Status ───

function formatBytes(bytes) {
  if (bytes >= 1099511627776) return (bytes / 1099511627776).toFixed(1) + ' TiB';
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GiB';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MiB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return bytes + ' B';
}

function toggleStorageGuard(pause) {
  var fd = new FormData();
  fd.append('paused', pause ? '1' : '0');
  fetch(config.toggleStorageGuardUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function() { updateStorageStatus(); });
}

function updateStorageStatus() {
  fetch(config.storageStatusUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var el = document.getElementById('storage-status');
      var disk = data.disk || {};
      var hasMinFree = data.storage_min_free_bytes != null;
      var isLow = data.storage_low || false;
      var guardPaused = data.guard_paused || false;

      var body = 'Disk free: ' + formatBytes(disk.free_bytes || 0)
        + ' / ' + formatBytes(disk.total_bytes || 0);

      if (hasMinFree) {
        body += ' (min ' + formatBytes(data.storage_min_free_bytes) + ' required)';
      }

      body += '<br>OpenHost data: ' + formatBytes(data.openhost_data_used_bytes || 0)
        + ' &middot; App data: ' + formatBytes(data.app_data_used_bytes || 0);

      // Per-app usage breakdown
      var perApp = data.per_app || {};
      var appNames = Object.keys(perApp).sort();
      if (appNames.length > 0) {
        body += '<br><span style="font-size:0.9em;">Per app: ';
        body += appNames.map(function(name) { return name + '&nbsp;' + formatBytes(perApp[name]); }).join(' &middot; ');
        body += '</span>';
      }

      // Guard toggle (only when min-free threshold is configured)
      var guardHtml = '';
      if (hasMinFree) {
        if (guardPaused) {
          guardHtml = '<div style="margin-top:0.5em;padding:0.4em 0.6em;background:#fff3cd;border:1px solid #ffc107;border-radius:3px;font-size:0.9em;">'
            + 'Storage guard paused &mdash; apps will not be stopped for low storage. '
            + '<button class="btn" onclick="toggleStorageGuard(false)" style="font-size:0.85em;">Resume Guard</button></div>';
        } else if (isLow) {
          guardHtml = '<div style="margin-top:0.5em;font-size:0.9em;">'
            + '<button class="btn" onclick="toggleStorageGuard(true)" style="font-size:0.85em;">Pause Guard</button> '
            + 'Pause to restart an app and clean up data.</div>';
        }
      }

      if (!isLow) {
        el.innerHTML = '<div style="background:#e8f5e9;border:1px solid #43a047;padding:0.8em 1em;border-radius:4px;">'
          + '<strong>&#x1F4BE; Storage</strong><div style="margin-top:0.35em;">' + body + '</div>'
          + guardHtml + '</div>';
      } else {
        el.innerHTML = '<div style="background:#fff8e1;border:1px solid #f59e0b;padding:0.8em 1em;border-radius:4px;">'
          + '<strong>&#x26A0;&#xFE0F; Storage low</strong><div style="margin-top:0.35em;">' + body + '</div>'
          + guardHtml + '</div>';
      }
    });
}

updateStorageStatus();
setInterval(updateStorageStatus, 5000);

// ─── Router Restart ───

function restartRouter() {
  if (confirm('Restart the router service? All apps will briefly go offline.')) {
    fetch(config.restartRouterUrl, {method: 'POST', credentials: 'same-origin'}).then(function() {
      document.getElementById('restart-msg').textContent = 'Restarting...';
    });
  }
}

function dropBuildCache() {
  if (!confirm(
    'Drop container build cache?\n\n' +
    'Running containers will not be stopped, but images for stopped apps will be removed and rebuilt on next deploy.'
  )) return;

  var btn = document.getElementById('drop-build-cache-btn');
  var msg = document.getElementById('drop-build-cache-msg');
  btn.disabled = true;
  msg.style.color = '#d97706';
  msg.textContent = 'Dropping cache...';

  fetch(config.dropBuildCacheUrl, {method: 'POST', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        msg.style.color = '#dc3545';
        msg.textContent = 'Drop failed: ' + data.error;
        return;
      }
      var reclaimed = '';
      if (data.output) {
        var match = data.output.match(/Total reclaimed space:\s*(.+)/i);
        if (match && match[1]) {
          reclaimed = ' Freed ' + match[1] + '.';
        }
      }
      msg.style.color = '#16a34a';
      msg.textContent = 'Build cache dropped.' + reclaimed;
    })
    .catch(function() {
      msg.style.color = '#dc3545';
      msg.textContent = 'Drop failed: request error';
    })
    .then(function() {
      btn.disabled = false;
    });
}

// ─── SSH Toggle ───

function updateSshStatus() {
  fetch(config.sshStatusUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var btn = document.getElementById('ssh-btn');
      var status = document.getElementById('ssh-status');
      btn.disabled = false;
      if (data.ssh_enabled) {
        btn.textContent = 'Disable SSH';
        btn.className = 'btn btn-danger';
        status.textContent = 'SSH active';
        status.style.color = '#dc3545';
      } else {
        btn.textContent = 'Enable SSH';
        btn.className = 'btn';
        status.textContent = 'SSH disabled';
        status.style.color = '#6b7280';
      }
    });
}

function toggleSsh() {
  var btn = document.getElementById('ssh-btn');
  btn.disabled = true;
  btn.textContent = '...';
  fetch(config.toggleSshUrl, {method: 'POST', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function() { updateSshStatus(); });
}

updateSshStatus();
setInterval(updateSshStatus, 5000);
