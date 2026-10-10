"use strict";
(function () {
  var root = document.getElementById("orchestratorConversation");
  var form = document.querySelector("[data-chat-form]");
  if (!root || !form) return;
  var messages = document.getElementById("chatMessages");
  var input = document.getElementById("chatMessage");
  var send = document.getElementById("chatSend");
  var pending = document.getElementById("chatPending");
  var error = document.getElementById("chatError");
  var endpoint = form.getAttribute("action");
  var draftKey = "saberops-chat-draft:" + root.dataset.chatId;
  var requestId = null;
  var lastDraft = "";
  var requestAttachments = "";
  var requestTarget = null;
  var requestMode = null;
  var stop = document.getElementById("chatStop");
  var liveReply = document.getElementById("chatLiveReply");
  var replyPreview = document.getElementById("chatReplyPreview");
  var thinkingContent = document.getElementById("chatThinkingContent");
  var renderedPreview = "";
  var requestKey = draftKey + ":request";
  var followLatest = true;
  var requestExecution = "single";
  var requestWorkers = 3;
  var posting = false;
  var polling = false;
  var manualControl = null;
  try {
    input.value = localStorage.getItem(draftKey) || "";
    var savedRequest = JSON.parse(localStorage.getItem(requestKey) || "null");
    if (savedRequest) { requestId = savedRequest.id; lastDraft = savedRequest.text; requestTarget = savedRequest.target || endpoint; requestMode = savedRequest.mode || "auto"; requestAttachments = savedRequest.attachments || ""; requestExecution = savedRequest.execution || "single"; requestWorkers = savedRequest.workers || 3; }
  } catch (_) { /* optional */ }
  function saveDraft() {
    try { localStorage.setItem(draftKey, input.value); } catch (_) { /* optional */ }
  }
  input.addEventListener("input", saveDraft);
  function busy(value) {
    send.disabled = value;
    pending.hidden = !value;
    form.setAttribute("aria-busy", String(value));
    document.querySelectorAll("[data-chat-mode]").forEach(function (button) { button.disabled = value || (!!manualControl && button.dataset.chatMode === "work"); });
    var promote = document.getElementById("chatPromote"); if (promote) promote.disabled = value || (!!manualControl && document.getElementById("app").dataset.conversationMode === "work");
  }
  function append(message) {
    if (messages.querySelector('[data-message-id="' + message.id + '"]')) return;
    var article = document.createElement("article");
    var isUser = ["user", "worker_user", "reviewer_user"].includes(message.role);
    article.className = "message " + (isUser ? "chat-user-message" : "chat-agent-message");
    article.dataset.messageId = message.id;
    article.dataset.messageRole = message.role;
    if (message.run_id) article.dataset.runId = message.run_id;
    if (["worker", "reviewer", "worker_user", "reviewer_user"].includes(message.role) || /^\s*<(?:local-command-|command-name>)/.test(message.text)) article.dataset.workOnly = "";
    var avatar = document.createElement("div");
    avatar.className = "avatar" + (message.role === "assistant" ? " assistant" : "");
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = message.role === "assistant" ? "S" : "Y";
    var content = document.createElement("div");
    content.className = "chat-message-content";
    var head = document.createElement("div");
    head.className = "message-head";
    var name = document.createElement("strong");
    name.textContent = {assistant:"Orchestrator",worker:"Worker",reviewer:"Reviewer",worker_user:"You → Worker",reviewer_user:"You → Reviewer"}[message.role] || "You";
    head.appendChild(name);
    if (message.model) {
      var model = document.createElement("small");
      model.className = "muted chat-model-label";
      model.title = message.model;
      model.textContent = message.model;
      head.appendChild(model);
    }
    var body = document.createElement("div");
    body.className = "message-body user-prompt";
    if (!isUser && window.SaberOpsMarkdown) window.SaberOpsMarkdown(body, message.text);
    else body.textContent = message.text;
    body.dataset.rawText = message.text;
    body.id = "chatBody" + message.id;
    content.appendChild(head);
    content.appendChild(body);
    if (message.attachments && message.attachments.length) {
      var files = document.createElement("div"); files.className = "chat-message-attachments";
      message.attachments.forEach(function (item) {files.appendChild(window.SaberOpsAttachmentView(item));});
      content.appendChild(files);
    }
    var actions = document.createElement("div");
    actions.className = "chat-message-actions";
    if (message.text.length > 1400) {
      var expand = document.createElement("button"); expand.type = "button";
      expand.className = "text-button"; expand.dataset.chatExpand = message.id;
      expand.setAttribute("aria-expanded", "false"); expand.setAttribute("aria-controls", body.id);
      expand.dataset.closedLabel = isUser ? "Read full message" : "Read full reply";
      expand.textContent = expand.dataset.closedLabel; actions.appendChild(expand);
    }
    var copy = document.createElement("button"); copy.type = "button";
    copy.className = "text-button"; copy.dataset.chatCopy = message.id;
    copy.setAttribute("aria-label", "Copy message"); copy.title = "Copy message"; copy.appendChild(window.SaberOpsIcon("copy")); actions.appendChild(copy);
    content.appendChild(actions);
    if (message.run_id) {
      var link = document.createElement("a");
      link.className = "text-button chat-run-link"; link.dataset.workOnly = ""; link.setAttribute("aria-label", "View run"); link.title = "View run";
      link.href = "/runs/" + encodeURIComponent(message.run_id) + "?from=dashboard#work";
      link.appendChild(window.SaberOpsIcon("external"));
      actions.appendChild(link);
    }
    
    article.appendChild(content);
    messages.appendChild(article);
  }
  document.addEventListener("saberops:conversation-mode", function () { manualControl = null; refresh(); });
  async function refresh() {
    if (polling || posting) return;
    polling = true;
    try {
      var response = await fetch(endpoint, { credentials: "same-origin" });
      if (!response.ok) throw new Error("Could not refresh the conversation.");
      var data = await response.json();
      var scroller = document.getElementById("agentChatScroll") || document.getElementById("conversation");
      var visible = scroller && scroller.clientHeight > 0;
      var nearBottom = visible && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
      data.messages.forEach(append);
      manualControl = document.getElementById("app").dataset.conversationMode === "work" ? data.manual_control || null : null;
      var shared = data.shared_native_chat || null;
      busy(manualControl ? manualControl.mode !== "MANUAL" : data.busy);
      var recipient = document.getElementById("chatRecipient");
      if (recipient) recipient.textContent = manualControl ? "To " + manualControl.role + " · manual control" : shared && shared.connected ? "To orchestrator · shared native CLI" : "To orchestrator";
      pending.textContent = manualControl ? (manualControl.mode === "REQUESTED" ? "Pausing agent before manual control…" : manualControl.mode === "RESUMING" ? "Returning control to automation…" : "Automation is held. Messages go to the selected agent.") : "Orchestrator is replying…";
      if (!manualControl && shared) pending.textContent = shared.reason || "Waiting for the native CLI reply. Check the terminal for startup or permission prompts.";
      pending.hidden = manualControl ? false : !data.busy;
      if (window.SaberOpsComposerControls) window.SaberOpsComposerControls.replyState(!!data.can_stop);
      else if (stop) { stop.hidden = !data.can_stop; send.hidden = !!data.can_stop; }
      if (liveReply) {
        var progress = data.progress || {};
        liveReply.hidden = !data.busy || !!manualControl || !!shared;
        if (!liveReply.hidden) {
          var preview = progress.text || "";
          if (renderedPreview !== preview) {
            renderedPreview = preview;
            if (window.SaberOpsMarkdown) window.SaberOpsMarkdown(replyPreview, preview);
            else replyPreview.textContent = preview;
          }
          document.getElementById("chatThinkingLabel").textContent = progress.thinking ? "Thinking" : preview ? "Writing reply" : "Replying";
          document.getElementById("chatElapsed").textContent = (progress.elapsed || 0) + "s";
          thinkingContent.textContent = (progress.thinking || "") + (progress.activity?.length ? "\n\n" + progress.activity.join("\n") : "");
          if (!thinkingContent.textContent.trim()) thinkingContent.textContent = "Waiting for the provider to report activity.";
          pending.hidden = true;
        }
      }
      // A Work conversation has one run view. Preserve its conversation draft key
      // when the first run appears; original Chat never follows this navigation.
      if (root.dataset.threadKind === "work" && data.work_url && !data.busy &&
          !document.getElementById("run-header") && !document.querySelector("dialog[open]")) {
        saveDraft(); location.replace(data.work_url); return;
      }
      document.dispatchEvent(new CustomEvent("saberops:chat-refreshed", {detail: data}));
      if (error.dataset.kind === "refresh") error.hidden = true;
      if (visible && (followLatest || nearBottom)) scroller.scrollTop = scroller.scrollHeight;
      if (visible) followLatest = false;
    } catch (err) {
      error.dataset.kind = "refresh";
      error.textContent = err.message + " Reconnecting…";
      error.hidden = false;
    } finally { polling = false; }
  }
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var attachments = form.querySelector('[name="attachments"]')?.value || "";
    if (posting || send.disabled || (!input.value.trim() && !attachments)) return;
    var text = input.value || "Please review the attached files.";
    var modeField = form.querySelector("[data-chat-mode-input]");
    var selectedMode = modeField ? modeField.value : "auto";
    var execution = selectedMode === "chat" ? "single" : form.querySelector('[name="execution_mode"]')?.value || "single";
    var workers = Number(form.querySelector('[name="max_parallel_workers"]')?.value || 3);
    if (execution === "parallel" && manualControl) {
      error.textContent = "Return control to the app before starting a parallel team.";
      error.hidden = false; return;
    }
    if (attachments && manualControl && selectedMode !== "chat") {
      error.textContent = "Switch to Chat to send these attachments to the orchestrator."; error.hidden = false; return;
    }
    if (lastDraft !== text || !requestId || requestMode !== selectedMode || requestAttachments !== attachments || requestExecution !== execution || requestWorkers !== workers) {
      requestId = window.crypto.randomUUID();
      lastDraft = text;
      requestMode = selectedMode;
      requestAttachments = attachments;
      requestExecution = execution; requestWorkers = workers;
      requestTarget = root.dataset.threadKind === "chat" && selectedMode === "work" ? "/chats/" + encodeURIComponent(root.dataset.chatId) + "/work" : manualControl && selectedMode !== "chat" ? manualControl.endpoint + "/messages" : endpoint;
      try { localStorage.setItem(requestKey, JSON.stringify({id: requestId, text: text, target: requestTarget, mode: requestMode, attachments: requestAttachments, execution: requestExecution, workers: requestWorkers})); } catch (_) { /* optional */ }
    }
    var body = new FormData();
    body.append("message", text);
    body.append("request_id", requestId);
    body.append("mode", requestMode || "auto");
    body.append("attachments", requestAttachments);
    body.append("execution_mode", requestExecution);
    body.append("max_parallel_workers", String(requestWorkers));
    followLatest = true;
    posting = true;
    busy(true);
    error.hidden = true;
    try {
      var response = await fetch(requestTarget, { method: "POST", body: body, credentials: "same-origin" });
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || "Your message could not be sent.");
      if (input.value === text) { input.value = ""; input.dispatchEvent(new Event("input")); }
      saveDraft();
      if (form.saberopsClearAttachments) form.saberopsClearAttachments();
      requestId = null;
      requestTarget = null;
      requestMode = null;
      lastDraft = "";
      try { localStorage.removeItem(requestKey); } catch (_) { /* optional */ }
      if (result.redirect_url) {location.assign(result.redirect_url);return;}
    } catch (err) {
      if (form.dataset.promoting) {delete form.dataset.promoting;form.querySelector("[data-chat-mode-input]").value = "chat";}
      error.dataset.kind = "send";
      error.textContent = err.message;
      error.hidden = false;
      busy(false);
    } finally { posting = false; }
    await refresh();
  });
  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  messages.addEventListener("click", async function (event) {
    var expand = event.target.closest("[data-chat-expand]");
    if (expand) {
      var article = expand.closest(".message");
      var opened = article.classList.toggle("chat-expanded");
      expand.setAttribute("aria-expanded", String(opened));
      expand.textContent = opened ? "Show less" : expand.dataset.closedLabel;
    }
    var copy = event.target.closest("[data-chat-copy]");
    if (copy) {
      try { await navigator.clipboard.writeText((copy.closest(".message").querySelector(".message-body").dataset.rawText || copy.closest(".message").querySelector(".message-body").textContent)); copy.replaceChildren(window.SaberOpsIcon("check")); copy.title = "Copied"; copy.setAttribute("aria-label", "Message copied"); setTimeout(function () { copy.replaceChildren(window.SaberOpsIcon("copy")); copy.title = "Copy message"; copy.setAttribute("aria-label", "Copy message"); }, 1800); }
      catch (_) { error.textContent = "Copy is unavailable. Select the reply text to copy it."; error.hidden = false; }
    }
  });
  if (stop) stop.addEventListener("click", async function () {
    if (stop.dataset.stopRun) return;
    stop.disabled = true;
    try {
      var response = await fetch("/chats/" + encodeURIComponent(root.dataset.chatId) + "/stop", { method: "POST", credentials: "same-origin" });
      var data = await response.json();
      if (!response.ok) throw new Error(data.error || "Could not stop the reply.");
    } catch (err) { error.textContent = err.message; error.hidden = false; }
    finally { stop.disabled = false; refresh(); }
  });
  var scroller = document.getElementById("agentChatScroll") || document.getElementById("conversation");
  scroller.addEventListener("scroll", function () {
    // Hidden run tabs have zero dimensions. They must not enable automatic following.
    if (scroller.clientHeight > 0 && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight >= 100) followLatest = false;
  });
  document.addEventListener("saberops:control-changed", refresh);
  busy(root.dataset.busy === "true");
  setInterval(refresh, 1000);
  refresh();
})();
