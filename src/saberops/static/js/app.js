"use strict";

// Run-page live controller. Loaded ONLY on the run-detail page.
// Ownership: this file owns the single run-event EventSource per page
// plus the elapsed timer and accept/action refresh. Shell chrome, theme,
// and composer enhancement live in console.js; the PTY terminal lives in
// terminal.js.

(function () {
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
              "<dt>Supervisor</dt><dd></dd><dt>Review</dt><dd></dd>";
            var cells = meta.querySelectorAll("dd");
            cells[0].textContent =
              (projection.current_provider || "UNKNOWN") +
              " / " +
              (projection.current_model || "UNKNOWN");
            if (cells[1]) cells[1].textContent = projection.health || "UNKNOWN";
            if (cells[2]) cells[2].textContent = projection.supervisor_state || "UNKNOWN";
            if (cells[3]) cells[3].textContent = projection.review_state || "UNKNOWN";
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

      // React to status changes: patch badges immediately, then refresh
      // every action control from authoritative server state so Accept /
      // Review / Cancel / Cleanup never stay stale from page load.
      // Owner-action events (queued/started/completed/failed/uncertain)
      // arrive on an already-terminal run and must also trigger an
      // authoritative /actions refresh so Review/Accept buttons and
      // status update from durable server state without a reload.
      if (evData.event_type === "owner_action_queued" || evData.event_type === "owner_action_started" || evData.event_type === "owner_action_completed" || evData.event_type === "owner_action_failed" || evData.event_type === "owner_action_uncertain") {
        refreshActionState();
      }
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
        refreshActionState();
      }
    }

    // --- Authoritative action-state refresh (single SSE owner) ---
    function refreshActionState() {
      if (!window.fetch) return;
      window.fetch("/runs/" + encodeURIComponent(runId) + "/actions", { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (actions) {
          if (!actions) return;
          var host = document.getElementById("run-actions");
          if (host && actions.status) {
            host.setAttribute("data-run-status", actions.status);
          }
          var flags = {
            retry: !!actions.can_retry,
            cancel: !!actions.can_cancel,
            review: !!actions.can_review,
            cleanup: !!actions.can_cleanup,
            accept: !!actions.can_accept
          };
          Object.keys(flags).forEach(function (name) {
            var scope = document.querySelector('[data-action="' + name + '"]');
            if (!scope) return;
            var button = scope.tagName === "BUTTON" ? scope : scope.querySelector("button");
            if (!button) return;
            if (flags[name]) {
              button.removeAttribute("disabled");
            } else {
              button.setAttribute("disabled", "disabled");
            }
          });
          var checklist = document.getElementById("accept-checklist");
          if (checklist && Array.isArray(actions.accept_checklist)) {
            checklist.setAttribute("data-accept-eligible", actions.can_accept ? "true" : "false");
            actions.accept_checklist.forEach(function (check) {
              var row = checklist.querySelector('[data-check-key="' + check.key + '"]');
              if (!row) return;
              row.setAttribute("data-check-status", check.status);
              var mark = row.querySelector(".mono");
              if (mark) mark.textContent = check.status === "pass" ? "✓" : "✗";
              var reason = row.querySelector(".muted");
              if (reason && check.reason) reason.textContent = check.reason;
            });
          }
          var emptyNote = document.getElementById("actions-empty-note");
          if (emptyNote) {
            var anyEnabled = Object.keys(flags).some(function (name) { return flags[name]; });
            emptyNote.style.display = anyEnabled ? "none" : "";
          }
        })
        .catch(function () { /* transient SSE/API reconnect failure */ });
    }

    function reportConnection(state, message) {
      if (window.SaberOpsConsole && typeof window.SaberOpsConsole.setConnectionState === "function") {
        window.SaberOpsConsole.setConnectionState(state, message);
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

    // Also listen to named events. Owner-action events are subscribed
    // explicitly so post-run Review/Accept work on an already-terminal
    // run reaches this same EventSource (no second polling loop).
    var eventTypes = [
      "run_created", "run_started", "attempt_started", "worker_completed",
      "gate_started", "gate_completed", "attempt_succeeded", "attempt_failed_no_changes",
      "gate_failed", "run_completed", "run_failed", "run_exception",
      "review_started", "review_completed", "review_failed", "run_accepted",
      "owner_action_queued", "owner_action_started", "owner_action_completed",
      "owner_action_failed", "owner_action_uncertain"
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

    eventSource.onopen = function () {
      reportConnection("live");
    };

    eventSource.onerror = function () {
      var currentStatus = header.getAttribute("data-run-status");
      if (currentStatus === "COMPLETED" || currentStatus === "FAILED") {
        // R3-E2.1: a terminal run may still have an active post-run
        // owner action (Review/Accept). Keep the stream usable in that
        // case; close only once no owner action remains active. The
        // single authoritative /actions read below is not a poll loop.
        if (window.fetch) {
          window.fetch("/runs/" + encodeURIComponent(runId) + "/actions", { credentials: "same-origin" })
            .then(function (response) { return response.ok ? response.json() : null; })
            .then(function (actions) {
              var ownerActive = !!(actions && (actions.owner_review_active || actions.owner_accept_active));
              if (!ownerActive) {
                eventSource.close();
                reportConnection("live");
              } else {
                reportConnection("live");
              }
            })
            .catch(function () {
              eventSource.close();
              reportConnection("live");
            });
          return;
        }
        eventSource.close();
        reportConnection("live");
        return;
      }
      reportConnection("disconnected", "Connection lost — retrying the live event stream…");
    };
  }

  // --- DOM Ready ---
  // Theme is owned by console.js (plus the pre-paint snippet in
  // base.html); composer enhancement also lives in console.js so it
  // runs on pages where this file is not loaded.
  document.addEventListener("DOMContentLoaded", function () {
    initElapsedTimer();
    initEventStream();
  });
})();
