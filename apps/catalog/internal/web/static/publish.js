(function () {
  var root = document.getElementById("publish-page");
  if (!root) {
    return;
  }

  var statusUrl = root.dataset.statusUrl;
  var logsUrl = root.dataset.logsUrl;
  var terminal = root.dataset.terminal === "1";

  var statusEl = document.getElementById("publish-status");
  var errorEl = document.getElementById("publish-error");
  var logsEl = document.getElementById("publish-logs");
  var appNameEl = document.getElementById("router-app-name");
  var appLinkEl = document.getElementById("router-app-url");
  var pageLinkEl = document.getElementById("router-page-url");
  var manualBoxEl = document.getElementById("manual-install");
  var manualLinkEl = document.getElementById("manual-install-link");

  var statusTimer = null;
  var logsTimer = null;

  function statusClass(status) {
    switch ((status || "").toLowerCase()) {
      case "running":
        return "status-running";
      case "error":
        return "status-error";
      case "building":
      case "starting":
        return "status-active";
      case "redirect_required":
        return "status-warn";
      default:
        return "status-neutral";
    }
  }

  function setVisible(el, visible) {
    if (!el) {
      return;
    }
    el.style.display = visible ? "" : "none";
    el.classList.remove("hidden");
  }

  function updateStatus(data) {
    if (!statusEl) {
      return;
    }

    var status = data.status || "unknown";
    statusEl.textContent = status;
    statusEl.className = statusClass(status);

    if (errorEl) {
      if (data.error_message) {
        errorEl.textContent = data.error_message;
        setVisible(errorEl, true);
      } else {
        errorEl.textContent = "";
        setVisible(errorEl, false);
      }
    }

    if (appNameEl && data.router_app_name) {
      appNameEl.textContent = data.router_app_name;
    }

    if (appLinkEl && data.router_app_url) {
      appLinkEl.href = data.router_app_url;
      setVisible(appLinkEl, true);
    }
    if (pageLinkEl && data.router_page_url) {
      pageLinkEl.href = data.router_page_url;
      setVisible(pageLinkEl, true);
    }

    if (manualBoxEl) {
      var needsManual = status === "redirect_required";
      setVisible(manualBoxEl, needsManual);
      if (needsManual && manualLinkEl && data.manual_install_url) {
        manualLinkEl.href = data.manual_install_url;
      }
    }

    terminal = Boolean(data.terminal);
    if (terminal) {
      clearInterval(statusTimer);
      clearInterval(logsTimer);
    }
  }

  function refreshStatus() {
    fetch(statusUrl, { credentials: "same-origin" })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("status request failed");
        }
        return res.json();
      })
      .then(updateStatus)
      .catch(function () {
        // keep UI stable on transient failures
      });
  }

  function refreshLogs() {
    if (terminal && statusEl && statusEl.textContent === "redirect_required") {
      return;
    }
    fetch(logsUrl, { credentials: "same-origin" })
      .then(function (res) {
        if (!res.ok) {
          throw new Error("log request failed");
        }
        return res.text();
      })
      .then(function (text) {
        if (!logsEl) {
          return;
        }
        var nearBottom = logsEl.scrollHeight - logsEl.scrollTop - logsEl.clientHeight < 40;
        logsEl.textContent = text || "No logs yet.";
        if (nearBottom) {
          logsEl.scrollTop = logsEl.scrollHeight;
        }
      })
      .catch(function () {
        // ignore
      });
  }

  refreshStatus();
  refreshLogs();

  if (!terminal) {
    statusTimer = setInterval(refreshStatus, 1500);
    logsTimer = setInterval(refreshLogs, 1500);
  }
})();
