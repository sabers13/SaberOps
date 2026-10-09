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
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim(),
      fontSize: 13,
      theme: { background: "#0f1115" },
    });
    term.open(terminalEl);
    document.fonts.ready.then(function () { term.refresh(0, term.rows - 1); });
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
    tabs.forEach(function (tab, index) {
      var name = tab.getAttribute("data-tab");
      var panel = Array.from(panels).find(function (item) { return item.getAttribute("data-panel") === name; });
      tab.id = "run-tab-" + name;
      tab.setAttribute("role", "tab");
      if (panel) {
        panel.id = "run-panel-" + name;
        panel.setAttribute("role", "tabpanel");
        panel.tabIndex = 0;
        panel.setAttribute("aria-labelledby", tab.id);
        tab.setAttribute("aria-controls", panel.id);
      }
      tab.addEventListener("keydown", function (ev) {
        var next = ev.key === "ArrowRight" ? (index + 1) % tabs.length : ev.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : ev.key === "Home" ? 0 : ev.key === "End" ? tabs.length - 1 : -1;
        if (next < 0) return;
        ev.preventDefault();
        tabs[next].focus();
        activate(tabs[next].getAttribute("data-tab"));
        tabs[next].scrollIntoView({block: "nearest", inline: "nearest"});
      });
    });
    function activate(name) {
      if (name !== "chat" && window.SaberOpsSetMode && document.getElementById("app").dataset.conversationMode !== "work") window.SaberOpsSetMode("work");
      var previous = document.querySelector('.run-tab[aria-selected="true"]');
      var changed = !previous || previous.getAttribute("data-tab") !== name;
      tabs.forEach(function (t) {
        var isActive = t.getAttribute("data-tab") === name;
        t.classList.toggle("is-active", isActive);
        t.setAttribute("aria-selected", isActive ? "true" : "false");
        t.tabIndex = isActive ? 0 : -1;
      });
      var shell = document.querySelector(".run-chat-shell");
      if (shell) shell.classList.toggle("run-chat-active", name === "chat");
      panels.forEach(function (p) {
        var show = p.getAttribute("data-panel") === name;
        p.style.display = show ? "" : "none";
      });
      if (changed) {
        var conversation = document.getElementById("conversation");
        if (conversation) conversation.scrollTo({ top: 0, behavior: "instant" });
      }
      document.dispatchEvent(new CustomEvent("saberops:run-tab", { detail: { tab: name } }));
      try {
        window.localStorage.setItem("orch-run-tab", name);
        var mode = shell && shell.dataset.conversationMode;
        window.history.replaceState(null, "", name === "chat" && mode === "work" ? "#work" : "#" + name);
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
    document.addEventListener("click", function (ev) {
      var link = ev.target.closest("a[data-run-tab]");
      if (!link || !known(link.getAttribute("data-run-tab"))) return;
      ev.preventDefault();
      var name = link.getAttribute("data-run-tab");
      activate(name);
      document.getElementById("run-tab-" + name).focus({ preventScroll: true });
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
    function known(name) { return Array.from(tabs).some(function (tab) { return tab.getAttribute("data-tab") === name; }); }
    window.addEventListener("hashchange", function () {
      var name = window.location.hash === "#work" ? "chat" : window.location.hash.replace("#", "");
      if (known(name)) activate(name);
    });
    if (hashTab === "work") {
      activate("chat");
    } else if (hashTab && known(hashTab)) {
      activate(hashTab);
    } else if (!document.querySelector(".run-chat-shell") && stored && known(stored)) {
      activate(stored);
    } else {
      activate(document.querySelector(".run-chat-shell") ? "chat" : "overview");
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
