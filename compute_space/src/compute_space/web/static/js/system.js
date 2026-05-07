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

// ─── Security Audit ───

function updateSecurityAudit() {
  fetch(config.securityAuditUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var body = document.getElementById('security-body');
      var rows = '';
      Object.keys(data.checks).sort().forEach(function(name) {
        var c = data.checks[name];
        var statusCls = c.ok ? 'status-running' : 'status-error';
        var statusText = c.ok ? 'OK' : 'FAIL';
        rows += '<tr><td><code>' + escHtml(name) + '</code></td>'
          + '<td class="' + statusCls + '">' + statusText + '</td>'
          + '<td>' + escHtml(c.detail) + '</td></tr>';
      });
      body.innerHTML = rows || '<tr><td colspan="3" class="muted">No checks reported.</td></tr>';
    });
}

// ─── Listening Ports ───

function updateListeningPorts() {
  fetch(config.listeningPortsUrl, {credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var body = document.getElementById('ports-body');
      var ports = (data && data.ports) || [];
      if (!ports.length) {
        body.innerHTML = '<tr><td colspan="4" class="error">Could not enumerate listening ports.</td></tr>';
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
  var fd = new FormData();
  fd.append('paused', pause ? '1' : '0');
  fetch(config.toggleStorageGuardUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function() { updateStorageStatus(); });
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
      rows += '<tr><th class="label-col">Disk free</th><td' + freeCls + '>' + escHtml(freeText) + '</td></tr>';
      rows += '<tr><th class="label-col">OpenHost data</th><td>' + escHtml(formatBytes(data.openhost_data_used_bytes || 0)) + '</td></tr>';
      rows += '<tr><th class="label-col">App data</th><td>' + escHtml(formatBytes(data.app_data_used_bytes || 0)) + '</td></tr>';

      var perApp = data.per_app || {};
      var appNames = Object.keys(perApp).sort();
      if (appNames.length > 0) {
        var perAppHtml = appNames.map(function(name) {
          return escHtml(name) + ' ' + escHtml(formatBytes(perApp[name]));
        }).join(' &middot; ');
        rows += '<tr><th class="label-col">Per app</th><td>' + perAppHtml + '</td></tr>';
      }

      if (hasMinFree) {
        var guardText = guardPaused ? 'Paused' : (isLow ? 'Active (low storage)' : 'Active');
        var guardCls = (guardPaused || isLow) ? ' class="status-error"' : '';
        rows += '<tr><th class="label-col">Storage guard</th><td' + guardCls + '>' + escHtml(guardText) + '</td></tr>';
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

// ─── Router restart / build cache ───

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
  msg.className = 'muted';
  msg.textContent = 'Dropping cache...';

  fetch(config.dropBuildCacheUrl, {method: 'POST', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.error) {
        msg.className = 'error';
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
      msg.className = 'status-running';
      msg.textContent = 'Build cache dropped.' + reclaimed;
    })
    .catch(function() {
      msg.className = 'error';
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
        status.className = 'status-error';
      } else {
        btn.textContent = 'Enable SSH';
        btn.className = 'btn';
        status.textContent = 'SSH disabled';
        status.className = 'muted';
      }
    });
}

function toggleSsh() {
  var btn = document.getElementById('ssh-btn');
  btn.disabled = true;
  btn.textContent = '...';
  fetch(config.toggleSshUrl, {method: 'POST', credentials: 'same-origin'})
    .then(function(r) { return r.json(); })
    .then(function() {
      // Re-run the audit and ports too — SSH state affects both.
      updateSshStatus();
      updateSecurityAudit();
      updateListeningPorts();
    });
}

// ─── Archive Backend ───

function renderArchiveBackend(state) {
  var el = document.getElementById('archive-backend-status');
  var rows = '';
  if (state.backend === 's3') {
    rows += '<tr><th class="label-col">Backend</th>'
      + '<td><span class="status-running">S3 (JuiceFS)</span>'
      + (state.state_message ? ' <span class="error">' + escHtml(state.state_message) + '</span>' : '')
      + '</td></tr>';
    var bucketLine = escHtml(state.s3_bucket || '?')
      + (state.s3_prefix ? '/' + escHtml(state.s3_prefix) : '')
      + (state.s3_region ? ' <span class="hint">(' + escHtml(state.s3_region) + ')</span>' : '');
    rows += '<tr><th>S3 bucket</th><td><code>' + bucketLine + '</code></td></tr>';
    if (state.s3_access_key_id) {
      rows += '<tr><th>Access key</th><td><code>' + escHtml(state.s3_access_key_id.slice(0, 4)) + '…</code></td></tr>';
    }
    if (state.archive_dir) {
      rows += '<tr><th>Host path</th><td><code>' + escHtml(state.archive_dir) + '</code></td></tr>';
    }
    if (state.meta_db_path) {
      rows += '<tr><th>Metadata DB</th><td><code>' + escHtml(state.meta_db_path) + '</code>'
        + ' <span class="error">(must back up to survive disk loss)</span></td></tr>';
    }
    var dumps = state.meta_dumps;
    var dumpLine;
    if (dumps && dumps.count > 0) {
      dumpLine = '<code>' + escHtml(dumps.latest_at || '?') + '</code> <span class="hint">(' + dumps.count + ' in bucket, hourly cadence)</span>';
    } else if (dumps && dumps.count === 0) {
      dumpLine = '<span class="error">No metadata dumps in bucket yet.</span> <span class="hint">JuiceFS writes one within an hour of mount.</span>';
    } else {
      dumpLine = '<span class="hint">unavailable; could not list <code>'
        + escHtml((state.s3_prefix ? state.s3_prefix + '/' : '') + 'meta/')
        + '</code></span>';
    }
    rows += '<tr><th>Latest meta dump</th><td>' + dumpLine + '</td></tr>';
  } else {
    rows += '<tr><th class="label-col">Backend</th>'
      + '<td><span class="status-stopped">not configured</span></td></tr>';
  }

  var experimentalNote = '';
  if (state.backend === 's3') {
    experimentalNote = '<p class="hint"><strong class="error">Experimental:</strong> the S3 archive backend is best-effort durable. '
      + 'Filename-to-S3-chunk mappings live in a SQLite metadata DB on this zone\u2019s local disk; '
      + 'recovery after the local disk is wiped requires the latest meta dump in S3 plus a manual <code>juicefs load</code>.</p>';
  }
  var disabledNote = '';
  var configureBtn = '';
  if (state.backend === 'disabled') {
    disabledNote = '<p class="hint"><strong>No archive backend configured.</strong> '
      + 'Apps that opt into the <code>app_archive</code> data tier (such as Immich) will refuse to install until S3 storage is configured below. Apps that don\u2019t use the archive tier are unaffected. '
      + '<strong>This is a one-time setup; reconfiguration is not supported.</strong></p>';
    configureBtn = '<div class="control-row"><button class="btn" id="archive-backend-configure-btn">Configure S3 backend\u2026</button></div>';
  }

  el.innerHTML = '<table id="archive-backend-table"><tbody>' + rows + '</tbody></table>'
    + disabledNote
    + experimentalNote
    + configureBtn
    + '<div id="archive-backend-form" hidden></div>';
  if (state.backend === 'disabled') {
    document.getElementById('archive-backend-configure-btn').onclick = function() { showConfigureForm(); };
  }
}

function showConfigureForm() {
  var formEl = document.getElementById('archive-backend-form');
  formEl.innerHTML = '<p><strong>Configure S3 archive storage.</strong> JuiceFS will format the bucket and mount it locally; this is a one-time operation.</p>'
    + '<p class="error"><strong>Experimental.</strong> Filename-to-S3-chunk mappings live in a SQLite metadata DB on this zone\u2019s local disk, not in the bucket. If the local disk is wiped, the bucket bytes can be recovered only from JuiceFS\'s periodic meta dumps in S3 (replayed via <code>juicefs load</code>).</p>'
    + '<p class="hint">JuiceFS will automatically dump the metadata DB to <code>&lt;bucket&gt;/&lt;prefix&gt;/meta/dump-*.json.gz</code> once an hour. These dumps are the recovery anchor for reattaching a freshly-installed zone to an existing bucket.</p>'
    + '<table class="form-table"><tbody>'
    + '<tr><th><label for="ab-bucket">S3 bucket</label></th><td><input id="ab-bucket" type="text" placeholder="my-openhost-archive"></td></tr>'
    + '<tr><th><label for="ab-region">Region</label></th><td><input id="ab-region" type="text" value="us-east-1"></td></tr>'
    + '<tr><th><label for="ab-endpoint">Endpoint</label></th><td><input id="ab-endpoint" type="text" placeholder="https://..."> <span class="hint">optional, non-AWS</span></td></tr>'
    + '<tr><th><label for="ab-prefix">Prefix</label></th><td><input id="ab-prefix" type="text" placeholder="andrew-3"> <span class="hint">optional single-segment name; lets multiple zones share one bucket &mdash; also used as the JuiceFS volume name</span></td></tr>'
    + '<tr><th><label for="ab-access-key">Access key ID</label></th><td><input id="ab-access-key" type="text"></td></tr>'
    + '<tr><th><label for="ab-secret-key">Secret access key</label></th><td><input id="ab-secret-key" type="password"></td></tr>'
    + '</tbody></table>'
    + '<p><label><input type="checkbox" id="ab-confirm"> I understand the S3 archive backend is experimental and that reconfiguration is not supported.</label></p>'
    + '<div class="control-row">'
    + '<button class="btn" id="ab-test-btn">Test connection</button>'
    + '<button class="btn btn-primary" id="ab-submit-btn">Configure</button>'
    + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
    + '<span id="ab-msg" class="hint"></span>'
    + '</div>';
  formEl.hidden = false;
  document.getElementById('ab-cancel-btn').onclick = function() {
    formEl.hidden = true;
    formEl.innerHTML = '';
  };
  document.getElementById('ab-test-btn').onclick = testArchiveConnection;
  document.getElementById('ab-submit-btn').onclick = submitConfigure;
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

function submitConfigure() {
  var msg = document.getElementById('ab-msg');
  if (!document.getElementById('ab-confirm').checked) {
    msg.style.color = '#dc3545';
    msg.textContent = 'Tick the confirmation checkbox first.';
    return;
  }
  var fd = new FormData();
  fd.append('s3_bucket', document.getElementById('ab-bucket').value);
  fd.append('s3_region', document.getElementById('ab-region').value);
  fd.append('s3_endpoint', document.getElementById('ab-endpoint').value);
  fd.append('s3_prefix', document.getElementById('ab-prefix').value);
  fd.append('s3_access_key_id', document.getElementById('ab-access-key').value);
  fd.append('s3_secret_access_key', document.getElementById('ab-secret-key').value);
  msg.style.color = '';
  msg.textContent = 'Configuring (may take 10-30s)…';
  document.getElementById('ab-submit-btn').disabled = true;
  fetch(config.archiveBackendConfigureUrl, {method: 'POST', credentials: 'same-origin', body: fd})
    .then(function(r) { return r.json().then(function(b) { return [r.status, b]; }); })
    .then(function(pair) {
      if (pair[0] === 200) {
        loadArchiveBackend();
      } else {
        msg.style.color = '#dc3545';
        msg.textContent = 'Failed: ' + (pair[1].error || pair[1]);
        document.getElementById('ab-submit-btn').disabled = false;
      }
    })
    .catch(function(err) {
      msg.style.color = '#dc3545';
      msg.textContent = 'Network error: ' + err;
      document.getElementById('ab-submit-btn').disabled = false;
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
    })
    .catch(function(err) {
      var el = document.getElementById('archive-backend-status');
      if (el) {
        el.innerHTML = '<p class="error"><strong>Archive backend status unavailable.</strong> '
          + escHtml(String(err)) + '</p>';
      }
    });
}

// ─── Init ───

updateSecurityAudit();
setInterval(updateSecurityAudit, 10000);

updateListeningPorts();
setInterval(updateListeningPorts, 10000);

updateStorageStatus();
setInterval(updateStorageStatus, 5000);

updateSshStatus();
setInterval(updateSshStatus, 5000);

loadArchiveBackend();
