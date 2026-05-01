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

// ─── Archive Backend ───
//
// Operator-facing panel that lets the operator switch the app_archive
// storage tier between local-disk (default) and S3-backed (JuiceFS).
// The switch flow stops every opted-in app, copies data, brings up
// the new backend, then restarts the apps.  We poll while a switch
// is in flight so the operator can see progress.

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderArchiveBackend(state) {
  var el = document.getElementById('archive-backend-status');
  var bgColor = state.backend === 's3' ? '#e3f2fd' : '#f5f5f5';
  var borderColor = state.backend === 's3' ? '#2196f3' : '#9e9e9e';
  var label = state.backend === 's3' ? 'S3 (JuiceFS)' : 'Local disk';
  var note = '';
  if (state.state === 'switching') {
    note = '<div style="margin-top:0.5em;color:#d97706;">Switching: ' + escapeHtml(state.state_message || '') + '…</div>';
  } else if (state.state_message) {
    note = '<div style="margin-top:0.5em;color:#dc3545;">Last switch error: ' + escapeHtml(state.state_message) + '</div>';
  }
  var details = '';
  if (state.backend === 's3') {
    details = ' <span style="color:#666;font-size:0.9em;">'
      + 'bucket=' + escapeHtml(state.s3_bucket || '?')
      + (state.s3_prefix ? '/' + escapeHtml(state.s3_prefix) : '')
      + (state.s3_region ? ', region=' + escapeHtml(state.s3_region) : '')
      + (state.s3_access_key_id ? ', key=' + escapeHtml(state.s3_access_key_id.slice(0, 4)) + '…' : '')
      + '</span>';
  }
  var pathInfo = '<div style="margin-top:0.35em;color:#666;font-size:0.9em;">'
    + 'Host path: <code>' + escapeHtml(state.archive_dir || '') + '</code></div>';
  var disabled = state.state === 'switching' ? 'disabled' : '';
  var buttonLabel = state.backend === 's3' ? 'Switch to local disk…' : 'Switch to S3…';
  var btn = '<button class="btn" id="archive-backend-switch-btn" ' + disabled + '>' + buttonLabel + '</button>';
  el.innerHTML = '<div style="background:' + bgColor + ';border:1px solid ' + borderColor + ';padding:0.8em 1em;border-radius:4px;">'
    + '<strong>&#x1F5C4;&#xFE0F; Archive backend:</strong> ' + escapeHtml(label) + details
    + pathInfo + note
    + '<div style="margin-top:0.5em;">' + btn + '</div>'
    + '<div id="archive-backend-form" style="display:none;margin-top:0.8em;border-top:1px solid #ccc;padding-top:0.8em;"></div>'
    + '</div>';
  document.getElementById('archive-backend-switch-btn').onclick = function() { showSwitchForm(state); };
}

function showSwitchForm(state) {
  var formEl = document.getElementById('archive-backend-form');
  var goingToS3 = state.backend === 'local';
  var html;
  if (goingToS3) {
    html = '<p><strong>Switch to S3-backed archive.</strong> Affected apps (those using <code>app_archive</code> or <code>access_all_data</code>) will be stopped, archive data copied to the new backend, and apps restarted. In-flight uploads will be lost.</p>'
      + '<div style="display:grid;grid-template-columns:max-content 1fr;gap:0.4em 0.8em;align-items:center;max-width:600px;">'
      + '<label>S3 bucket</label><input id="ab-bucket" value="' + escapeHtml(state.s3_bucket || '') + '" placeholder="my-openhost-archive">'
      + '<label>Region</label><input id="ab-region" value="' + escapeHtml(state.s3_region || 'us-east-1') + '">'
      + '<label>Endpoint <span style="color:#888;font-size:0.85em;">(optional, non-AWS)</span></label><input id="ab-endpoint" value="' + escapeHtml(state.s3_endpoint || '') + '" placeholder="https://...">'
      + '<label>Prefix <span style="color:#888;font-size:0.85em;">(optional path under the bucket; lets multiple zones share one bucket)</span></label><input id="ab-prefix" value="' + escapeHtml(state.s3_prefix || '') + '" placeholder="s3-backing/zone-name">'
      + '<label>Access key ID</label><input id="ab-access-key" value="' + escapeHtml(state.s3_access_key_id || '') + '">'
      + '<label>Secret access key</label><input id="ab-secret-key" type="password">'
      + '<label>Volume name</label><input id="ab-volume" value="' + escapeHtml(state.juicefs_volume_name || 'openhost') + '">'
      + '</div>'
      + '<label style="display:block;margin-top:0.6em;"><input type="checkbox" id="ab-confirm"> I understand: opted-in apps will be stopped, restarted, and any in-flight uploads will be lost.</label>'
      + '<label style="display:block;margin-top:0.3em;"><input type="checkbox" id="ab-delete-source"> Also delete the local-disk archive after the copy succeeds.</label>'
      + '<div style="margin-top:0.6em;display:flex;gap:0.5em;align-items:center;">'
      + '<button class="btn" id="ab-test-btn">Test connection</button>'
      + '<button class="btn btn-primary" id="ab-submit-btn">Switch to S3</button>'
      + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
      + '<span id="ab-msg" style="font-size:0.9em;"></span>'
      + '</div>';
  } else {
    html = '<p><strong>Switch to local-disk archive.</strong> Affected apps will be stopped, archive data copied off S3 to local disk, and apps restarted.  The S3 bucket\'s contents stay; you can delete it manually after.</p>'
      + '<label style="display:block;"><input type="checkbox" id="ab-confirm"> I understand: opted-in apps will be stopped and restarted, and in-flight uploads will be lost.</label>'
      + '<div style="margin-top:0.6em;display:flex;gap:0.5em;align-items:center;">'
      + '<button class="btn btn-primary" id="ab-submit-btn">Switch to local</button>'
      + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
      + '<span id="ab-msg" style="font-size:0.9em;"></span>'
      + '</div>';
  }
  formEl.innerHTML = html;
  formEl.style.display = '';
  document.getElementById('ab-cancel-btn').onclick = function() { formEl.style.display = 'none'; formEl.innerHTML = ''; };
  if (goingToS3) {
    document.getElementById('ab-test-btn').onclick = function() { testArchiveConnection(); };
  }
  document.getElementById('ab-submit-btn').onclick = function() { submitSwitch(goingToS3); };
}

