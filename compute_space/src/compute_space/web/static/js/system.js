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
      body.innerHTML = rows || '<tr><td colspan="3" style="color:#888;">No checks reported.</td></tr>';
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
      rows += '<tr><th style="width:14em;">Disk free</th><td' + freeCls + '>' + escHtml(freeText) + '</td></tr>';
      rows += '<tr><th>OpenHost data</th><td>' + escHtml(formatBytes(data.openhost_data_used_bytes || 0)) + '</td></tr>';
      rows += '<tr><th>App data</th><td>' + escHtml(formatBytes(data.app_data_used_bytes || 0)) + '</td></tr>';

      var perApp = data.per_app || {};
      var appNames = Object.keys(perApp).sort();
      if (appNames.length > 0) {
        var perAppHtml = appNames.map(function(name) {
          return escHtml(name) + ' ' + escHtml(formatBytes(perApp[name]));
        }).join(' &middot; ');
        rows += '<tr><th>Per app</th><td>' + perAppHtml + '</td></tr>';
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
        guardRow.innerHTML = '<button class="btn" onclick="toggleStorageGuard(false)">Resume Guard</button>'
          + ' <span style="color:#6b7280;font-size:0.9em;">Apps will not be stopped while paused.</span>';
      } else if (hasMinFree && isLow) {
        guardRow.innerHTML = '<button class="btn" onclick="toggleStorageGuard(true)">Pause Guard</button>'
          + ' <span style="color:#6b7280;font-size:0.9em;">Pause to start an app for cleanup.</span>';
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
        status.className = 'status-error';
      } else {
        btn.textContent = 'Enable SSH';
        btn.className = 'btn';
        status.textContent = 'SSH disabled';
        status.className = '';
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
  var bgColor = state.backend === 's3' ? '#e3f2fd' : '#f5f5f5';
  var borderColor = state.backend === 's3' ? '#2196f3' : '#9e9e9e';
  var label = state.backend === 's3' ? 'S3 (JuiceFS)' : 'Local disk';
  var note = '';
  if (state.state === 'switching') {
    note = '<div style="margin-top:0.5em;color:#d97706;">Switching: ' + escHtml(state.state_message || '') + '…</div>';
  } else if (state.state_message) {
    note = '<div style="margin-top:0.5em;color:#dc3545;">Last switch error: ' + escHtml(state.state_message) + '</div>';
  }
  var details = '';
  if (state.backend === 's3') {
    details = ' <span style="color:#666;font-size:0.9em;">'
      + 'bucket=' + escHtml(state.s3_bucket || '?')
      + (state.s3_prefix ? '/' + escHtml(state.s3_prefix) : '')
      + (state.s3_region ? ', region=' + escHtml(state.s3_region) : '')
      + (state.s3_access_key_id ? ', key=' + escHtml(state.s3_access_key_id.slice(0, 4)) + '…' : '')
      + '</span>';
  }
  // Three host-path lines clarifying where the operator-relevant
  // pieces of state actually live on disk.  Order is intentional:
  // operators want "where do my bytes live" first (Host path), then
  // "what file do I have to back up if the disk dies" (Metadata DB).
  // The metadata-DB line renders for both backends — local-mode shows
  // the path it WILL use after a switch, so the operator knows in
  // advance and can pre-plan their backup story.
  var hostInfo = '<div style="margin-top:0.35em;color:#666;font-size:0.9em;">'
    + 'Host path: <code>' + escHtml(state.archive_dir || '') + '</code></div>';
  if (state.meta_db_path) {
    hostInfo += '<div style="color:#666;font-size:0.9em;">'
      + 'Metadata DB: <code>' + escHtml(state.meta_db_path) + '</code>'
      + (state.backend === 's3'
        ? ' <span style="color:#600;">(must back up to survive disk loss)</span>'
        : '')
      + '</div>';
  }

  // Meta-dump status (s3 only).  Three render paths: list succeeded
  // and dumps exist; list succeeded and zero dumps; list failed (we
  // got null from the server because S3 was unreachable or boto3
  // wasn't installed).
  var dumpInfo = '';
  if (state.backend === 's3') {
    var dumps = state.meta_dumps;
    if (dumps && dumps.count > 0) {
      dumpInfo = '<div style="margin-top:0.35em;color:#666;font-size:0.9em;">'
        + 'Last metadata dump: <code>' + escHtml(dumps.latest_at || '?') + '</code>'
        + ' (' + dumps.count + ' in bucket, hourly cadence)</div>';
    } else if (dumps && dumps.count === 0) {
      dumpInfo = '<div style="margin-top:0.35em;color:#dc3545;font-size:0.9em;">'
        + 'No metadata dumps in bucket yet.  JuiceFS writes one within an hour of mount; if this persists past the first hour something is wrong with the mount.</div>';
    } else {
      // ``meta_dumps === null`` -> server couldn't list.  Could be
      // a transient S3 hiccup or a missing list permission on the
      // creds.  Surface the uncertainty rather than silently
      // omitting the line, so an operator who wonders "is JuiceFS
      // backing up my metadata" gets a visible "we don't know."
      dumpInfo = '<div style="margin-top:0.35em;color:#d97706;font-size:0.9em;">'
        + 'Metadata-dump status unavailable: could not list <code>'
        + escHtml((state.s3_prefix ? state.s3_prefix + '/' : '') + 'meta/')
        + '</code> in the bucket.  Check creds + bucket reachability.</div>';
    }
  }

  var disabled = state.state === 'switching' ? 'disabled' : '';
  var buttonLabel = state.backend === 's3' ? 'Switch to local disk…' : 'Switch to S3…';
  var btn = '<button class="btn" id="archive-backend-switch-btn" ' + disabled + '>' + buttonLabel + '</button>';
  // Persistent reminder when an experimental tier is in use.  Anyone
  // who walks back to this page weeks later should see immediately
  // that the archive lives on a backend they shouldn't trust as the
  // sole copy of irreplaceable data.  Local-disk doesn't get the
  // banner because openhost backs that up the same as every other
  // local-tier file.
  var experimentalNote = '';
  if (state.backend === 's3') {
    experimentalNote = '<div style="margin-top:0.5em;background:#f8d7da;border:1px solid #dc3545;color:#c00;padding:0.4em 0.6em;border-radius:4px;font-size:0.9em;">'
      + '<strong>Experimental:</strong> the S3 archive backend is best-effort durable.  '
      + 'Filename-to-S3-chunk mappings live in a SQLite metadata DB on this VM; '
      + 'recovery from a lost VM requires the latest meta dump in S3 plus a manual <code>juicefs load</code>.  '
      + 'Do not use for anything you cannot afford to lose without an out-of-band backup.'
      + '</div>';
  }
  el.innerHTML = '<div style="background:' + bgColor + ';border:1px solid ' + borderColor + ';padding:0.8em 1em;border-radius:4px;">'
    + '<strong>Archive backend:</strong> ' + escHtml(label) + details
    + hostInfo + dumpInfo + note + experimentalNote
    + '<div style="margin-top:0.5em;">' + btn + '</div>'
    + '<div id="archive-backend-form" style="display:none;margin-top:0.8em;border-top:1px solid #ccc;padding-top:0.8em;"></div>'
    + '</div>';
  document.getElementById('archive-backend-switch-btn').onclick = function() { showArchiveSwitchForm(state); };
}

