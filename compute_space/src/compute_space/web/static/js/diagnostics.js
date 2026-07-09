var config = JSON.parse(document.getElementById('page-config').textContent);

var latest = null;

function escHtml(s) {
  var d = document.createElement('div');
  d.textContent = (s == null) ? '' : String(s);
  return d.innerHTML;
}

function formatBytes(bytes) {
  if (bytes == null) return '';
  if (bytes >= 1099511627776) return (bytes / 1099511627776).toFixed(1) + ' TiB';
  if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + ' GiB';
  if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MiB';
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
  return bytes + ' B';
}

function gitText(git) {
  if (!git || !git.sha) return '(not a git checkout)';
  var branch = git.branch || '(detached HEAD)';
  var dirty = git.dirty ? ' (dirty)' : '';
  return branch + ' @ ' + (git.short_sha || git.sha) + dirty;
}

function renderSummary(data) {
  var sys = data.system || {};
  var rt = data.container_runtime || {};
  var disk = (data.storage && data.storage.disk) || {};
  var rows = '';
  function row(label, value) {
    rows += '<tr><th class="label-col">' + escHtml(label) + '</th><td>' + value + '</td></tr>';
  }
  row('Generated at', escHtml(data.generated_at));
  row('Zone domain', escHtml(data.zone_domain));
  row('OpenHost version', escHtml(gitText(data.openhost)));
  if (data.openhost && data.openhost.remote_url) {
    row('OpenHost remote', '<code>' + escHtml(data.openhost.remote_url) + '</code>');
  }
  row('Host', escHtml(sys.hostname) + ' — ' + escHtml(sys.platform));
  row('Kernel', escHtml(sys.system) + ' ' + escHtml(sys.release) + ' (' + escHtml(sys.machine) + ')');
  row('CPU count', escHtml(sys.cpu_count));
  row('Boot time', escHtml(sys.boot_time));
  row('Python', escHtml(sys.python_implementation) + ' ' + escHtml(sys.python_version));
  var rtText = rt.available
    ? ('podman ' + escHtml(rt.version || '?') + (rt.rootless === true ? ', rootless' : (rt.rootless === false ? ', ROOTFUL' : '')))
    : ('unavailable' + (rt.error ? ' (' + escHtml(rt.error) + ')' : ''));
  var rtCls = (rt.available && rt.rootless !== false) ? '' : ' class="status-error"';
  rows += '<tr><th class="label-col">Container runtime</th><td' + rtCls + '>' + rtText + '</td></tr>';
  if (disk.total_bytes != null) {
    row('Disk', formatBytes(disk.free_bytes) + ' free / ' + formatBytes(disk.total_bytes));
  }
  var deps = data.dependencies || {};
  var depNames = Object.keys(deps).sort();
  if (depNames.length) {
    var depHtml = depNames.map(function(n) {
      return '<code>' + escHtml(n) + '</code> ' + escHtml(deps[n]);
    }).join(' &middot; ');
    row('Key dependencies', depHtml);
  }
  document.getElementById('summary-body').innerHTML = rows;
}

function renderApps(data) {
  var apps = data.apps || [];
  var body = document.getElementById('apps-body');
  if (!apps.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted">No apps installed.</td></tr>';
    return;
  }
  body.innerHTML = apps.map(function(a) {
    var statusCls = a.status === 'running' ? 'status-running' : (a.status === 'error' ? 'status-error' : 'status-stopped');
    return '<tr><td>' + escHtml(a.name) + '</td>'
      + '<td>' + escHtml(a.version || '') + '</td>'
      + '<td class="' + statusCls + '">' + escHtml(a.status) + '</td>'
      + '<td>' + escHtml(gitText(a.git)) + '</td></tr>';
  }).join('');
}

function loadDiagnostics() {
  document.getElementById('copy-status').textContent = '';
  fetch(config.diagnosticsUrl, {credentials: 'same-origin'})
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      latest = data;
      renderSummary(data);
      renderApps(data);
      document.getElementById('diag-json').textContent = JSON.stringify(data, null, 2);
    })
    .catch(function(e) {
      document.getElementById('diag-json').textContent = 'Failed to load diagnostics: ' + e.message;
    });
}

document.getElementById('copy-btn').addEventListener('click', function() {
  if (!latest) return;
  var text = JSON.stringify(latest, null, 2);
  var status = document.getElementById('copy-status');
  var done = function() { status.textContent = 'Copied.'; };
  var fail = function() { status.textContent = 'Copy failed — select the JSON below manually.'; };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, fail);
  } else {
    // Fallback for non-secure contexts (plain-HTTP dev): copy via a temp textarea.
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy') ? done() : fail(); } catch (e) { fail(); }
    document.body.removeChild(ta);
  }
});

document.getElementById('refresh-btn').addEventListener('click', loadDiagnostics);

loadDiagnostics();
