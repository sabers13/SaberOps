"use strict";
(function () {
  function escape(text) {
    var node = document.createElement("span"); node.textContent = text; return node.innerHTML;
  }
  // Markdown is rendered only for assistant prose, with HTML treated as text.
  window.SaberOpsMarkdown = function (node, text) {
    node.dataset.rawText = text;
    if (!window.marked || !window.DOMPurify) { node.textContent = text; return; }
    var renderer = new window.marked.Renderer();
    renderer.html = function (token) { return escape(token.text); };
    var html = window.marked.parse(text, { gfm: true, breaks: false, renderer: renderer });
    node.innerHTML = window.DOMPurify.sanitize(html, {
      ALLOWED_TAGS: ["p", "br", "hr", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "a"],
      ALLOWED_ATTR: ["href", "title"], ALLOW_DATA_ATTR: false
    });
    node.classList.add("chat-markdown");
    node.querySelectorAll("a[href]").forEach(function (link) {
      try {
        var url = new URL(link.getAttribute("href"), location.href);
        if (!["http:", "https:", "mailto:"].includes(url.protocol)) link.removeAttribute("href");
        else if (url.origin !== location.origin) { link.target = "_blank"; link.rel = "noopener noreferrer"; }
      } catch (_) { link.removeAttribute("href"); }
    });
    node.querySelectorAll("pre").forEach(function (pre) {
      var button = document.createElement("button"); button.type = "button";
      button.className = "chat-code-copy"; button.textContent = "Copy code";
      var code = pre.querySelector("code"); var raw = code ? code.textContent : pre.textContent;
      button.addEventListener("click", async function () {
        try { await navigator.clipboard.writeText(raw); button.textContent = "Copied"; }
        catch (_) { button.textContent = "Select to copy"; }
      }); pre.appendChild(button);
    });
  };
  document.querySelectorAll(".chat-agent-message .message-body").forEach(function (body) {
    window.SaberOpsMarkdown(body, body.textContent);
  });
  window.SaberOpsIcon = function (name) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16");
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "1.7"); svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", name === "copy" ? "M8 8h12v12H8zM16 8V4H4v12h4" : name === "check" ? "m5 12 4 4L19 6" : "M14 3h7v7m0-7L10 14M10 3H3v18h18v-7");
    svg.appendChild(path); return svg;
  };
  var app = document.querySelector(".workspace-shell");
  if (!app) return;
  var sidebarOpen = document.getElementById("chatSidebarButton");
  if (sidebarOpen) sidebarOpen.addEventListener("click", function () { document.getElementById("leftRailToggle").click(); });
  document.querySelectorAll("[data-project-folder]").forEach(function (folder) {
    var key = "saberops-project-folder:" + folder.dataset.projectFolder;
    try { if (localStorage.getItem(key) !== null) folder.open = localStorage.getItem(key) === "true"; } catch (_) { /* optional */ }
    folder.addEventListener("toggle", function () { try { localStorage.setItem(key, String(folder.open)); } catch (_) { /* optional */ } });
  });
  var root = document.getElementById("orchestratorConversation");
  var input = document.getElementById("chatMessage") || document.getElementById("prompt") || document.getElementById("runningTask");
  var form = document.querySelector("[data-chat-form]") || document.getElementById("startRunForm") || document.getElementById("runConversationForm");
  var fixedKind = app.dataset.threadKind;
  if (!["chat", "work"].includes(fixedKind)) fixedKind = null;
  var mode = fixedKind || (root && root.dataset.mode === "work" ? "work" : "chat");
  var runHeader = document.getElementById("run-header");
  var draftKey = root ? null : runHeader ? "saberops-run-chat-draft:" + runHeader.dataset.runId : "saberops-new-chat-draft";
  if (draftKey && input) { try { input.value = localStorage.getItem(draftKey) || input.value; } catch (_) { /* optional */ } }
  function setMode(next) {
    if (fixedKind && next !== fixedKind) {
      location.assign(next === "chat" ? (app.dataset.sourceChat ? "/chats/" + encodeURIComponent(app.dataset.sourceChat) + "#chat" : "/#chat") : "/#work");
      return;
    }
    mode = next;
    app.dataset.conversationMode = mode;
    // Close work drawers before entering plain conversation, preserving console ownership.
    if (mode === "chat") {
      var closeWorkers = document.getElementById("closeWorkersButton");
      if (app.classList.contains("mobile-right-open") && closeWorkers) closeWorkers.click();
      app.classList.add("right-collapsed"); app.style.removeProperty("--right");
      var chatTab = document.querySelector('[data-tab="chat"]');
      if (chatTab && chatTab.getAttribute("aria-selected") !== "true") chatTab.click();
    }
    if (app.classList.contains("chat-shell") &&
        (!location.hash || location.hash === "#chat" || location.hash === "#work")) {
      history.replaceState(null, "", mode === "work" ? "#work" : "#chat");
    }
    document.querySelectorAll("[data-chat-mode]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.chatMode === mode));
    });
    document.querySelectorAll("[data-chat-mode-input]").forEach(function (field) { field.value = mode; });
    var newThread = document.querySelector('[data-od-id="pane-chats"]');
    if (newThread) {newThread.href = mode === "work" ? "/#work" : "/#chat";newThread.querySelector("span").textContent = mode === "work" ? "New work" : "New chat";}
    document.querySelectorAll("[data-project-new-thread]").forEach(function (label) { label.textContent = mode === "work" ? "New work in project" : "New chat in project"; label.href = mode === "work" ? "/#work" : "/#chat"; });
    var hint = document.getElementById("chatModeHint");
    var welcome = document.querySelector("[data-chat-welcome]");
    if (welcome) welcome.textContent = mode === "chat" ? "What can I help with?" : "What would you like to work on?";
    var welcomeText = document.querySelector("[data-chat-welcome-text]");
    if (welcomeText) welcomeText.textContent = mode === "chat" ? "Ask a question, think through an idea, or turn your conversation into work." : "Describe a change. Your workers implement it, then checks and review run before you decide.";
    var permission = document.getElementById("chatPermissionHint");
    var text = mode === "chat" ? "Discussion only" : "Configured workers · project checks";
    if (hint) hint.textContent = text;
    if (permission) permission.textContent = mode === "chat" ? "Discussion only" : "Candidate changes";
    if (input) input.placeholder = mode === "chat" ? "Ask anything, or think through an idea…" : "Describe what you want to build or change…";
    if (!root) { var footer = document.querySelector(".composer-hint"); if (footer) footer.textContent = mode === "chat" ? "Enter to send · Shift+Enter for a new line" : "Implementation uses your configured workers and project policy."; }
    if (mode === "work" && app.dataset.hasInspector === "true") {
      var control = document.getElementById(innerWidth <= 860 ? "mobileWorkersButton" : "rightCollapseButton");
      if (control && (innerWidth <= 860 ? !app.classList.contains("mobile-right-open") : app.classList.contains("right-collapsed"))) control.click();
    }
    document.dispatchEvent(new CustomEvent("saberops:conversation-mode", {detail: {mode: mode}}));
  }
  window.SaberOpsSetMode = setMode;
  document.querySelectorAll("[data-chat-mode]").forEach(function (button) {
    button.addEventListener("click", function () { if (!form || form.getAttribute("aria-busy") !== "true") setMode(button.dataset.chatMode); });
  });
  if (location.hash && location.hash !== "#chat" && document.querySelector("[data-tab]")) mode = "work";
  if (["#work", "#execution", "#gate-storage"].includes(location.hash)) mode = "work";
  if (location.hash === "#chat") mode = "chat";
  if (fixedKind) mode = fixedKind;
  setMode(mode);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () {setMode(mode);}, {once:true});
  document.querySelectorAll('[data-tab]:not([data-tab="chat"])').forEach(function (tab) { tab.addEventListener("click", function () { if (mode !== "work") setMode("work"); }); });
  window.addEventListener("hashchange", function () { if (document.querySelector("[data-tab]")) setMode(fixedKind || (location.hash === "#chat" ? "chat" : "work")); });
  function resize() {
    if (!input) return;
    input.style.height = "auto"; input.style.height = Math.min(170, input.scrollHeight) + "px";
  }
  if (input) {
    input.addEventListener("input", function () { resize(); if (draftKey) { try { localStorage.setItem(draftKey, input.value); } catch (_) { /* optional */ } } });
    resize();
    if (!root) input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); form.requestSubmit(); }
    });
  }
  if (!root && form) form.addEventListener("submit", function () { try { localStorage.removeItem(draftKey); } catch (_) { /* optional */ } });
  document.querySelectorAll(".chat-dialog").forEach(function (dialog) {
    dialog.addEventListener("keydown", function (event) { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dialog.close(); } });
  });
  var modelSwitcher = document.querySelector(".chat-model-switcher");
  var modelPanel = document.getElementById("chatModelPanel");
  if (modelPanel) document.body.appendChild(modelPanel);
  function moveModelPicker(tab) {
    if (!modelSwitcher) return;
    var composer = tab && tab !== "chat" ? document.querySelector(".run-secondary-composer .composer-bar") :
      document.querySelector("[data-chat-form] .composer-actions, #startRunForm .composer-actions, .run-composer .composer-bar");
    if (composer) composer.insertBefore(modelSwitcher, composer.querySelector(":scope > .send-button"));
  }
  moveModelPicker();
  document.addEventListener("saberops:run-tab", function (event) {moveModelPicker(event.detail.tab);});
  var workDialog = document.getElementById("chatWorkDialog");
  var brief = document.getElementById("chatWorkBrief");
  function promote() {
    if (!input || !form || form.getAttribute("aria-busy") === "true") return;
    var last = document.querySelectorAll(".chat-user-message .message-body");
    brief.value = input.value.trim().replace(/^Implement the following brief:\s*/, "") || (last.length ? last[last.length - 1].textContent : "");
    if (brief.value.trim().startsWith("<")) brief.value = "";
    workDialog.showModal(); brief.focus();
  }
  var promoteButton = document.getElementById("chatPromote");
  if (promoteButton) promoteButton.addEventListener("click", promote);
  document.getElementById("chatWorkBriefForm").addEventListener("submit", function (event) {
    event.preventDefault();
    if (!brief.value.trim()) return;
    if (fixedKind === "chat") {
      form.dataset.promoting = "true";
      form.querySelector("[data-chat-mode-input]").value = "work";
    } else setMode("work");
    input.value = "Implement the following brief:\n\n" + brief.value.trim();
    input.dispatchEvent(new Event("input")); workDialog.close(); form.requestSubmit();
  });
  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () { button.closest("dialog").close(); });
  });
  var searchDialog = document.getElementById("chatSearchDialog");
  var search = document.getElementById("chatSearchInput");
  function searchChats() {
    var results = document.getElementById("chatSearchResults"); results.replaceChildren();
    document.querySelectorAll('.side-pane[data-side-pane="chats"] a[data-history-kind]').forEach(function (link) {
      if (!link.textContent.toLowerCase().includes(search.value.toLowerCase())) return;
      var clone = link.cloneNode(true); clone.removeAttribute("aria-current"); results.appendChild(clone);
    });
    if (!results.childElementCount) results.textContent = "No matching chats or runs.";
  }
  search.addEventListener("input", searchChats);
  document.querySelectorAll("[data-chat-search]").forEach(function (button) {
    button.addEventListener("click", function () { search.value = ""; searchChats(); searchDialog.showModal(); search.focus(); });
  });
  var scroll = document.getElementById("agentChatScroll");
  if (scroll) {
    var jump = document.createElement("button"); jump.type = "button"; jump.className = "chat-jump";
    jump.textContent = "↓"; jump.setAttribute("aria-label", "Jump to latest message"); jump.hidden = true;
    document.getElementById("mainWorkspace").appendChild(jump);
    function updateJump() { jump.hidden = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100; }
    scroll.addEventListener("scroll", updateJump);
    jump.addEventListener("click", function () { scroll.scrollTop = scroll.scrollHeight; });
  }
})();
