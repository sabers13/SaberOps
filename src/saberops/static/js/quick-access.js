"use strict";
(function () {
  var quickButton = document.getElementById("quickAccessButton");
  var quickPanel = document.getElementById("quickAccessPanel");
  var modelButton = document.getElementById("chatModelButton");
  var modelPanel = document.getElementById("chatModelPanel");
  var active = null;
  var activeTrigger = null;
  function close(restoreFocus) {
    if (!active) return;
    active.hidden = true;
    activeTrigger.setAttribute("aria-expanded", "false");
    if (restoreFocus) activeTrigger.focus();
    active = activeTrigger = null;
  }
  function toggle(panel, trigger) {
    if (active === panel) { close(true); return; }
    close(false);
    panel.hidden = false;
    if (panel === modelPanel) {
      var theme = getComputedStyle(document.getElementById("app"));
      ["bg", "fg", "surface", "panel", "muted", "border", "accent", "accent-soft"].forEach(function (name) {
        panel.style.setProperty("--" + name, theme.getPropertyValue("--" + name));
      });
      var bounds = trigger.getBoundingClientRect(), width = Math.min(430, innerWidth - 24);
      panel.style.position = "fixed"; panel.style.width = width + "px";
      panel.style.left = Math.max(12, Math.min(innerWidth - width - 12, bounds.right - width)) + "px";
      panel.style.bottom = Math.max(12, innerHeight - bounds.top + 8) + "px";
      panel.style.top = "auto";
    }
    trigger.setAttribute("aria-expanded", "true");
    active = panel; activeTrigger = trigger;
    var first = panel.querySelector("input, summary, button, a");
    if (first) first.focus();
  }
  if (quickButton) quickButton.addEventListener("click", function () {
    toggle(quickPanel, quickButton);
  });
  document.querySelectorAll("[data-close-popover]").forEach(function (button) {
    button.addEventListener("click", function () { close(true); });
  });
  document.addEventListener("pointerdown", function (event) {
    if (active && !active.contains(event.target) && !activeTrigger.contains(event.target)) close(false);
  });
  document.addEventListener("keydown", function (event) {
    if (!active) return;
    if (event.key === "Escape") { event.preventDefault(); close(true); }
    if (event.key === "Tab") {
      var focusable = Array.from(active.querySelectorAll("button, a, input, summary"))
        .filter(function (el) { return !el.disabled && el.getClientRects().length; });
      if (!focusable.length) return;
      var first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  document.getElementById("quickSettingsButton").addEventListener("click", function () {
    close(true); window.SaberOpsConsole.openSettingsModal("general");
  });
  var usageBusy = false;
  var usageLoadedAt = 0;
  var usageContent = document.getElementById("quickUsageContent");
  var usageStatus = document.getElementById("quickUsageStatus");
  var refreshButton = document.getElementById("quickUsageRefresh");
  async function refreshUsage(force) {
    if (usageBusy || (!force && Date.now() - usageLoadedAt < 60000)) return;
    usageBusy = true; refreshButton.disabled = true;
    usageContent.setAttribute("aria-busy", "true");
    usageStatus.textContent = "Reading CLI limits…";
    try {
      if (!usageLoadedAt) {
        var cached = await fetch("/quick-access/usage", {credentials: "same-origin"});
        if (cached.ok) usageContent.innerHTML = await cached.text();
      }
      var response = await fetch("/quick-access/usage", {
        method: "POST", credentials: "same-origin",
        body: new URLSearchParams({force: force ? "1" : "0"})
      });
      if (!response.ok) throw new Error("refresh failed");
      usageContent.innerHTML = await response.text();
      usageLoadedAt = Date.now(); usageStatus.textContent = "Reported account limits";
    } catch (_) { usageStatus.textContent = "Refresh unavailable. Previous readings retained."; }
    finally {
      usageBusy = false; refreshButton.disabled = false; usageContent.removeAttribute("aria-busy");
    }
  }
  document.getElementById("quickUsageDetails").addEventListener("toggle", function () {
    if (this.open) refreshUsage(false);
  });
  refreshButton.addEventListener("click", function () { refreshUsage(true); });

  if (!modelButton) return;
  var models = [], selectedId = "", modelBusy = false;
  var list = document.getElementById("chatModelOptions");
  var status = document.getElementById("chatModelStatus");
  var search = document.getElementById("chatModelSearch");
  var label = document.getElementById("chatModelLabel");
  var providerNames = {codex: "Codex", anthropic: "Claude Code", copilot: "GitHub Copilot",
    opencode: "OpenCode", antigravity: "Antigravity", cline: "Cline"};
  function updateLabel() {
    var current = models.find(function (m) { return m.binding_id === selectedId; });
    label.textContent = current ? current.model : "Choose model";
    modelButton.title = current ? "Orchestrator: " + current.provider + "/" + current.model : "Choose orchestrator model";
  }
  function render() {
    list.replaceChildren();
    var query = search.value.toLowerCase().trim();
    var visible = models.filter(function (m) {
      return (m.model + " " + (providerNames[m.provider] || m.provider) + " " + m.profile_id).toLowerCase().includes(query);
    });
    visible.forEach(function (m) {
      var button = document.createElement("button"); button.type = "button";
      button.className = "chat-model-option";
      button.disabled = modelBusy;
      button.setAttribute("aria-pressed", String(m.binding_id === selectedId));
      var copy = document.createElement("span");
      var name = document.createElement("strong"); name.textContent = m.model;
      var meta = document.createElement("small");
      meta.textContent = (providerNames[m.provider] || m.provider) + " · " + m.profile_id.replace("discovered-profile:", "") + " · " +
        (m.backend === "DIRECT_API" ? "Direct API · API billing" : m.backend === "DIRECT_CLI" ? "Native CLI" : "Gateway");
      copy.append(name, meta); button.append(copy);
      if (m.binding_id === selectedId) {
        var check = document.createElement("span"); check.textContent = "✓"; check.setAttribute("aria-hidden", "true"); button.append(check);
      }
      button.addEventListener("click", function () { selectModel(m.binding_id); }); list.append(button);
    });
    if (!visible.length && !modelBusy) status.textContent = models.length ? "No matching models." : "No chosen orchestrator models available. Add models in Routing.";
    else if (!modelBusy) status.textContent = "Choose a primary from your Orchestrator routing list.";
  }
  async function loadModels() {
    if (modelBusy) return;
    modelBusy = true; status.textContent = "Loading models…";
    try {
      var response = await fetch("/chat/models", {credentials: "same-origin", cache: "no-store"});
      if (!response.ok) throw new Error("model list unavailable");
      var data = await response.json(); models = data.bindings; selectedId = data.selected_binding_id;
      modelBusy = false; updateLabel(); render();
    } catch (_) { modelBusy = false; status.textContent = "Model list unavailable. Open Routing to choose a model."; }
  }
  async function selectModel(bindingId) {
    if (modelBusy) return;
    modelBusy = true; status.textContent = "Saving orchestrator…"; render();
    try {
      var response = await fetch("/chat/model", {method: "POST", credentials: "same-origin",
        body: new URLSearchParams({binding_id: bindingId})});
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || "Selection failed.");
      selectedId = data.selected_binding_id; modelBusy = false; updateLabel(); render(); if (active === modelPanel) close(true);
      window.SaberOpsConsole.showToast("Orchestrator model updated");
    } catch (error) {
      modelBusy = false; render(); status.textContent = error.message;
    }
  }
  modelButton.addEventListener("click", function () {
    toggle(modelPanel, modelButton);
    if (!modelPanel.hidden) { search.focus(); loadModels(); }
  });
  window.addEventListener("focus", loadModels);
  search.addEventListener("input", render);
  loadModels();
})();
