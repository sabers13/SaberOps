"use strict";

(function () {
  var THEME_KEY = "orch-theme";
  var MANAGER_TRANSCRIPT_KEY = "orch-manager-transcript";

  // --- Theme Management ---
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
  }

  function storedTheme() {
    var stored = null;
    try {
      stored = window.localStorage.getItem(THEME_KEY);
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

  function toggleTheme() {
    var next = (storedTheme() || systemTheme()) === "dark" ? "light" : "dark";
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch (_err) {
      /* ignore */
    }
    applyTheme(next);
  }

  // --- Elapsed Timer ---
  function initElapsedTimer() {
    var header = document.getElementById("run-header");
    var timerEl = document.getElementById("elapsed-timer");
    if (!header || !timerEl) return;

    var createdAtStr = header.getAttribute("data-created-at");
    var completedAtStr = header.getAttribute("data-completed-at");
    var status = header.getAttribute("data-run-status");
    if (!createdAtStr) return;

    var startTime = new Date(createdAtStr).getTime();

    function formatDuration(ms) {
      if (isNaN(ms) || ms < 0) return "00:00";
      var totalSec = Math.floor(ms / 1000);
      var min = Math.floor(totalSec / 60);
      var sec = totalSec % 60;
      return (min < 10 ? "0" + min : min) + ":" + (sec < 10 ? "0" + sec : sec);
    }

    function update() {
      if (status === "RUNNING" || status === "PENDING") {
        var now = Date.now();
        timerEl.textContent = "Elapsed: " + formatDuration(now - startTime);
      } else if (completedAtStr) {
        var endTime = new Date(completedAtStr).getTime();
        timerEl.textContent = "Duration: " + formatDuration(endTime - startTime);
      }
    }

    update();
    if (status === "RUNNING" || status === "PENDING") {
      var interval = setInterval(function () {
        var currentStatus = header.getAttribute("data-run-status");
        if (currentStatus !== "RUNNING" && currentStatus !== "PENDING") {
          clearInterval(interval);
          update();
        } else {
          update();
        }
      }, 1000);
    }
  }

  // --- SSE Live Streaming ---
  function initEventStream() {
    var header = document.getElementById("run-header");
    if (!header) return;

    var runId = header.getAttribute("data-run-id");
    var status = header.getAttribute("data-run-status");
    var eventsLog = document.getElementById("events-log");
    if (!runId || !eventsLog) return;

    // Find highest event id currently in DOM
    var highestId = 0;
    var existingRows = eventsLog.querySelectorAll(".event-row[data-event-id]");
    existingRows.forEach(function (row) {
      var idVal = parseInt(row.getAttribute("data-event-id") || "0", 10);
      if (idVal > highestId) highestId = idVal;
    });

    var eventSource = new EventSource("/runs/" + encodeURIComponent(runId) + "/events/stream?after=" + highestId);

    function refreshPlanProjection() {
      var pane = document.getElementById("plan-pane");
      if (!pane || !window.fetch) return;
      window.fetch("/runs/" + encodeURIComponent(runId) + "/plan", { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (projection) {
          if (!projection || !projection.plan) return;
          var meta = pane.querySelector(".compact-plan-meta");
          if (meta) {
            meta.innerHTML = "<dt>Worker</dt><dd></dd><dt>Health</dt><dd></dd>" +
              "<dt>Supervisor</dt><dd></dd><dt>Manager</dt><dd></dd><dt>Review</dt><dd></dd>";
            var values = [projection.current_provider || "UNKNOWN", projection.current_model || "UNKNOWN",
              projection.health || "UNKNOWN", projection.supervisor_state || "UNKNOWN",
              projection.manager_state || "UNKNOWN", projection.review_state || "UNKNOWN"];
            var cells = meta.querySelectorAll("dd");
            cells[0].textContent = values[0] + " / " + values[1];
            for (var index = 1; index < cells.length; index += 1) cells[index].textContent = values[index + 1];
          }
          var list = pane.querySelector(".plan-step-list");
          if (list) {
            list.innerHTML = "";
            projection.steps.forEach(function (step) {
              var item = document.createElement("li");
              item.className = "plan-step plan-" + String(step.status).toLowerCase();
              item.textContent = step.status + " " + step.title;
              list.appendChild(item);
            });
          }
        })
        .catch(function () { /* transient SSE/API reconnect failure */ });
    }

    function appendEvent(evData) {
      if (!evData || !evData.id) return;
      if (evData.id <= highestId) return; // Prevent duplicate append
      highestId = evData.id;

      var row = document.createElement("div");
      row.className = "event-row";
      row.setAttribute("data-event-id", evData.id);
      row.style.display = "flex";
      row.style.gap = "0.75rem";

      var numSpan = document.createElement("span");
      numSpan.className = "muted";
      numSpan.textContent = "#" + evData.id;

      var timeSpan = document.createElement("span");
      timeSpan.className = "muted";
      timeSpan.textContent = evData.created_at || "";

      var typeSpan = document.createElement("strong");
      typeSpan.style.color = "var(--primary, #3b82f6)";
      typeSpan.textContent = evData.event_type;

      row.appendChild(numSpan);
      row.appendChild(timeSpan);
      row.appendChild(typeSpan);

      if (evData.attempt_id) {
        var attSpan = document.createElement("span");
        attSpan.className = "muted";
        attSpan.textContent = "[" + evData.attempt_id + "]";
        row.appendChild(attSpan);
      }

      eventsLog.appendChild(row);
      eventsLog.scrollTop = eventsLog.scrollHeight;
      refreshPlanProjection();

      // React to status changes
      if (evData.event_type === "run_completed" || evData.event_type === "run_failed" || evData.event_type === "run_exception" || evData.event_type === "run_started") {
        var badge = document.getElementById("run-status-badge");
        var detailStatus = document.getElementById("detail-status");
        var newStatus = evData.event_type === "run_completed" ? "COMPLETED" : ((evData.event_type === "run_failed" || evData.event_type === "run_exception") ? "FAILED" : "RUNNING");
        var newClass = newStatus.toLowerCase();

        header.setAttribute("data-run-status", newStatus);
        if (badge) {
          badge.className = "badge badge-" + newClass;
          badge.textContent = newStatus;
        }
        if (detailStatus) {
          detailStatus.className = "badge badge-" + newClass;
          detailStatus.textContent = newStatus;
        }
        if (newStatus === "COMPLETED" || newStatus === "FAILED") {
          header.setAttribute("data-completed-at", evData.created_at || new Date().toISOString());
        }
      }
    }

    eventSource.onmessage = function (e) {
      try {
        var data = JSON.parse(e.data);
        appendEvent(data);
      } catch (err) {
        /* ignore parse error */
      }
    };

    // Also listen to named events
    var eventTypes = [
      "run_created", "run_started", "attempt_started", "worker_completed",
      "gate_started", "gate_completed", "attempt_succeeded", "attempt_failed_no_changes",
      "gate_failed", "run_completed", "run_failed", "run_exception",
      "review_started", "review_completed", "review_failed", "run_accepted"
    ];

    eventTypes.forEach(function (type) {
      eventSource.addEventListener(type, function (e) {
        try {
          var data = JSON.parse(e.data);
          appendEvent(data);
        } catch (err) {
          /* ignore */
        }
      });
    });

    eventSource.onerror = function () {
      var currentStatus = header.getAttribute("data-run-status");
      if (currentStatus === "COMPLETED" || currentStatus === "FAILED") {
        eventSource.close();
      }
    };
  }

  // --- Ox Alpha Manager Chat ---
  function initManagerChat() {
    var form = document.getElementById("manager-form");
    var input = document.getElementById("manager-input");
    var container = document.getElementById("chat-container");
    var statusEl = document.getElementById("manager-status");
    var clearBtn = document.getElementById("clear-transcript-btn");
    var sendBtn = document.getElementById("manager-send-btn");
    if (!form || !input || !container) return;

    function renderMessage(role, text, toolCalls) {
      var msgDiv = document.createElement("div");
      msgDiv.className = "chat-msg chat-msg-" + role;
      msgDiv.style.display = "flex";
      msgDiv.style.flexDirection = "column";
      msgDiv.style.gap = "0.25rem";
      msgDiv.style.padding = "0.5rem 0.75rem";
      msgDiv.style.borderRadius = "6px";
      msgDiv.style.maxWidth = "85%";

      if (role === "user") {
        msgDiv.style.alignSelf = "flex-end";
        msgDiv.style.background = "var(--primary, #3b82f6)";
        msgDiv.style.color = "#fff";
      } else {
        msgDiv.style.alignSelf = "flex-start";
        msgDiv.style.background = "var(--bg-card, #1e293b)";
        msgDiv.style.border = "1px solid var(--border-color, #334155)";
      }

      var roleSpan = document.createElement("span");
      roleSpan.style.fontSize = "0.75rem";
      roleSpan.style.fontWeight = "bold";
      roleSpan.style.opacity = "0.7";
      roleSpan.textContent = role === "user" ? "You" : "Ox Alpha Manager";
      msgDiv.appendChild(roleSpan);

      if (toolCalls && toolCalls.length > 0) {
        var toolsDiv = document.createElement("div");
        toolsDiv.style.display = "flex";
        toolsDiv.style.gap = "0.25rem";
        toolsDiv.style.flexWrap = "wrap";
        toolCalls.forEach(function (tc) {
          var pill = document.createElement("span");
          pill.className = "badge badge-normal mono";
          pill.style.fontSize = "0.75rem";
          pill.textContent = "⚡ " + tc.tool;
          toolsDiv.appendChild(pill);
        });
        msgDiv.appendChild(toolsDiv);
      }

      var textP = document.createElement("div");
      textP.style.whiteSpace = "pre-wrap";
      textP.textContent = text;
      msgDiv.appendChild(textP);

      container.appendChild(msgDiv);
      container.scrollTop = container.scrollHeight;
    }

    function loadTranscript() {
      try {
        var saved = window.localStorage.getItem(MANAGER_TRANSCRIPT_KEY);
        if (saved) {
          var items = JSON.parse(saved);
          if (Array.isArray(items)) {
            items.forEach(function (item) {
              renderMessage(item.role, item.text, item.tool_calls);
            });
          }
        }
      } catch (_err) {
        /* ignore */
      }
    }

    function saveTranscriptItem(role, text, toolCalls) {
      try {
        var saved = window.localStorage.getItem(MANAGER_TRANSCRIPT_KEY);
        var items = saved ? JSON.parse(saved) : [];
        if (!Array.isArray(items)) items = [];
        items.push({ role: role, text: text, tool_calls: toolCalls || [] });
        window.localStorage.setItem(MANAGER_TRANSCRIPT_KEY, JSON.stringify(items));
      } catch (_err) {
        /* ignore */
      }
    }

    loadTranscript();

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = input.value.trim();
      if (!text) return;

      input.value = "";
      renderMessage("user", text);
      saveTranscriptItem("user", text);

      if (statusEl) statusEl.textContent = "Ox Alpha is thinking & verifying...";
      if (sendBtn) sendBtn.disabled = true;

      fetch("/manager/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text })
      })
        .then(function (res) {
          return res.json();
        })
        .then(function (data) {
          if (statusEl) statusEl.textContent = "";
          if (sendBtn) sendBtn.disabled = false;
          var reply = data.reply || "No reply returned.";
          var toolCalls = data.tool_calls || [];
          renderMessage("assistant", reply, toolCalls);
          saveTranscriptItem("assistant", reply, toolCalls);
        })
        .catch(function (err) {
          if (statusEl) statusEl.textContent = "";
          if (sendBtn) sendBtn.disabled = false;
          var errMsg = "Error communicating with Ox Alpha manager: " + err;
          renderMessage("assistant", errMsg);
          saveTranscriptItem("assistant", errMsg);
        });
    });

    if (clearBtn) {
      clearBtn.addEventListener("click", function () {
        try {
          window.localStorage.removeItem(MANAGER_TRANSCRIPT_KEY);
        } catch (_err) {
          /* ignore */
        }
        container.innerHTML = '<div class="chat-msg chat-msg-system"><p class="muted">Transcript cleared.</p></div>';
      });
    }

    document.querySelectorAll(".prompt-pill").forEach(function (pill) {
      pill.addEventListener("click", function () {
        var p = pill.getAttribute("data-prompt");
        if (p) {
          input.value = p;
          input.focus();
        }
      });
    });
  }

  // --- Effective Timeouts (progressive enhancement) ---
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

  // --- Review Mode / Limit visibility ---
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

  // --- DOM Ready ---
  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(storedTheme() || systemTheme());
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.addEventListener("click", toggleTheme);
    }

    initElapsedTimer();
    initEventStream();
    initManagerChat();
    initEffectiveTimeouts();
    initReviewMode();
  });
})();