function showArchiveSwitchForm(state) {
  var formEl = document.getElementById('archive-backend-form');
  var goingToS3 = state.backend === 'local';
  var html;
  if (goingToS3) {
    // Loud experimental warning at the top of the form, before the
    // operator types a single character.  Same red-danger styling
    // as the persistent banner in renderArchiveBackend so the two
    // pieces are visually linked.
    html = '<div style="background:#f8d7da;border:1px solid #dc3545;color:#c00;padding:0.6em 0.8em;border-radius:4px;margin-bottom:0.8em;">'
      + '<strong>Experimental backend.  You may lose data.  Do not use this for anything you cannot afford to lose without a separate backup.</strong>'
      + '<div style="margin-top:0.35em;font-size:0.9em;color:#600;">'
      + 'Filename-to-S3-chunk mappings live in a SQLite metadata DB on this VM, not in the bucket.  '
      + 'A lost VM means the bucket bytes can be recovered only from JuiceFS\'s periodic meta dumps in S3 (replayed via <code>juicefs load</code>); anything written between the last dump and the loss is orphan chunks with no inode.'
      + '</div></div>'
      + '<p><strong>Switch to S3-backed archive.</strong> Affected apps (those using <code>app_archive</code> or <code>access_all_data</code>) will be stopped, archive data copied to the new backend, and apps restarted. In-flight uploads will be lost.</p>'
      + '<p style="color:#666;font-size:0.9em;">JuiceFS will automatically dump the metadata DB to <code>&lt;bucket&gt;/&lt;prefix&gt;/meta/dump-*.json.gz</code> once an hour after the mount comes up.  These dumps are the recovery anchor for the "fresh VM, same bucket" case &mdash; a zone whose VM dies retains everything written before the last dump.</p>'
      + '<div style="display:grid;grid-template-columns:max-content 1fr;gap:0.4em 0.8em;align-items:center;max-width:600px;">'
      + '<label>S3 bucket</label><input id="ab-bucket" value="' + escHtml(state.s3_bucket || '') + '" placeholder="my-openhost-archive">'
      + '<label>Region</label><input id="ab-region" value="' + escHtml(state.s3_region || 'us-east-1') + '">'
      + '<label>Endpoint <span style="color:#888;font-size:0.85em;">(optional, non-AWS)</span></label><input id="ab-endpoint" value="' + escHtml(state.s3_endpoint || '') + '" placeholder="https://...">'
      + '<label>Prefix <span style="color:#888;font-size:0.85em;">(optional single-segment name; lets multiple zones share one bucket — also used as the JuiceFS volume name)</span></label><input id="ab-prefix" value="' + escHtml(state.s3_prefix || '') + '" placeholder="andrew-3">'
      + '<label>Access key ID</label><input id="ab-access-key" value="' + escHtml(state.s3_access_key_id || '') + '">'
      + '<label>Secret access key</label><input id="ab-secret-key" type="password">'
      + '</div>'
      + '<label style="display:block;margin-top:0.6em;"><input type="checkbox" id="ab-confirm"> I understand: opted-in apps will be stopped, restarted, and any in-flight uploads will be lost.  I also understand the S3 archive backend is experimental and may lose data.</label>'
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
        + '<strong>Archive backend status unavailable</strong>'
        + '<div style="margin-top:0.35em;color:#666;font-size:0.9em;">'
        + escHtml(String(err)) + '</div></div>';
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
