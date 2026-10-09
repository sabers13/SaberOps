"use strict";

/* SaberOps console shell controller.
 *
 * Visual/interaction source: OpenDesign project
 * 25e96df8-02ca-468a-9335-0ea38e57308f, artifact
 * orchestrator-console-v2.html (historical shell; reconstruction in DESIGN.md).
 *
 * Ownership: this file drives shell chrome (panes, theme, palette,
 * settings modal, dialogs, composer progressive enhancement). It owns
 * the ONE theme implementation (read, toggle, persist, control state;
 * base.html carries only the pre-paint snippet). Run live data comes
 * from the run page's single SSE stream (app.js) and PTY terminal
 * (terminal.js) -- this file never opens its own EventSource.
 */
(function () {
  var THEME_KEY = "saberops-theme";
  var PALETTE_KEY = "saberops-palette";
  var DEFAULT_PALETTE = "graphite-blue";
  var LEGACY_THEME_KEY = "orch-theme";

  function storedTheme() {
    var stored = null;
    try {
      stored = window.localStorage.getItem(THEME_KEY);
      if ((stored === "light" || stored === "dark")) return stored;
      // One-way migration from the legacy sidebar key.
      var legacy = window.localStorage.getItem(LEGACY_THEME_KEY);
      if (legacy === "light" || legacy === "dark") {
        window.localStorage.setItem(THEME_KEY, legacy);
        return legacy;
      }
    } catch (_err) {
      stored = null;
    }
    return stored === "light" || stored === "dark" ? stored : null;
  }

  function systemTheme() {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function currentTheme() {
    var mode = document.documentElement.getAttribute("data-theme");
    return mode === "light" || mode === "dark" ? mode : storedTheme() || systemTheme();
  }

  function validPalette(palette) {
    return palette === "graphite-blue" || palette === "neutral-ink";
  }

  function storedPalette() {
    try {
      var palette = window.localStorage.getItem(PALETTE_KEY);
      if (validPalette(palette)) return palette;
    } catch (_err) { /* Keep the default when browser storage is unavailable. */ }
    return DEFAULT_PALETTE;
  }

  function currentPalette() {
    var palette = document.documentElement.getAttribute("data-palette");
    return validPalette(palette) ? palette : storedPalette();
  }

  function applyPalette(palette) {
    if (!validPalette(palette)) return;
    document.documentElement.setAttribute("data-palette", palette);
    try { window.localStorage.setItem(PALETTE_KEY, palette); } catch (_err) { /* ignore */ }
    paintThemeButton();
  }

  function paintThemeButton() {
    var label = document.getElementById("themeLabel");
    var icon = document.getElementById("themeIcon");
    var next = currentTheme() === "dark" ? "Light" : "Dark";
    if (label) label.textContent = next;
    var toggle = document.getElementById("themeToggle");
    if (toggle) toggle.setAttribute("aria-label", "Switch to " + next.toLowerCase() + " mode");
    if (icon) icon.textContent = currentTheme() === "dark" ? "☾" : "☀";
    document.querySelectorAll("[data-set-theme]").forEach(function (btn) {
      if (btn.getAttribute("data-set-theme") === currentTheme()) {
        btn.classList.add("active");
        btn.setAttribute("aria-pressed", "true");
      } else {
        btn.classList.remove("active");
        btn.setAttribute("aria-pressed", "false");
      }
    });
    document.querySelectorAll("[data-set-palette]").forEach(function (btn) {
      var active = btn.getAttribute("data-set-palette") === currentPalette();
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function applyTheme(theme) {
    if (theme !== "light" && theme !== "dark") return;
    document.documentElement.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch (_err) {
      /* ignore */
    }
    paintThemeButton();
  }

  function toggleTheme() {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  }

  function showToast(message) {
    var toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast._timer);
    showToast._timer = window.setTimeout(function () {
      toast.classList.remove("show");
    }, 2600);
  }

  function app() {
    return document.getElementById("app");
  }

  /* --- Sidebar panes --- */
  var PANE_TITLES = { chats: "Projects", projects: "Projects", runs: "Runs", archive: "Archive" };

  function openPane(name) {
    var root = app();
    if (!root) return;
    document.querySelectorAll("[data-pane]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-pane") === name);
    });
    document.querySelectorAll(".side-pane").forEach(function (pane) {
      pane.hidden = pane.getAttribute("data-side-pane") !== name;
    });
    var title = document.getElementById("paneTitle");
    if (title) title.textContent = PANE_TITLES[name] || "Chats";
    root.classList.remove("left-collapsed");
  }

  function toggleLeft() {
    var root = app();
    if (!root) return;
    if (window.innerWidth <= 860) { openDrawer("left"); return; }
    root.classList.toggle("left-collapsed");
    var toggle = document.getElementById("leftRailToggle");
    var expanded = !root.classList.contains("left-collapsed");
    if (toggle) {
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.setAttribute("aria-label", expanded ? "Collapse navigation" : "Expand navigation");
    }
  }

  function toggleRight() {
    var root = app();
    if (!root || root.dataset.hasInspector !== "true") return;
    if (window.innerWidth <= 860) { openDrawer("right"); return; }
    root.classList.toggle("right-collapsed");
    syncDrawerAccess();
    var collapsed = root.classList.contains("right-collapsed");
    var toggle = document.getElementById("rightCollapseButton");
    if (toggle) {
      toggle.setAttribute("aria-label", collapsed ? "Open workers" : "Collapse workers");
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      var dock = document.getElementById("workerDock");
      if (collapsed && dock && dock.contains(document.activeElement)) toggle.focus();
    }
  }

  var drawerOrigin = null;
  var modalOrigin = null;
  var activeModal = null;

  function visibleFocusables(container) {
    return Array.from(container.querySelectorAll('a[href], button, input, select, textarea, [tabindex="0"]')).filter(function (el) {
      return !el.disabled && !el.closest("[inert]") && el.getClientRects().length > 0;
    });
  }

  function restoreFocus(origin) {
    if (origin && !origin.closest("[inert]") && origin.getClientRects().length) origin.focus();
    else {
      var fallback = document.getElementById(window.innerWidth <= 860 ? "mobileNavButton" : "commandOpenButton");
      if (fallback) fallback.focus();
    }
  }

  function syncDrawerAccess() {
    var root = app();
    if (!root) return;
    var mobile = window.innerWidth <= 860;
    var left = mobile && root.classList.contains("mobile-left-open");
    var right = mobile && root.classList.contains("mobile-right-open");
    document.getElementById("navigationSidebar").inert = mobile && !left;
    document.getElementById("mainWorkspace").inert = left || right;
    document.getElementById("workerDock").inert = mobile ? !right : root.classList.contains("right-collapsed");
    document.getElementById("mobileNavButton").setAttribute("aria-expanded", left ? "true" : "false");
    document.getElementById("mobileWorkersButton").setAttribute("aria-expanded", right ? "true" : "false");
  }

  function openDrawer(side) {
    var root = app();
    if (!root || activeModal) return;
    closeDrawers();
    drawerOrigin = document.activeElement;
    root.classList.add("mobile-" + side + "-open");
    syncDrawerAccess();
    var close = document.getElementById(side === "left" ? "closeNavButton" : "closeWorkersButton");
    if (close) close.focus();
  }

  function closeDrawers() {
    var root = app();
    if (root) root.classList.remove("mobile-left-open", "mobile-right-open");
    syncDrawerAccess();
    if (drawerOrigin) { restoreFocus(drawerOrigin); drawerOrigin = null; }
  }

  function openModal(id, firstFocus) {
    var layer = document.getElementById(id);
    if (!layer) return;
    if (activeModal) closeModal(activeModal.id);
    closeDrawers();
    modalOrigin = document.activeElement;
    layer.classList.add("open");
    layer.setAttribute("aria-hidden", "false");
    activeModal = layer;
    var focus = document.getElementById(firstFocus) || visibleFocusables(layer)[0];
    if (focus) focus.focus();
    app().inert = true;
  }

  function closeModal(id) {
    var layer = document.getElementById(id);
    if (!layer || !layer.classList.contains("open")) return;
    layer.classList.remove("open");
    layer.setAttribute("aria-hidden", "true");
    app().inert = false;
    activeModal = null;
    syncDrawerAccess();
    restoreFocus(modalOrigin);
    modalOrigin = null;
  }

  function trapFocus(ev) {
    if (ev.key !== "Tab") return;
    var root = app();
    var container = activeModal || (root.classList.contains("mobile-left-open") ? document.getElementById("navigationSidebar") : root.classList.contains("mobile-right-open") ? document.getElementById("workerDock") : null);
    if (!container) return;
    var items = visibleFocusables(container);
    if (!items.length) { ev.preventDefault(); return; }
    var first = items[0], last = items[items.length - 1];
    if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
  }

  /* --- Command palette --- */
  function openCommands() {
    var layer = document.getElementById("commandLayer");
    if (!layer) return;
    var input = document.getElementById("commandInput");
    if (input) {
      input.value = "";
      filterCommands("");
    }
    openModal("commandLayer", "commandInput");
  }

  function closeCommands() {
    closeModal("commandLayer");
  }

  function filterCommands(query) {
    var q = (query || "").trim().toLowerCase();
    var count = 0;
    document.querySelectorAll("#commandList .command-item").forEach(function (item) {
      var text = (item.textContent || "").toLowerCase();
      var matches = !q || text.indexOf(q) !== -1;
      item.style.display = matches ? "" : "none";
      if (matches && !item.hidden) count++;
    });
    var empty = document.getElementById("commandEmptyResults");
    if (empty) empty.hidden = count > 0;
  }

  /* --- Settings modal --- */
  var SETTING_META = {
    general: ["General", "Interface and workspace defaults"],
    execution: ["Execution", "Timeouts and project execution policy"],
    providers: ["Providers", "Account and API access"],
    models: ["Models", "Discovered model catalog"],
    routing: ["Routing", "Worker routing chains"],
    repositories: ["Repositories", "Active target project for new runs"]
  };

  function renderSettingsPage(name) {
    document.querySelectorAll(".settings-nav-button").forEach(function (btn) {
      var selected = btn.getAttribute("data-settings-page") === name;
      btn.classList.toggle("active", selected);
      if (selected) btn.setAttribute("aria-current", "page");
      else btn.removeAttribute("aria-current");
    });
    document.querySelectorAll("[data-settings-section]").forEach(function (section) {
      section.hidden = section.getAttribute("data-settings-section") !== name;
    });
    var meta = SETTING_META[name] || SETTING_META.general;
    var title = document.getElementById("settingsPageTitle");
    var desc = document.getElementById("settingsPageDescription");
    if (title) title.textContent = meta[0];
    if (desc) desc.textContent = meta[1];
  }

  function openSettingsModal(page) {
    var layer = document.getElementById("settingsLayer");
    if (!layer) return;
    renderSettingsPage(page || "general");
    openModal("settingsLayer", "settingsCloseButton");
  }

  function closeSettingsModal() {
    closeModal("settingsLayer");
  }

  /* --- Cancel run --- */
  function openCancelRunDialog() {
    closeCommands();
    openModal("cancelRunLayer", "keepRunningButton");
  }

  function closeCancelRunDialog() {
    closeModal("cancelRunLayer");
  }

  /* --- Composer --- */
  function fillComposer(text) {
    var prompt = document.getElementById("prompt");
    if (!prompt) return;
    prompt.value = text;
    prompt.focus();
  }

  function copyOutput(button) {
    var article = button ? button.closest(".message") : null;
    var body = article ? article.querySelector(".message-body") : null;
    var text = body ? body.innerText : "";
    if (!text) return;
    function done() {
      showToast("Copied to clipboard");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        showToast("Copy unavailable");
      });
    } else {
      showToast("Copy unavailable");
    }
  }

  /* --- Connection banner state (driven by the run page's single SSE owner) --- */
  function setConnectionState(state, message) {
    var root = app();
    var bannerText = document.getElementById("ambientBannerText");
    var status = document.getElementById("connectionState");
    if (status) status.textContent = state === "live" ? "Connected" : message || state;
    if (bannerText) bannerText.textContent = message || "Connection state";
    if (!root) return;
    root.classList.toggle("disconnected", state === "disconnected");
    root.classList.toggle("reconnected", state === "reconnected");
    if (state === "live") {
      root.classList.remove("disconnected", "reconnected");
    }
  }

  function init() {
    // Keep the pre-paint selection, including when storage is blocked.
    document.documentElement.setAttribute("data-palette", currentPalette());
    paintThemeButton();
    window.addEventListener("storage", function (event) {
      if (event.key === THEME_KEY || event.key === LEGACY_THEME_KEY || event.key === PALETTE_KEY || event.key === null) {
        document.documentElement.setAttribute("data-theme", storedTheme() || systemTheme());
        document.documentElement.setAttribute("data-palette", storedPalette());
        paintThemeButton();
      }
    });
    var root = app();
    if (root && window.innerWidth < 1180) root.classList.add("right-collapsed");
    var inspectorToggle = document.getElementById("rightCollapseButton");
    if (inspectorToggle && root) {
      var collapsed = root.classList.contains("right-collapsed");
      inspectorToggle.setAttribute("aria-label", collapsed ? "Open workers" : "Collapse workers");
      inspectorToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
    syncDrawerAccess();
    window.addEventListener("resize", function () {
      if (window.innerWidth > 860) closeDrawers();
      else syncDrawerAccess();
    });
    var commandOpen = document.getElementById("commandOpenButton");
    if (commandOpen) commandOpen.addEventListener("click", openCommands);

    var params = new URLSearchParams(window.location.search);
    var returnLink = document.getElementById("runReturnLink");
    if (returnLink) {
      var returnKey = "saberops-return:" + window.location.pathname;
      var from = params.get("from");
      if (from !== "inbox" && from !== "runs" && from !== "dashboard") {
        try { from = window.sessionStorage.getItem(returnKey); } catch (_err) { from = null; }
      } else {
        try { window.sessionStorage.setItem(returnKey, from); } catch (_err) { /* ignore */ }
      }
      if (from === "runs") {
        returnLink.href = "/?pane=runs";
        returnLink.textContent = "← Runs";
      } else if (from === "dashboard") {
        returnLink.href = "/";
        returnLink.textContent = "← Workspace";
      }
    }
    // Project history stays grouped on every page, including the runs index.

    document.querySelectorAll("[data-pane]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var pane = btn.getAttribute("data-pane") || "chats";
        if (window.location.pathname !== "/" && (pane === "chats" || pane === "runs")) {
          window.location.assign(pane === "runs" ? "/?pane=runs" : "/");
          return;
        }
        openPane(pane);
        if (pane === "chats" || pane === "runs") {
          window.history.replaceState(null, "", pane === "runs" ? "/?pane=runs" : "/");
        }
      });
    });

    var leftToggle = document.getElementById("leftRailToggle");
    if (leftToggle) leftToggle.addEventListener("click", toggleLeft);
    var rightToggle = document.getElementById("rightCollapseButton");
    if (rightToggle) rightToggle.addEventListener("click", toggleRight);
    var workersHeading = document.getElementById("workersHeading");
    if (workersHeading) {
      workersHeading.addEventListener("click", toggleRight);
      workersHeading.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          toggleRight();
        }
      });
    }

    var mobileNav = document.getElementById("mobileNavButton");
    if (mobileNav) {
      mobileNav.addEventListener("click", function () {
        openDrawer("left");
      });
    }
    var mobileWorkers = document.getElementById("mobileWorkersButton");
    if (mobileWorkers) {
      mobileWorkers.addEventListener("click", function () {
        openDrawer("right");
      });
    }
    var closeNav = document.getElementById("closeNavButton");
    if (closeNav) closeNav.addEventListener("click", closeDrawers);
    var closeWorkers = document.getElementById("closeWorkersButton");
    if (closeWorkers) closeWorkers.addEventListener("click", closeDrawers);
    var backdrop = document.getElementById("drawerBackdrop");
    if (backdrop) backdrop.addEventListener("click", closeDrawers);

    var themeToggle = document.getElementById("themeToggle");
    if (themeToggle) themeToggle.addEventListener("click", toggleTheme);
    document.querySelectorAll("[data-set-theme]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var theme = btn.getAttribute("data-set-theme");
        if (theme === "light" || theme === "dark") applyTheme(theme);
      });
    });
    document.querySelectorAll("[data-set-palette]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyPalette(btn.getAttribute("data-set-palette"));
      });
    });
    var themeCommand = document.getElementById("commandThemeItem");
    if (themeCommand) {
      themeCommand.addEventListener("click", function () {
        toggleTheme();
        closeCommands();
      });
    }

    var railSettings = document.getElementById("railSettingsButton");
    if (railSettings) {
      railSettings.addEventListener("click", function () {
        openSettingsModal("general");
      });
    }
    var commandSettings = document.getElementById("commandSettingsItem");
    if (commandSettings) {
      commandSettings.addEventListener("click", function () {
        closeCommands();
        openSettingsModal("general");
      });
    }
    var settingsClose = document.getElementById("settingsCloseButton");
    if (settingsClose) settingsClose.addEventListener("click", closeSettingsModal);
    var settingsLayer = document.getElementById("settingsLayer");
    if (settingsLayer) {
      settingsLayer.addEventListener("click", function (ev) {
        if (ev.target === settingsLayer) closeSettingsModal();
      });
    }
    document.querySelectorAll(".settings-nav-button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        renderSettingsPage(btn.getAttribute("data-settings-page") || "general");
      });
    });
    var settingsSearch = document.getElementById("settingsSearch");
    if (settingsSearch) {
      settingsSearch.addEventListener("input", function () {
        var q = settingsSearch.value.trim().toLowerCase();
        document.querySelectorAll(".settings-nav-button").forEach(function (btn) {
          var text = (btn.textContent || "").toLowerCase();
          btn.hidden = Boolean(q) && text.indexOf(q) === -1;
        });
      });
    }

    var commandCancel = document.getElementById("commandCancelRun");
    if (commandCancel) commandCancel.addEventListener("click", openCancelRunDialog);
    var keepRunning = document.getElementById("keepRunningButton");
    if (keepRunning) keepRunning.addEventListener("click", closeCancelRunDialog);
    var cancelLayer = document.getElementById("cancelRunLayer");
    if (cancelLayer) {
      cancelLayer.addEventListener("click", function (ev) {
        if (ev.target === cancelLayer) closeCancelRunDialog();
      });
    }

    var commandLayer = document.getElementById("commandLayer");
    if (commandLayer) {
      commandLayer.addEventListener("click", function (ev) {
        if (ev.target === commandLayer) closeCommands();
      });
    }
    var commandInput = document.getElementById("commandInput");
    if (commandInput) {
      commandInput.addEventListener("input", function () {
        filterCommands(commandInput.value);
      });
    }
    var navToggle = document.getElementById("commandNavToggle");
    if (navToggle) {
      navToggle.addEventListener("click", function () {
        toggleLeft();
        closeCommands();
      });
    }
    var workersToggleCmd = document.getElementById("commandWorkersToggle");
    if (workersToggleCmd) {
      workersToggleCmd.addEventListener("click", function () {
        toggleRight();
        closeCommands();
      });
    }

    enhanceRunContent(document);

    document.querySelectorAll("[data-model-search]").forEach(function (input) {
      input.addEventListener("input", function () {
        var container = input.closest(".access-models");
        var query = input.value.trim().toLowerCase();
        var count = 0;
        container.querySelectorAll("[data-model-row]").forEach(function (row) {
          row.hidden = row.querySelector("strong").textContent.toLowerCase().indexOf(query) === -1;
          if (!row.hidden) count++;
        });
        container.querySelector(".model-empty").hidden = count > 0;
      });
    });

    document.addEventListener("keydown", function (ev) {
      trapFocus(ev);
      var mod = ev.metaKey || ev.ctrlKey;
      if (ev.key === "Escape") {
        closeCommands();
        closeSettingsModal();
        closeCancelRunDialog();
        closeDrawers();
        return;
      }
      if (!mod || (activeModal && ev.key.toLowerCase() !== "k")) return;
      var key = ev.key.toLowerCase();
      if (key === "k") {
        ev.preventDefault();
        var layer = document.getElementById("commandLayer");
        if (layer && layer.classList.contains("open")) closeCommands();
        else openCommands();
      } else if (key === "b" && ev.shiftKey) {
        ev.preventDefault();
        toggleRight();
      } else if (key === "b") {
        ev.preventDefault();
        toggleLeft();
      } else if (key === ",") {
        ev.preventDefault();
        openSettingsModal("general");
      }
    });

    // No EventSource here by design: the run page's app.js owns the
    // single SSE stream and reports connection state via
    // SaberOpsConsole.setConnectionState.
    document.querySelectorAll('form[method="post"]').forEach(function (form) {
      if (form.matches("[data-usage-refresh], [data-chat-form]")) return;
      form.addEventListener("submit", function (ev) {
        if (ev.defaultPrevented) return;
        if (form.dataset.pending === "true") { ev.preventDefault(); return; }
        form.dataset.pending = "true";
        form.setAttribute("aria-busy", "true");
        var button = ev.submitter;
        if (button) {
          button.dataset.originalText = button.textContent;
          button.setAttribute("aria-busy", "true");
          if (button.matches(".composer-submit, .composer-stop")) {
            button.setAttribute("aria-label", button.classList.contains("composer-stop") ? "Stopping task" : "Starting task");
            button.classList.add("starting");
          } else button.textContent = form.action.endsWith("/access/test") ? "Checking…" : form.action.endsWith("/access/discover") ? "Refreshing…" : "Working…";
        }
      });
    });
    window.addEventListener("pageshow", function () {
      document.querySelectorAll('form[data-pending="true"]').forEach(function (form) {
        delete form.dataset.pending;
        form.removeAttribute("aria-busy");
        form.querySelectorAll('[data-original-text]').forEach(function (button) {
          if (button.matches(".composer-submit, .composer-stop")) {
            button.classList.remove("starting");
            button.setAttribute("aria-label", button.classList.contains("composer-stop") ? "Stop task" : "Run task");
          } else button.textContent = button.dataset.originalText;
          delete button.dataset.originalText;
          button.removeAttribute("aria-busy");
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.matches || !form.matches("[data-access-check]")) return;
    if (form.dataset.checkPending === "true") { event.preventDefault(); return; }
    form.dataset.checkPending = "true";
    form.setAttribute("aria-busy", "true");
    var button = form.querySelector("button[type=submit]");
    if (button) { button.disabled = true; button.textContent = "Checking access…"; }
    var status = form.querySelector("[role=status]");
    if (status) status.textContent = "Checking the selected model. Please wait; this can take up to 45 seconds.";
  });

  // Rebind only new inspector content after authoritative live refresh.
  function enhanceRunContent(root) {
    root.querySelectorAll(".copy-output").forEach(function (btn) {
      if (btn.dataset.runBound) return;
      btn.dataset.runBound = "true";
      btn.addEventListener("click", function () {
        copyOutput(btn);
      });
    });
    root.querySelectorAll(".worker-card > .worker-card-head").forEach(function (head) {
      if (head.dataset.runBound) return;
      head.dataset.runBound = "true";
      function expand() {
        var card = head.closest(".worker-card");
        if (card) {
          card.classList.toggle("open");
          head.setAttribute("aria-expanded", card.classList.contains("open") ? "true" : "false");
        }
      }
      head.setAttribute("aria-expanded", head.closest(".worker-card").classList.contains("open") ? "true" : "false");
      head.addEventListener("click", expand);
      head.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); expand(); }
      });
    });
    root.querySelectorAll(".worker-mode-tab[data-worker-filter]").forEach(function (tab) {
      if (tab.dataset.runBound) return;
      tab.dataset.runBound = "true";
      tab.addEventListener("click", function () {
        document.querySelectorAll(".worker-mode-tab[data-worker-filter]").forEach(function (other) {
          other.classList.toggle("active", other === tab);
        });
        var wanted = tab.getAttribute("data-worker-filter");
        document.querySelectorAll("#workerDockBody .worker-card").forEach(function (card) {
          if (!wanted || wanted === "all") {
            card.style.display = "";
            return;
          }
          card.style.display = card.getAttribute("data-attempt") === wanted ? "" : "none";
        });
      });
    });

  }

  window.SaberOpsConsole = {
    openPane: openPane,
    toggleLeft: toggleLeft,
    toggleRight: toggleRight,
    openCommands: openCommands,
    closeCommands: closeCommands,
    openSettingsModal: openSettingsModal,
    closeSettingsModal: closeSettingsModal,
    renderSettingsPage: renderSettingsPage,
    openCancelRunDialog: openCancelRunDialog,
    closeCancelRunDialog: closeCancelRunDialog,
    fillComposer: fillComposer,
    copyOutput: copyOutput,
    showToast: showToast,
    toggleTheme: toggleTheme,
    setConnectionState: setConnectionState,
    enhanceRunContent: enhanceRunContent
  };
})();
