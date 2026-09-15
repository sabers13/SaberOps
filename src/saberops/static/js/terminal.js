"use strict";

/* PTY web terminal: read-only, reconnectable, transcript-backed.
   Connects to WS endpoint /runs/<id>/terminal/ws.
   Vendor xterm.js handles ANSI rendering. No input channel.
*/
(function () {
  function initTerminal() {
    var terminalEl = document.getElementById("terminal");
    var runHeader = document.getElementById("run-header");
    if (!terminalEl || !runHeader) return;
    var runId = runHeader.getAttribute("data-run-id");
    if (!runId) return;
    if (typeof window.Terminal === "undefined") return;

    var term = new window.Terminal({
      convertEol: true,
      disableStdin: true,
      cursorBlink: false,
      fontFamily: "SFMono-Regular, Menlo, monospace",
      fontSize: 13,
      theme: { background: "#0f1115" },
    });
    term.open(terminalEl);
    // Focus handling: keep read-only
    term.attachCustomKeyEventHandler(function () {
      return false;
    });

    var protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    var wsUrl = protocol + window.location.host + "/runs/" + encodeURIComponent(runId) + "/terminal/ws";
    var ws;
    function connect() {
      ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      ws.onmessage = function (ev) {
        if (ev.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(ev.data));
        } else if (ev.data instanceof Blob) {
          ev.data.arrayBuffer().then(function (buf) {
            term.write(new Uint8Array(buf));
          });
        } else {
          term.write(String(ev.data));
        }
      };
      ws.onclose = function (ev) {
        if (ev.code === 4404) {
          term.write("\r\n[Terminal: run not found]\r\n");
        }
        // Auto-reconnect after 2s unless explicitly closed for missing run
        if (ev.code !== 4404 && ev.code !== 1000) {
          window.setTimeout(connect, 2000);
        }
      };
      ws.onerror = function () {
        // Error will trigger onclose
      };
    }
    connect();

    // Prevent any keyboard input from reaching PTY (controlled PTY only)
    terminalEl.addEventListener("keydown", function (e) {
      e.preventDefault();
    });
  }

  function initTabs() {
    var tabs = document.querySelectorAll(".run-tab");
    var panels = document.querySelectorAll(".run-tab-panel");
    if (!tabs.length) return;
    function activate(name) {
      tabs.forEach(function (t) {
        var isActive = t.getAttribute("data-tab") === name;
        t.classList.toggle("is-active", isActive);
        t.setAttribute("aria-selected", isActive ? "true" : "false");
      });
      panels.forEach(function (p) {
        var show = p.getAttribute("data-panel") === name;
        p.style.display = show ? "" : "none";
      });
      try {
        window.localStorage.setItem("orch-run-tab", name);
      } catch (_e) {
        /* ignore */
      }
    }
    tabs.forEach(function (t) {
      t.addEventListener("click", function () {
        var name = t.getAttribute("data-tab") || "overview";
        activate(name);
      });
    });
    // Restore or default to terminal if run is still active? Requirement says terminal focus
    var stored = null;
    try {
      stored = window.localStorage.getItem("orch-run-tab");
    } catch (_e) {
      stored = null;
    }
    // If URL hash indicates tab
    var hashTab = window.location.hash ? window.location.hash.replace("#", "") : null;
    if (hashTab && document.querySelector('.run-tab[data-tab="' + hashTab + '"]')) {
      activate(hashTab);
    } else if (stored && document.querySelector('.run-tab[data-tab="' + stored + '"]')) {
      activate(stored);
    } else {
      activate("overview");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initTabs();
      initTerminal();
    });
  } else {
    initTabs();
    initTerminal();
  }
})();
