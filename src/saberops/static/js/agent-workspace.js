"use strict";
(function () {
  // Delegation also covers server-refreshed Changes content.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-diff-expand], [data-diff-collapse]");
    if (!button) return;
    var expanded = button.hasAttribute("data-diff-expand");
    document.querySelectorAll(".file-diff").forEach(function (file) { file.open = expanded; });
  });
  var root = document.getElementById("agentWorkspace");
  if (!root) return;
  var endpoint = root.dataset.sessionEndpoint;
  var cards = document.getElementById("agentSessionCards");
  var select = document.getElementById("agentSessionSelect");
  var feedback = document.getElementById("agentWorkspaceFeedback");
  var error = document.getElementById("agentSessionError");
  var follow = document.getElementById("agentFollow");
  var sessions = [], selected = null, layout = "drawer", polling = false, capabilities = {};
  var signature = "", pendingFocus = null, controlBusy = false;
  var storageKey = "saberops-agent-layout-v3";
  var popped = new URLSearchParams(window.location.search).get("console");
  var expandedSession = null;
  var consoleDialog = document.createElement("dialog");
  consoleDialog.id = "agentConsoleDialog"; consoleDialog.className = "agent-console-dialog";
  consoleDialog.setAttribute("aria-labelledby", "agentConsoleTitle");
  var dialogHeader = element("div", "agent-console-dialog-head");
  var dialogTitle = element("strong", "", "Agent console"); dialogTitle.id = "agentConsoleTitle";
  dialogHeader.appendChild(dialogTitle);
  var dialogClose = button("Close console", function () { consoleDialog.close(); });
  dialogHeader.appendChild(dialogClose); consoleDialog.appendChild(dialogHeader);
  var dialogOutput = element("pre", "agent-console-dialog-output mono"); dialogOutput.tabIndex = 0;
  consoleDialog.appendChild(dialogOutput);
  consoleDialog.appendChild(element("p", "agent-console-dialog-hint", "Captured output · Open the native CLI to interact with this agent."));
  document.body.appendChild(consoleDialog);
  consoleDialog.addEventListener("close", function () {
    var card = Array.from(cards.querySelectorAll("[data-session-id]")).find(function (c) { return c.dataset.sessionId === expandedSession; });
    var opener = card && card.querySelector('[data-action="expand"]');
    expandedSession = null;
    if (opener) opener.focus({preventScroll:true});
  });
  consoleDialog.addEventListener("keydown", function (event) { if (event.key === "Escape") event.stopPropagation(); });
  function updateExpandedConsole() {
    var session = sessions.find(function (s) { return s.id === expandedSession; });
    if (!session) return;
    dialogTitle.textContent = label(session) + " · " + stateLabel(session);
    var atEnd = dialogOutput.scrollHeight - dialogOutput.scrollTop - dialogOutput.clientHeight < 30;
    var text = consoleText((session.completion && session.response) || session.output || "No output received yet.");
    if (session.native_client_output) text += "\n\n── Native CLI output ──\n" + session.native_client_output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "");
    if (dialogOutput.textContent !== text) dialogOutput.textContent = text;
    if (atEnd) dialogOutput.scrollTop = dialogOutput.scrollHeight;
  }
  function syncRunLayout() {
    var panel = document.querySelector('.run-tab-panel[data-panel="chat"]');
    var active = !panel || panel.style.display !== "none";
    var shell = document.querySelector(".app");
    if (shell) shell.classList.toggle("agent-workspace-active", active);

  }
  document.addEventListener("saberops:run-tab", syncRunLayout);
  syncRunLayout();
  var nav = document.querySelector(".run-tabs");
  if (nav) {
    var shell = document.createElement("div"); shell.className = "run-tabs-shell";
    nav.before(shell); shell.appendChild(nav);
  }
  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function announce(message) { feedback.textContent = message; feedback.hidden = false; feedback.scrollIntoView({block:"nearest"}); }
  function setLayout(value) {
    layout = value;
    root.dataset.layout = value;
    document.querySelectorAll("[data-agent-layout]").forEach(function (button) {
      button.classList.toggle("is-active", button.dataset.agentLayout === value);
      button.setAttribute("aria-pressed", String(button.dataset.agentLayout === value));
    });
    var shell = document.querySelector(".app");
    shell.classList.toggle("agent-all-consoles", value === "tiles");
    if (value === "tiles") shell.classList.remove("right-collapsed");
    try { localStorage.setItem(storageKey, value); } catch (_) { /* optional */ }
    render();
    fitWorkspace();
  }
  function fitWorkspace() {
    var container = document.getElementById("conversation");
    var origin = root.getBoundingClientRect().top + (container ? container.scrollTop : 0);
    var toolbar = root.querySelector(".agent-workspace-toolbar").getBoundingClientRect().height;
    var bannerHeight = 0;
    root.querySelector(".agent-workspace-body").style.height = Math.max(380, Math.min(740, window.innerHeight - origin - toolbar - bannerHeight - 48)) + "px";
  }
  window.addEventListener("resize", function () { requestAnimationFrame(fitWorkspace); });
  function consoleText(raw) {
    // Preserve all events without interpreting output as markup or commands.
    return raw.split("\n").map(function (line) {
      try {
        var event = JSON.parse(line);
        var text = event.reply || event.text || (event.part && event.part.text) || (event.item && event.item.text);
        if (text) return text;
        return JSON.stringify(event, null, 2);
      } catch (_) { return line; }
    }).join("\n");
  }
  function label(session) {
    return session.role.charAt(0).toUpperCase() + session.role.slice(1) + " · " + session.provider + " / " + session.model;
  }
  function stateLabel(session) {
    var state = session.completion ? session.completion.status : session.state;
    return {ACTIVE:"Working", SUCCESS:"Finished", DONE:"Finished", FAILED:"Failed",
      TIMEOUT:"Timed out", EXPIRED:"Timed out", INTERRUPTED:"Interrupted",
      UNKNOWN:"Result unknown", UNCERTAIN:"Result unknown", REQUESTED:"Pausing",
      MANUAL:"Under your control", RESUMING:"Returning to automation"}[state] || state;
  }
  function button(labelText, handler) {
    var node = element("button", "text-button", labelText);
    node.type = "button"; node.addEventListener("click", handler); return node;
  }
  function render() {
    var previousSelect = select.value;
    select.replaceChildren();
    sessions.forEach(function (session) {
      var opt = element("option", "", label(session) + " · " + stateLabel(session));
      opt.value = session.id; select.appendChild(opt);
    });
    if (!sessions.length) {
      var empty = element("option", "", "No agent calls recorded yet"); select.appendChild(empty);
    }
    select.value = selected || previousSelect;
    var visible = popped ? sessions.filter(function (s) { return s.id === popped; }) : sessions;
    var count = document.querySelector("#workerDock .worker-count");
    if (count) count.textContent = sessions.length;

    var focused = document.activeElement;
    if (focused && cards.contains(focused) && focused.closest("[data-session-id]")) pendingFocus = { id: focused.closest("[data-session-id]").dataset.sessionId, action: focused.dataset.action };
    var remembered = {};
    cards.querySelectorAll("[data-session-id]").forEach(function (card) {
      var output = card.querySelector(".agent-console-output");
      var detail = card.querySelector(".agent-final-response");
      remembered[card.dataset.sessionId] = {expanded:card.open, scroll:output.scrollTop, height:output.style.height,
        atEnd:output.scrollHeight - output.scrollTop - output.clientHeight < 30,
        opened:detail && detail.open, info:card.querySelector(".agent-session-details") && card.querySelector(".agent-session-details").open};
    });
    cards.replaceChildren();
    if (!visible.length) cards.appendChild(element("p", "muted agent-session-empty", "No session output yet. Agent calls will appear here as the orchestrator dispatches them."));
    visible.forEach(function (session) {
      var card = element("details", "agent-session-card" + (!session.completion && session.state === "ACTIVE" ? " is-active" : ""));
      card.dataset.sessionId = session.id;
      card.open = layout === "tiles" || session.id === selected;
      var head = element("summary", "agent-session-head");
      var heading = element("div", "");
      heading.appendChild(element("strong", "", label(session)));
      head.appendChild(heading); head.appendChild(element("span", "badge", stateLabel(session))); card.appendChild(head);
      var finalOutput = session.completion && session.response;
      var output = element("pre", "agent-console-output mono", consoleText(finalOutput || session.output || "No output has been received for this call yet."));
      output.tabIndex = 0; output.setAttribute("aria-label", label(session) + " output");
      card.appendChild(output);
      var consoleTools = element("div", "agent-console-tools");
      var expand = button("Expand console", function () {
        expandedSession = session.id; updateExpandedConsole(); consoleDialog.showModal();
      });
      expand.dataset.action = "expand"; consoleTools.appendChild(expand);
      if (session.view_truncated || session.truncated) consoleTools.appendChild(element("span", "muted", "Recent output · full history in details"));
      card.appendChild(consoleTools);
      if (session.native_client) {
        card.appendChild(element("p", "agent-status-note", "Native CLI: " + ({STARTING:"Opening",ACTIVE:"Open",DONE:"Closed",FAILED:"Closed with an error",INTERRUPTED:"Disconnected"}[session.native_client.state] || session.native_client.state) + (session.native_client.error ? " · " + session.native_client.error : "")));
        if (session.native_client_output) {
          var clientOutput = element("details", "agent-client-output");
          clientOutput.appendChild(element("summary", "", "Recent native CLI output"));
          clientOutput.appendChild(element("pre", "agent-console-output mono", session.native_client_output.replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")));
          card.appendChild(clientOutput);
        }
      }
      if (!session.native_cli_available && !session.native_attach_ready) card.appendChild(element("p", "muted agent-access-reason", session.native_cli_access_reason || session.native_cli_reason));
      if (session.response && !finalOutput) {
        var detail = element("details", "agent-final-response");
        detail.dataset.disclosure = "response";
        detail.appendChild(element("summary", "", "Final agent response"));
        detail.appendChild(element("pre", "agent-console-output mono", session.response)); card.appendChild(detail);
      }
      var actions = element("div", "agent-session-actions");
      var focus = button(layout === "tiles" ? "Focus console" : "Show all", function () { selected = session.id; follow.checked = false; setLayout(layout === "tiles" ? "drawer" : "tiles"); });
      focus.dataset.action = "focus";
      var popout = button("Pop out console ↗", function () {
        var url = new URL(window.location.href); url.searchParams.set("console", session.id);
        if (document.getElementById("run-header")) url.hash = "chat";
        window.open(url.toString(), "_blank", "noopener");
      }); popout.dataset.action = "popout";
      var terminal = button("View log in terminal ↗", async function () {
        terminal.disabled = true;
        try {
          var runEndpoint = capabilities.run_id ? "/runs/" + encodeURIComponent(capabilities.run_id) + "/sessions" : endpoint;
          var response = await fetch(runEndpoint + "/" + encodeURIComponent(session.id) + "/open-terminal", {method:"POST", credentials:"same-origin"});
          var data = await response.json();
          if (!response.ok) throw new Error(data.error || "Could not open terminal.");
          announce("Opened a read-only desktop terminal viewer. This does not attach to or control the provider CLI.");
        } catch (err) { announce(err.message); } finally { terminal.disabled = false; }
      });
      terminal.disabled = !capabilities.native_terminal_available;
      terminal.title = capabilities.native_terminal_reason || "Open captured CLI output in a read-only desktop terminal";
      terminal.dataset.action = "terminal";
      var controlBase = sessionScope() + "/" + encodeURIComponent(session.id);
      if (session.shared_native_chat && session.shared_native_chat.connected) {
        card.appendChild(element("p", "agent-status-note", "App and native CLI share this conversation. Messages use one provider session."));
      }
      if (session.native_cli_available || session.native_attach_ready) {
        var native = button(session.native_cli_mode === "reopen" ? "Reopen saved CLI ↗" : session.native_cli_mode === "handoff" ? "Take over in CLI ↗" : "Open native CLI ↗", function () {
          controlAction(controlBase + "/open-native-cli", "Opened the exact provider session. For a handoff, automation stays held until the CLI closes and you return control.");
        });
        native.dataset.action = "native"; actions.appendChild(native);
        native.disabled = controlBusy;
        var multiplex = button("Add to tmux ↗", function () {
          controlAction(controlBase + "/open-native-cli?backend=tmux", "Opened the shared tmux workspace.");
        });
        multiplex.dataset.action = "tmux"; actions.appendChild(multiplex);
        multiplex.disabled = controlBusy || !(capabilities.tmux && capabilities.tmux.available);
      }
      if (session.can_takeover) {
        var take = button("Take control", function () { controlAction(controlBase + "/takeover", "Pausing agent. Manual input is available after the process stops."); });
        take.dataset.action = "takeover"; actions.appendChild(take);
      } else if (!session.completion && session.control_mode === "MANUAL") {
        var resume = button("Return to automation", function () { controlAction(controlBase + "/control/resume", "Return requested. SaberOps will verify the worktree before resuming."); });
        resume.dataset.action = "resume"; actions.appendChild(resume);
        var stop = button("Interrupt reply", function () { controlAction(controlBase + "/control/interrupt", "Interrupt requested. Automation stays held."); });
        stop.dataset.action = "interrupt"; actions.appendChild(stop);
        var synced = button("Open agent in CLI ↗", function () { controlAction(controlBase + "/open-synchronized-cli", "Opened synchronized CLI. Messages and replies appear in Chat too."); });
        synced.dataset.action = "synced"; actions.appendChild(synced);
      }
      card.appendChild(actions);
      var info = element("details", "agent-session-details");
      info.dataset.disclosure = "info";
      info.appendChild(element("summary", "", "Session details"));
      info.appendChild(element("p", "muted", session.source + " · " + session.association));
      if (session.native_id) info.appendChild(element("p", "mono muted", "Native session: " + session.native_id));
      info.appendChild(element("p", "muted", session.actual_model ? "Reported: " + (session.actual_provider || session.provider) + " / " + session.actual_model : "Requested model shown; provider identity is unconfirmed."));
      info.appendChild(element("p", "muted", session.can_takeover ? "Take control pauses automation. The original time limit continues." : session.native_cli_reason));
      if (session.control && session.control.error) info.appendChild(element("p", "so-error", session.control.error));
      if (session.native_access) info.appendChild(element("p", "muted", session.native_access === "shared-server" ? "Native shared session · terminal clients do not own execution" : session.native_access === "saved-session" ? "Saved provider session · exact-session resume uses a controlled handoff" : "Captured monitoring only"));
      if (session.completion) info.appendChild(element("p", "muted", "Completion: " + session.completion.status + " · source snapshot: " + (session.completion.source_tree || "unavailable")));
      if (session.completion) info.appendChild(element("p", "muted", "Checks and Review show the run's verification outcome separately."));
      info.appendChild(popout); info.appendChild(terminal);
      if (session.control) {
        var history = element("a", "text-button", "Download full session history");
        history.href = controlBase + "/history"; info.appendChild(history);
      }
      card.appendChild(info);
      var body = element("div", "agent-session-body");
      Array.from(card.children).slice(1).forEach(function (child) { body.appendChild(child); });
      card.appendChild(body);
      cards.appendChild(card);
      head.addEventListener("click", function () { selected = session.id; select.value = session.id; follow.checked = false; });
      var previous = remembered[session.id];
      if (previous && previous.height) output.style.height = previous.height;
      if (previous && layout !== "tiles" && !follow.checked) card.open = previous.expanded;
      output.scrollTop = !previous || previous.atEnd ? output.scrollHeight : previous.scroll;
      if (previous && previous.opened && card.querySelector(".agent-final-response")) card.querySelector(".agent-final-response").open = true;
      if (previous && previous.info) info.open = true;
    });
    renderControlBanner();
    updateExpandedConsole();
    fitWorkspace();
    if (pendingFocus) {
      var match = Array.from(cards.querySelectorAll("[data-session-id]")).find(function (card) { return card.dataset.sessionId === pendingFocus.id; });
      var action = match && match.querySelector('[data-action="' + pendingFocus.action + '"]');
      if (action) action.focus({preventScroll:true});
      pendingFocus = null;
    }
  }
  function sessionScope() {
    return capabilities.run_id ? "/runs/" + encodeURIComponent(capabilities.run_id) + "/sessions" : endpoint;
  }
  async function controlAction(url, message) {
    if (controlBusy) return;
    controlBusy = true;
    document.querySelectorAll('#agentOpenAllTmux, #agentOpenTmux, [data-action="native"], [data-action="tmux"]').forEach(function (b) { b.disabled = true; });
    announce("Opening or selecting the terminal session…");
    try {
      var response = await fetch(url, {method:"POST", credentials:"same-origin"});
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || "Control action failed.");
      var report = data.message || message;
      if (data.opened_sessions) report += " " + data.opened_sessions.length + " unique sessions opened or selected.";
      if (data.skipped && data.skipped.length) report += " " + data.skipped.length + " could not be added.";
      announce(report);
      if (data.skipped && data.skipped.length) {
        var skipped = element("details", "agent-batch-details");
        skipped.appendChild(element("summary", "", "Unavailable sessions (" + data.skipped.length + ")"));
        var reasons = element("ul", "");
        data.skipped.forEach(function (s) { reasons.appendChild(element("li", "", (s.label || "Session " + s.id) + ": " + s.reason)); });
        skipped.appendChild(reasons); feedback.appendChild(skipped);
      }
      signature = ""; await refresh();
      document.dispatchEvent(new CustomEvent("saberops:control-changed"));
    } catch (err) { announce(err.message); }
    finally { controlBusy = false; signature = ""; refresh(); }
  }
  function renderControlBanner() {
    var banner = document.getElementById("agentManualBanner");
    var session = sessions.find(function (s) { return !s.completion && ["MANUAL","REQUESTED","RESUMING"].includes(s.control_mode); });
    var nativeOpen = sessions.find(function (s) { return s.native_client && ["STARTING","ACTIVE"].includes(s.native_client.state); });
    var sharedOrchestrator = sessions.find(function (s) { return s.role === "orchestrator" && s.shared_native_chat && s.shared_native_chat.connected; });
    var exclusiveOrchestrator = sessions.find(function (s) { return s.role === "orchestrator" && s.native_client && ["STARTING","ACTIVE"].includes(s.native_client.state) && !(s.shared_native_chat && s.shared_native_chat.connected); });
    banner.replaceChildren(); banner.hidden = !session && !sharedOrchestrator && !exclusiveOrchestrator;
    if (exclusiveOrchestrator) banner.appendChild(element("span", "", "An orchestrator CLI owns this chat. Complete its shared-channel startup, or close it before sending app messages."));
    else if (sharedOrchestrator) banner.appendChild(element("span", "", "Shared orchestrator CLI connected. Chat here or in the terminal."));
    if (!session) return;
    var text = session.control_mode === "MANUAL" ? session.role + " is under your control. Automation is held; the original time limit continues." : session.control_mode === "REQUESTED" ? "Pausing " + session.role + " safely…" : "Verifying worktree and returning to automation…";
    banner.appendChild(element("span", "", text));
    if (session.control_mode === "MANUAL") {
      var base = sessionScope() + "/" + encodeURIComponent(session.id);
      var returnButton = button("Return to automation", function () { controlAction(base + "/control/resume", "Return requested."); });
      returnButton.disabled = !!nativeOpen; banner.appendChild(returnButton);
      banner.appendChild(button("Interrupt reply", function () { controlAction(base + "/control/interrupt", "Interrupt requested; automation stays held."); }));
    }
  }
  document.getElementById("agentOpenChatCli").addEventListener("click", function () {
    var base = capabilities.run_id ? "/runs/" + encodeURIComponent(capabilities.run_id) : endpoint.replace(/\/sessions$/, "");
    controlAction(base + "/open-synchronized-cli", "Opened chat in a synchronized desktop CLI.");
  });
  document.getElementById("agentOpenAllTmux").addEventListener("click", function () {
    controlAction(sessionScope() + "/open-tmux?all_sessions=true", "Opened all available sessions in tmux.");
  });
  document.getElementById("agentOpenTmux").addEventListener("click", function () {
    controlAction(sessionScope() + "/open-tmux", "Opened the shared tmux workspace.");
  });
  async function refresh() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      var response = await fetch(endpoint, {credentials:"same-origin", cache:"no-store"});
      if (!response.ok) throw new Error("Could not refresh agent consoles. Existing output is kept; retry Refresh.");
      var data = await response.json();
      capabilities = data;
      var tmuxAvailable = data.tmux && data.tmux.available;
      document.getElementById("agentOpenAllTmux").disabled = controlBusy || !tmuxAvailable || !data.sessions.some(function (s) { return s.native_cli_available; });
      document.getElementById("agentOpenTmux").disabled = controlBusy || !tmuxAvailable;
      var next = JSON.stringify(data);
      error.hidden = true;
      if (next === signature) return;
      signature = next; sessions = data.sessions;
      if (popped && sessions.some(function (s) { return s.id === popped; })) { selected = popped; follow.checked = false; }
      else {
        var active = sessions.find(function (s) { return ["MANUAL","REQUESTED","RESUMING"].includes(s.state); }) || sessions.find(function (s) { return s.state === "ACTIVE"; });
        if (follow.checked && active) selected = active.id;
        if (!sessions.some(function (s) { return s.id === selected; })) selected = sessions.length ? sessions[0].id : null;
      }
      render();
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { polling = false; }
  }
  document.querySelectorAll("[data-agent-layout]").forEach(function (b) { b.addEventListener("click", function () { setLayout(b.dataset.agentLayout); }); });
  select.addEventListener("change", function () { selected = select.value; follow.checked = false;
    var chosen = Array.from(cards.querySelectorAll("[data-session-id]")).find(function (card) { return card.dataset.sessionId === selected; });
    if (chosen) chosen.open = true;
    render();
    var current = Array.from(cards.querySelectorAll("[data-session-id]")).find(function (card) { return card.dataset.sessionId === selected; });
    if (current) { current.open = true; current.scrollIntoView({block:"start", behavior:"smooth"}); }
  });
  follow.addEventListener("change", function () { signature = ""; refresh(); });
  document.getElementById("agentSessionRefresh").addEventListener("click", refresh);
  document.addEventListener("saberops:run-refreshed", refresh);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
  try { var saved = localStorage.getItem(storageKey); if (["drawer", "tiles"].includes(saved)) layout = saved; } catch (_) { /* optional */ }
  if (popped) {
    layout = "drawer"; document.querySelector(".app").classList.add("agent-console-popout");
    requestAnimationFrame(function () { document.getElementById("workerDock").inert = false; });
  }
  setLayout(layout); refresh();
  setInterval(refresh, 2000);
})();
