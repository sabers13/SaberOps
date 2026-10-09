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
    if (!createdAtStr) return;

    var startTime = new Date(createdAtStr).getTime();
    var interval;

    function formatDuration(ms) {
      if (isNaN(ms) || ms < 0) return "00:00";
      var totalSec = Math.floor(ms / 1000);
      var min = Math.floor(totalSec / 60);
      var sec = totalSec % 60;
      return (min < 10 ? "0" + min : min) + ":" + (sec < 10 ? "0" + sec : sec);
    }

    function update() {
      var status = header.getAttribute("data-run-status");
      var completedAtStr = header.getAttribute("data-completed-at");
      if (status === "RUNNING" || status === "PENDING") {
        var now = Date.now();
        timerEl.textContent = "Elapsed: " + formatDuration(now - startTime);
      } else if (completedAtStr) {
        var endTime = new Date(completedAtStr).getTime();
        timerEl.textContent = "Duration: " + formatDuration(endTime - startTime);
      }
      if (status !== "RUNNING" && status !== "PENDING") clearInterval(interval);
    }

    update();
    document.addEventListener("saberops:run-refreshed", update);
    if (/^(RUNNING|PENDING)$/.test(header.getAttribute("data-run-status"))) interval = setInterval(update, 1000);
  }

  // --- SSE Live Streaming ---
  function initEventStream() {
    var header = document.getElementById("run-header");
    if (!header) return;

    var runId = header.getAttribute("data-run-id");
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

    // Read the same escaped, server-rendered evidence as a fresh page.
    // Only marked regions are replaced: the composer, confirmation forms,
    // EventSource and PTY remain mounted. Coalesce event bursts and serialize
    // reads so an older response cannot overwrite a newer snapshot.
    var refreshTimer;
    var refreshPending = false;
    var refreshRunning = false;
    function queueDetailsRefresh() {
      if (!window.fetch) return;
      refreshPending = true;
      if (refreshRunning || refreshTimer) return;
      refreshTimer = window.setTimeout(refreshDetails, 250);
    }
    function refreshDetails() {
      refreshTimer = null;
      refreshPending = false;
      refreshRunning = true;
      window.fetch("/runs/" + encodeURIComponent(runId) + window.location.search, { credentials: "same-origin", cache: "no-store" })
        .then(function (response) {
          if (!response.ok) throw new Error("Run details unavailable");
          return response.text();
        })
        .then(function (html) {
          if (refreshPending) return; // An event arrived after this read began; read again.
          var snapshot = new DOMParser().parseFromString(html, "text/html");
          var nextHeader = snapshot.getElementById("run-header");
          if (!nextHeader || nextHeader.getAttribute("data-run-id") !== runId) throw new Error("Invalid run details");
          var conversation = document.getElementById("conversation");
          var scrollTop = conversation ? conversation.scrollTop : 0;
          var activeFilter = document.querySelector(".worker-mode-tab.active[data-worker-filter]");
          var wanted = activeFilter && activeFilter.getAttribute("data-worker-filter");
          document.querySelectorAll("[data-run-refresh]").forEach(function (region) {
            // Live agent widgets own their state and event handlers via the session poller.
            if (region.hasAttribute("data-agent-live")) return;
            var key = region.getAttribute("data-run-refresh");
            var next = snapshot.querySelector('[data-run-refresh="' + key + '"]');
            if (!next) return;
            var opened = Array.from(region.querySelectorAll("details")).map(function (detail) { return detail.open; });
            var fileExpansions = {};
            region.querySelectorAll("details[data-diff-path], details[data-diff-total]").forEach(function (detail) { fileExpansions[detail.hasAttribute("data-diff-total") ? "total:" : "file:" + detail.getAttribute("data-diff-path")] = detail.open; });
            var expansion = {};
            region.querySelectorAll(".worker-card[data-attempt]").forEach(function (card) {
              expansion[card.getAttribute("data-attempt")] = card.classList.contains("open");
            });
            var focused = region.contains(document.activeElement) ? document.activeElement : null;
            var focusedCard = focused && focused.closest(".worker-card[data-attempt]");
            var focusedAttempt = focusedCard && focusedCard.getAttribute("data-attempt");
            region.replaceChildren.apply(region, Array.from(next.childNodes).map(function (node) { return document.importNode(node, true); }));
            region.querySelectorAll("details").forEach(function (detail, index) { if (index < opened.length) detail.open = opened[index]; });
            region.querySelectorAll("details[data-diff-path], details[data-diff-total]").forEach(function (detail) {
              var path = detail.hasAttribute("data-diff-total") ? "total:" : "file:" + detail.getAttribute("data-diff-path");
              if (Object.prototype.hasOwnProperty.call(fileExpansions, path)) detail.open = fileExpansions[path];
            });
            region.querySelectorAll(".worker-card[data-attempt]").forEach(function (card) {
              var attempt = card.getAttribute("data-attempt");
              if (Object.prototype.hasOwnProperty.call(expansion, attempt)) card.classList.toggle("open", expansion[attempt]);
            });
            if (window.SaberOpsConsole) window.SaberOpsConsole.enhanceRunContent(region);
            if (focusedAttempt) {
              var head = region.querySelector('[data-attempt="' + focusedAttempt + '"] > .worker-card-head');
              if (head) head.focus({ preventScroll: true });
            }
          });
          if (wanted) {
            var filter = document.querySelector('[data-worker-filter="' + wanted + '"]');
            if (filter) filter.click();
          }
          // Named SSE subscriptions cover lifecycle updates. Merge other
          // persisted events on the same read, retaining the mounted log and
          // any newer streamed rows beyond the server's display bound.
          snapshot.querySelectorAll("#events-log .event-row[data-event-id]").forEach(function (row) {
            var id = parseInt(row.getAttribute("data-event-id"), 10);
            if (!eventsLog.querySelector('[data-event-id="' + id + '"]')) {
              var later = Array.from(eventsLog.querySelectorAll(".event-row[data-event-id]")).find(function (existing) {
                return parseInt(existing.getAttribute("data-event-id"), 10) > id;
              });
              eventsLog.insertBefore(document.importNode(row, true), later || null);
            }
            highestId = Math.max(highestId, id);
          });
          ["data-run-status", "data-completed-at"].forEach(function (name) { header.setAttribute(name, nextHeader.getAttribute(name) || ""); });
          var badge = document.getElementById("run-status-badge");
          var nextBadge = snapshot.getElementById("run-status-badge");
          if (badge && nextBadge) { badge.textContent = nextBadge.textContent; badge.className = nextBadge.className; }
          if (conversation) conversation.scrollTop = scrollTop;
          var feedback = document.getElementById("run-refresh-status");
          if (feedback) feedback.hidden = true;
          document.dispatchEvent(new CustomEvent("saberops:run-refreshed"));
          refreshActionState();
        })
        .catch(function () {
          var feedback = document.getElementById("run-refresh-status");
          if (feedback) feedback.hidden = false;
        })
        .finally(function () {
          refreshRunning = false;
          if (refreshPending) queueDetailsRefresh();
        });
    }
    document.addEventListener("saberops:run-tab", queueDetailsRefresh);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) queueDetailsRefresh(); });
    var retryRefresh = document.getElementById("runRefreshRetry");
    if (retryRefresh) retryRefresh.addEventListener("click", queueDetailsRefresh);

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
      queueDetailsRefresh();

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
        document.dispatchEvent(new CustomEvent("saberops:run-refreshed"));
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
            document.querySelectorAll('[data-action="' + name + '"]').forEach(function (scope) {
              var button = scope.tagName === "BUTTON" ? scope : scope.querySelector("button");
              if (!button) return;
              if (flags[name]) {
                button.removeAttribute("disabled");
              } else {
                button.setAttribute("disabled", "disabled");
              }
            });
          });
          var stopForm = document.getElementById("runComposerStopForm");
          var newTask = document.getElementById("runComposerNewTask");
          var composerStatus = document.getElementById("runComposerStatus");
          if (stopForm) stopForm.hidden = !actions.can_cancel;
          if (newTask) newTask.hidden = !!actions.can_cancel;
          if (composerStatus) composerStatus.textContent = actions.can_cancel ? "Task is running" : "Run finished";
          document.dispatchEvent(new CustomEvent("saberops:run-action-state", {detail: {run_id:runId, can_cancel:!!actions.can_cancel}}));
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
      "run_created", "run_started", "attempt_started", "worker_started", "worker_completed",
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
      queueDetailsRefresh();
    };

    eventSource.onerror = function () {
      var currentStatus = header.getAttribute("data-run-status");
      if (currentStatus === "COMPLETED" || currentStatus === "FAILED" || currentStatus === "ORPHANED") {
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