function testArchiveConnection() {
  var msg = document.getElementById('ab-msg');
  msg.textContent = 'Testing…';
  msg.style.color = '';
  var fd = new FormData();
  fd.append('s3_bucket', document.getElementById('ab-bucket').value);
  fd.append('s3_region', document.getElementById('ab-region').value);
  fd.append('s3_endpoint', document.getElementById('ab-endpoint').value);
  fd.append('s3_prefix', document.getElementById('ab-prefix').value);
  fd.append('s3_access_key_id', document.getElementById('ab-access-key').value);
  fd.append('s3_secret_access_key', document.getElementById('ab-secret-key').value);
  fetch(config.archiveBackendTestUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json().then(function(b) { return [r.status, b]; }); })
    .then(function(pair) {
      var ok = pair[0] === 200 && pair[1].ok;
      msg.style.color = ok ? '#16a34a' : '#dc3545';
      msg.textContent = ok ? 'Bucket reachable' : ('Failed: ' + (pair[1].error || ''));
    })
    .catch(function(err) {
      msg.style.color = '#dc3545';
      msg.textContent = 'Network error: ' + err;
    });
}

function submitSwitch(goingToS3) {
  var msg = document.getElementById('ab-msg');
  if (!document.getElementById('ab-confirm').checked) {
    msg.style.color = '#dc3545';
    msg.textContent = 'Tick the confirmation checkbox first.';
    return;
  }
  var fd = new FormData();
  fd.append('backend', goingToS3 ? 's3' : 'local');
  fd.append('confirm_data_loss', 'true');
  if (goingToS3) {
    fd.append('s3_bucket', document.getElementById('ab-bucket').value);
    fd.append('s3_region', document.getElementById('ab-region').value);
    fd.append('s3_endpoint', document.getElementById('ab-endpoint').value);
    fd.append('s3_prefix', document.getElementById('ab-prefix').value);
    fd.append('s3_access_key_id', document.getElementById('ab-access-key').value);
    fd.append('s3_secret_access_key', document.getElementById('ab-secret-key').value);
    fd.append('juicefs_volume_name', document.getElementById('ab-volume').value);
    if (document.getElementById('ab-delete-source').checked) {
      fd.append('delete_source_after_copy', 'true');
    }
  }
  msg.style.color = '';
  msg.textContent = 'Submitting…';
  fetch(config.archiveBackendUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json().then(function(b) { return [r.status, b]; }); })
    .then(function(pair) {
      if (pair[0] === 202) {
        msg.style.color = '#d97706';
        msg.textContent = 'Switch in progress…';
        document.getElementById('archive-backend-form').style.display = 'none';
        pollArchiveBackend();
      } else {
        msg.style.color = '#dc3545';
        msg.textContent = 'Failed: ' + (pair[1].error || pair[1]);
      }
    })
    .catch(function(err) {
      msg.style.color = '#dc3545';
      msg.textContent = 'Network error: ' + err;
    });
}

function pollArchiveBackend() {
  loadArchiveBackend().then(function(state) {
    if (state && state.state === 'switching') {
      setTimeout(pollArchiveBackend, 1500);
    }
  }, function(err) {
    // Surface the polling failure rather than silently freezing the
    // last rendered state.  We don't reschedule — the operator can
    // hit reload to retry.
    var el = document.getElementById('archive-backend-status');
    if (el) {
      el.innerHTML = '<div style="background:#f8d7da;border:1px solid #dc3545;'
        + 'padding:0.8em 1em;border-radius:4px;">'
        + '<strong>&#x26A0;&#xFE0F; Archive backend status unavailable</strong>'
        + '<div style="margin-top:0.35em;color:#666;font-size:0.9em;">'
        + escapeHtml(String(err)) + '</div></div>';
    }
  });
}

function loadArchiveBackend() {
  return fetch(config.archiveBackendUrl, {credentials: 'same-origin'})
    .then(function(r) {
      if (!r.ok) {
        throw new Error('HTTP ' + r.status);
      }
      return r.json();
    })
    .then(function(data) {
      renderArchiveBackend(data);
      return data;
    });
}

loadArchiveBackend();
