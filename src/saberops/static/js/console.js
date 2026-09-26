"use strict";

/* SaberOps console shell controller.
 *
 * Visual/interaction source: OpenDesign project
 * 25e96df8-02ca-468a-9335-0ea38e57308f, artifact
 * orchestrator-console-v2.html (canonical product UI).
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
    return storedTheme() || systemTheme();
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
      } else {
        btn.classList.remove("active");
      }
    });
  }

  function applyTheme(theme) {
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
  var PANE_TITLES = { chats: "Chats", projects: "Projects", runs: "Runs", archive: "Archive" };

  function openPane(name) {
    var root = app();
    if (!root) return;
    document.querySelectorAll(".rail-button[data-pane]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-pane") === name);
    });
    document.querySelectorAll(".side-pane").forEach(function (pane) {
      pane.hidden = pane.getAttribute("data-side-pane") !== name;
    });
    var title = document.getElementById("paneTitle");
    if (title) title.textContent = PANE_TITLES[name] || "Chats";
    root.classList.remove("left-collapsed");
    closeDrawers();
  }

  function toggleLeft() {
    var root = app();
    if (root) root.classList.toggle("left-collapsed");
  }

  function toggleRight() {
    var root = app();
    if (root) root.classList.toggle("right-collapsed");
  }

  function closeDrawers() {
    var root = app();
    if (root) root.classList.remove("mobile-left-open", "mobile-right-open");
  }

  /* --- Command palette --- */
  function openCommands() {
    var layer = document.getElementById("commandLayer");
    if (!layer) return;
    layer.classList.add("open");
    var input = document.getElementById("commandInput");
    if (input) {
      input.value = "";
      filterCommands("");
      window.setTimeout(function () {
        input.focus();
      }, 30);
    }
  }

  function closeCommands() {
    var layer = document.getElementById("commandLayer");
    if (layer) layer.classList.remove("open");
  }

  function filterCommands(query) {
    var q = (query || "").trim().toLowerCase();
    document.querySelectorAll("#commandList .command-item").forEach(function (item) {
      var text = (item.textContent || "").toLowerCase();
      item.style.display = !q || text.indexOf(q) !== -1 ? "" : "none";
    });
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
      btn.classList.toggle("active", btn.getAttribute("data-settings-page") === name);
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
    layer.classList.add("open");
  }

  function closeSettingsModal() {
    var layer = document.getElementById("settingsLayer");
    if (layer) layer.classList.remove("open");
  }

  /* --- Run actions / cancel --- */
  function toggleRunActions() {
    var menu = document.getElementById("runActionsMenu");
    var button = document.getElementById("runActionsButton");
    if (!menu) return;
    var willShow = menu.hidden;
    menu.hidden = !willShow;
    if (button) button.setAttribute("aria-expanded", willShow ? "true" : "false");
  }

  function closeRunActions() {
    var menu = document.getElementById("runActionsMenu");
    var button = document.getElementById("runActionsButton");
    if (menu) menu.hidden = true;
    if (button) button.setAttribute("aria-expanded", "false");
  }

  function openCancelRunDialog() {
    closeRunActions();
    closeCommands();
    var layer = document.getElementById("cancelRunLayer");
    if (layer) layer.classList.add("open");
  }

  function closeCancelRunDialog() {
    var layer = document.getElementById("cancelRunLayer");
    if (layer) layer.classList.remove("open");
  }

  /* --- Composer --- */
  function toggleAdvancedRun(button) {
    var panel = document.getElementById("advancedRun");
    if (!panel) return;
    var willOpen = !panel.classList.contains("open");
    panel.classList.toggle("open", willOpen);
    if (button) button.setAttribute("aria-expanded", willOpen ? "true" : "false");
  }

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

  /* --- Composer progressive enhancement (dashboard; moved from app.js) ---
   *
   * app.js loads only on the run-detail page, so dashboard-only
   * enhancement must live here in the shell controller that loads
   * everywhere. */

  function initEffectiveTimeouts() {
    var panel = document.getElementById("effective-timeouts");
    if (!panel) return;
    var defaultsRaw = panel.getAttribute("data-timeout-defaults");
    if (!defaultsRaw) return;
    var defaults;
    try {
      defaults = JSON.parse(defaultsRaw);
    } catch (_err) {
      return;
    }

    var routingSel = document.getElementById("routing_mode");
    var manualSel = document.getElementById("manual_tier");
    var maxTierSel = document.getElementById("max_auto_tier");
    var workerInput = document.getElementById("worker_timeout");
    var gateInput = document.getElementById("gate_timeout");
    var reviewInput = document.getElementById("review_timeout");

    function fmt(num) {
      return String(num);
    }

    function workerText() {
      var override = workerInput ? workerInput.value.trim() : "";
      if (override !== "" && !isNaN(parseFloat(override))) {
        return fmt(parseFloat(override)) + "s";
      }
      var manual = manualSel ? manualSel.value : "";
      if (routingSel && routingSel.value === "manual" && manual && defaults[manual] !== undefined) {
        return fmt(defaults[manual]) + "s";
      }
      var ceiling = maxTierSel ? maxTierSel.value : "";
      var order = ["T1", "T2", "T3"];
      var parts = [];
      for (var i = 0; i < order.length; i++) {
        var t = order[i];
        if (defaults[t] === undefined) continue;
        parts.push(t + " " + fmt(defaults[t]) + "s");
        if (t === ceiling) break;
      }
      return parts.join(" / ");
    }

    function valueText(input, key) {
      var v = input ? input.value.trim() : "";
      if (v !== "" && !isNaN(parseFloat(v))) {
        return fmt(parseFloat(v)) + "s";
      }
      return fmt(defaults[key]) + "s";
    }

    function update() {
      var values = {
        worker: workerText(),
        gate: valueText(gateInput, "gate"),
        review: valueText(reviewInput, "review")
      };
      ["worker", "gate", "review"].forEach(function (key) {
        var el = panel.querySelector('[data-eff="' + key + '"]');
        if (el) el.textContent = values[key];
      });
    }

    [routingSel, manualSel, maxTierSel, workerInput, gateInput, reviewInput].forEach(function (el) {
      if (el) {
        el.addEventListener("input", update);
        el.addEventListener("change", update);
      }
    });

    update();
  }

  function initReviewMode() {
    var modeSel = document.getElementById("review_mode");
    var limitField = document.getElementById("review-limit-field");
    var reviewCheck = document.getElementById("review_enabled");
    var reviewControls = document.getElementById("review-controls-row");

    function update() {
      if (modeSel && limitField) {
        var bounded = modeSel.value !== "max";
        limitField.style.display = bounded ? "" : "none";
        var limitInput = document.getElementById("review_limit");
        if (limitInput) limitInput.disabled = !bounded;
      }
      if (reviewCheck && reviewControls) {
        reviewControls.style.opacity = reviewCheck.checked ? "1" : "0.5";
      }
    }

    if (modeSel) modeSel.addEventListener("change", update);
    if (reviewCheck) reviewCheck.addEventListener("change", update);
    update();
  }

  function init() {
    paintThemeButton();

    document.querySelectorAll(".rail-button[data-pane]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        openPane(btn.getAttribute("data-pane") || "chats");
      });
    });

    var leftToggle = document.getElementById("leftRailToggle");
    if (leftToggle) leftToggle.addEventListener("click", toggleLeft);
    var rightToggle = document.getElementById("rightCollapseButton");
    if (rightToggle) rightToggle.addEventListener("click", toggleRight);
    var workersToggle = document.getElementById("workersCollapseButton");
    if (workersToggle) workersToggle.addEventListener("click", toggleRight);
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
        var root = app();
        if (root) root.classList.add("mobile-left-open");
      });
    }
    var mobileWorkers = document.getElementById("mobileWorkersButton");
    if (mobileWorkers) {
      mobileWorkers.addEventListener("click", function () {
        var root = app();
        if (root) root.classList.add("mobile-right-open");
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

    var runActionsButton = document.getElementById("runActionsButton");
    if (runActionsButton) runActionsButton.addEventListener("click", toggleRunActions);
    document.addEventListener("click", function (ev) {
      var menu = document.getElementById("runActionsMenu");
      if (!menu || menu.hidden) return;
      var target = ev.target;
      if (target instanceof Element && (menu.contains(target) || (runActionsButton && runActionsButton.contains(target)))) return;
      closeRunActions();
    });
    var cancelMenuItem = document.getElementById("cancelRunMenuItem");
    if (cancelMenuItem) cancelMenuItem.addEventListener("click", openCancelRunDialog);
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

    document.querySelectorAll(".advanced-toggle").forEach(function (btn) {
      btn.addEventListener("click", function () {
        toggleAdvancedRun(btn);
      });
    });

    document.querySelectorAll(".copy-output").forEach(function (btn) {
      btn.addEventListener("click", function () {
        copyOutput(btn);
      });
    });
    document.querySelectorAll(".worker-card > .worker-card-head").forEach(function (head) {
      head.addEventListener("click", function () {
        var card = head.closest(".worker-card");
        if (card) card.classList.toggle("open");
      });
    });
    document.querySelectorAll(".worker-mode-tab[data-worker-filter]").forEach(function (tab) {
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

    document.addEventListener("keydown", function (ev) {
      var mod = ev.metaKey || ev.ctrlKey;
      if (ev.key === "Escape") {
        closeCommands();
        closeSettingsModal();
        closeCancelRunDialog();
        closeRunActions();
        return;
      }
      if (!mod) return;
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
      } else if (key === "t" && ev.shiftKey) {
        ev.preventDefault();
        toggleTheme();
      } else if (key === ",") {
        ev.preventDefault();
        openSettingsModal("general");
      } else if (key === "n") {
        ev.preventDefault();
        window.location.href = "/";
      }
    });

    // No EventSource here by design: the run page's app.js owns the
    // single SSE stream and reports connection state via
    // SaberOpsConsole.setConnectionState.
    initEffectiveTimeouts();
    initReviewMode();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
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
    toggleAdvancedRun: toggleAdvancedRun,
    fillComposer: fillComposer,
    copyOutput: copyOutput,
    showToast: showToast,
    toggleTheme: toggleTheme,
    setConnectionState: setConnectionState
  };
})();
