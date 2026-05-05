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
  var label, statusCls;
  if (state.backend === 's3') {
    label = 'S3 (JuiceFS)';
    statusCls = 'status-running';
  } else if (state.backend === 'local') {
    label = 'Local disk';
    statusCls = 'status-running';
  } else {
    label = 'not configured';
    statusCls = 'status-stopped';
  }

  var rows = '';
  rows += '<tr><th class="label-col">Backend</th>'
    + '<td><span class="' + statusCls + '">' + escHtml(label) + '</span>'
    + (state.state === 'switching' ? ' <span class="status-building">switching: ' + escHtml(state.state_message || '') + '…</span>' : '')
    + (state.state !== 'switching' && state.state_message ? ' <span class="error">last switch error: ' + escHtml(state.state_message) + '</span>' : '')
    + '</td></tr>';
  if (state.backend === 's3') {
    var bucketLine = escHtml(state.s3_bucket || '?')
      + (state.s3_prefix ? '/' + escHtml(state.s3_prefix) : '')
      + (state.s3_region ? ' <span class="hint">(' + escHtml(state.s3_region) + ')</span>' : '');
    rows += '<tr><th>S3 bucket</th><td><code>' + bucketLine + '</code></td></tr>';
    if (state.s3_access_key_id) {
      rows += '<tr><th>Access key</th><td><code>' + escHtml(state.s3_access_key_id.slice(0, 4)) + '…</code></td></tr>';
    }
  }
  var archiveDirText = state.archive_dir
    ? '<code>' + escHtml(state.archive_dir) + '</code>'
    : '<span class="hint">not yet provisioned</span>';
  rows += '<tr><th>Host path</th><td>' + archiveDirText + '</td></tr>';
  if (state.meta_db_path) {
    rows += '<tr><th>Metadata DB</th><td><code>' + escHtml(state.meta_db_path) + '</code>'
      + (state.backend === 's3' ? ' <span class="error">(must back up to survive disk loss)</span>' : '')
      + '</td></tr>';
  }
  if (state.backend === 's3') {
    var dumps = state.meta_dumps;
    var dumpLine;
    if (dumps && dumps.count > 0) {
      dumpLine = '<code>' + escHtml(dumps.latest_at || '?') + '</code> <span class="hint">(' + dumps.count + ' in bucket, hourly cadence)</span>';
    } else if (dumps && dumps.count === 0) {
      dumpLine = '<span class="error">No metadata dumps in bucket yet.</span> <span class="hint">JuiceFS writes one within an hour of mount; if this persists past the first hour something is wrong with the mount.</span>';
    } else {
      dumpLine = '<span class="hint">unavailable; could not list <code>'
        + escHtml((state.s3_prefix ? state.s3_prefix + '/' : '') + 'meta/')
        + '</code></span>';
    }
    rows += '<tr><th>Latest meta dump</th><td>' + dumpLine + '</td></tr>';
  }

  var disabled = state.state === 'switching' ? 'disabled' : '';
  var buttonLabel;
  if (state.backend === 'disabled') {
    buttonLabel = 'Configure archive backend…';
  } else if (state.backend === 's3') {
    buttonLabel = 'Switch to local disk…';
  } else {
    buttonLabel = 'Switch to S3…';
  }
  var btn = '<button class="btn" id="archive-backend-switch-btn" ' + disabled + '>' + escHtml(buttonLabel) + '</button>';

  var disabledNote = '';
  if (state.backend === 'disabled') {
    disabledNote = '<p class="hint"><strong>No archive backend configured yet.</strong> '
      + 'Apps that opt into the <code>app_archive</code> data tier (such as Immich) will refuse to install until you pick a backend below. Apps that don\u2019t use the archive tier are unaffected.</p>';
  }
  var experimentalNote = '';
  if (state.backend === 's3') {
    experimentalNote = '<p class="hint"><strong class="error">Experimental:</strong> the S3 archive backend is best-effort durable. '
      + 'Filename-to-S3-chunk mappings live in a SQLite metadata DB on this VM; '
      + 'recovery from a lost VM requires the latest meta dump in S3 plus a manual <code>juicefs load</code>. '
      + 'Do not use for anything you cannot afford to lose without an out-of-band backup.</p>';
  }

  el.innerHTML = '<table id="archive-backend-table"><tbody>' + rows + '</tbody></table>'
    + disabledNote
    + experimentalNote
    + '<div class="control-row">' + btn + '</div>'
    + '<div id="archive-backend-form" hidden></div>';
  document.getElementById('archive-backend-switch-btn').onclick = function() { showArchiveSwitchForm(state); };
}

function _s3SwitchFormHtml(state, includeDeleteSource) {
  return '<p><strong>Switch to S3-backed archive.</strong> Affected apps (those using <code>app_archive</code> or <code>access_all_data</code>) will be stopped, archive data copied to the new backend, and apps restarted. In-flight uploads will be lost.</p>'
    + '<p class="error"><strong>Experimental.</strong> Filename-to-S3-chunk mappings live in a SQLite metadata DB on this VM, not in the bucket. A lost VM means the bucket bytes can be recovered only from JuiceFS\'s periodic meta dumps in S3 (replayed via <code>juicefs load</code>); anything written between the last dump and the loss is orphan chunks with no inode.</p>'
    + '<p class="hint">JuiceFS will automatically dump the metadata DB to <code>&lt;bucket&gt;/&lt;prefix&gt;/meta/dump-*.json.gz</code> once an hour after the mount comes up. These dumps are the recovery anchor for the "fresh VM, same bucket" case &mdash; a zone whose VM dies retains everything written before the last dump.</p>'
    + '<table class="form-table"><tbody>'
    + '<tr><th><label for="ab-bucket">S3 bucket</label></th><td><input id="ab-bucket" type="text" value="' + escHtml(state.s3_bucket || '') + '" placeholder="my-openhost-archive"></td></tr>'
    + '<tr><th><label for="ab-region">Region</label></th><td><input id="ab-region" type="text" value="' + escHtml(state.s3_region || 'us-east-1') + '"></td></tr>'
    + '<tr><th><label for="ab-endpoint">Endpoint</label></th><td><input id="ab-endpoint" type="text" value="' + escHtml(state.s3_endpoint || '') + '" placeholder="https://..."> <span class="hint">optional, non-AWS</span></td></tr>'
    + '<tr><th><label for="ab-prefix">Prefix</label></th><td><input id="ab-prefix" type="text" value="' + escHtml(state.s3_prefix || '') + '" placeholder="andrew-3"> <span class="hint">optional single-segment name; lets multiple zones share one bucket — also used as the JuiceFS volume name</span></td></tr>'
    + '<tr><th><label for="ab-access-key">Access key ID</label></th><td><input id="ab-access-key" type="text" value="' + escHtml(state.s3_access_key_id || '') + '"></td></tr>'
    + '<tr><th><label for="ab-secret-key">Secret access key</label></th><td><input id="ab-secret-key" type="password"></td></tr>'
    + '</tbody></table>'
    + '<p><label><input type="checkbox" id="ab-confirm"> I understand: opted-in apps will be stopped, restarted, and any in-flight uploads will be lost. I also understand the S3 archive backend is experimental and may lose data.</label></p>'
    + (includeDeleteSource
      ? '<p><label><input type="checkbox" id="ab-delete-source"> Also delete the local-disk archive after the copy succeeds.</label></p>'
      : '')
    + '<div class="control-row">'
    + '<button class="btn" id="ab-test-btn">Test connection</button>'
    + '<button class="btn btn-primary" id="ab-submit-btn">Switch to S3</button>'
    + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
    + '<span id="ab-msg" class="hint"></span>'
    + '</div>';
}

function _localSwitchFormHtml(fromBackend) {
  if (fromBackend === 'disabled') {
    return '<p><strong>Configure local-disk archive.</strong> Archive data will live on the persistent host volume under <code>persistent_data/app_archive/</code>. Same backup story as <code>app_data</code>.</p>'
      + '<p><label><input type="checkbox" id="ab-confirm"> I understand: archive-using apps deployed after this point will store their bulk data on the host\u2019s local disk.</label></p>'
      + '<div class="control-row">'
      + '<button class="btn btn-primary" id="ab-submit-btn">Configure local</button>'
      + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
      + '<span id="ab-msg" class="hint"></span>'
      + '</div>';
  }
  return '<p><strong>Switch to local-disk archive.</strong> Affected apps will be stopped, archive data copied off S3 to local disk, and apps restarted. The S3 bucket\'s contents stay; you can delete it manually after.</p>'
    + '<p><label><input type="checkbox" id="ab-confirm"> I understand: opted-in apps will be stopped and restarted, and in-flight uploads will be lost.</label></p>'
    + '<div class="control-row">'
    + '<button class="btn btn-primary" id="ab-submit-btn">Switch to local</button>'
    + '<button class="btn" id="ab-cancel-btn">Cancel</button>'
    + '<span id="ab-msg" class="hint"></span>'
    + '</div>';
}

function showArchiveSwitchForm(state) {
  var formEl = document.getElementById('archive-backend-form');
  if (state.backend === 'disabled') {
    var pickerHtml = '<p><strong>Pick an archive backend.</strong> This is a one-time configure step for fresh zones; you can still switch between local and s3 afterwards.</p>'
      + '<div class="control-row">'
      + '<label><input type="radio" name="ab-target" value="local" checked> Local disk</label>'
      + '<label><input type="radio" name="ab-target" value="s3"> S3 (JuiceFS) &mdash; <span class="error">experimental</span></label>'
      + '</div>'
      + '<div id="ab-target-body"></div>';
    formEl.innerHTML = pickerHtml;
    formEl.hidden = false;
    var renderBody = function() {
      var picked = document.querySelector('input[name="ab-target"]:checked').value;
      var body = document.getElementById('ab-target-body');
      body.innerHTML = picked === 's3'
        ? _s3SwitchFormHtml(state, false)
        : _localSwitchFormHtml('disabled');
      _wireArchiveSwitchFormButtons(picked === 's3', formEl);
    };
    document.querySelectorAll('input[name="ab-target"]').forEach(function(el) {
      el.onchange = renderBody;
    });
    renderBody();
    return;
  }

  var goingToS3 = state.backend === 'local';
  formEl.innerHTML = goingToS3
    ? _s3SwitchFormHtml(state, true)
    : _localSwitchFormHtml('s3');
  formEl.hidden = false;
  _wireArchiveSwitchFormButtons(goingToS3, formEl);
}

function _wireArchiveSwitchFormButtons(goingToS3, formEl) {
  document.getElementById('ab-cancel-btn').onclick = function() {
    formEl.hidden = true;
    formEl.innerHTML = '';
  };
  if (goingToS3) {
    document.getElementById('ab-test-btn').onclick = function() { testArchiveConnection(); };
  }
  document.getElementById('ab-submit-btn').onclick = function() { submitArchiveSwitch(goingToS3); };
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

function submitArchiveSwitch(goingToS3) {
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
    // Checkbox only exists on the local→s3 transition.
    var deleteSourceEl = document.getElementById('ab-delete-source');
    if (deleteSourceEl && deleteSourceEl.checked) {
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
    var el = document.getElementById('archive-backend-status');
    if (el) {
      el.innerHTML = '<p class="error"><strong>Archive backend status unavailable.</strong> '
        + escHtml(String(err)) + '</p>';
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
