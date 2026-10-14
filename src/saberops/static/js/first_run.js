/* SaberOps first-run assets (Step 019 early slice, UI-1).
 * Progressive enhancement ONLY: local step navigation within this page.
 * This script never calls a backend and performs no remote requests of any
 * kind. It never reads, writes, or transmits login material
 * or provider state; the only storage it touches is the theme key already
 * owned by console.js, and only to keep the documented appearance toggle
 * working if that control is added later. Preview-only controls stay
 * disabled in markup; this script never enables them. Without this script
 * the page degrades to all sections visible via anchor links. */
(function () {
  "use strict";

  var STEP_COUNT = 10;

  var TITLES = {
    1: "Install",
    2: "Providers",
    3: "Models",
    4: "Posture",
    5: "Routing",
    6: "Project",
    7: "Plan",
    8: "Readiness",
    9: "Monitor",
    10: "Accept"
  };

  function sections() {
    return Array.prototype.slice.call(
      document.querySelectorAll("section[data-fr-step]")
    );
  }

  function statusEl() {
    return document.getElementById("fr-status");
  }

  function currentFromHash() {
    var m = /^#fr-step-(\d+)$/.exec(window.location.hash || "");
    if (m) {
      var n = parseInt(m[1], 10);
      if (n >= 1 && n <= STEP_COUNT) {
        return n;
      }
    }
    return 1;
  }

  function show(n) {
    sections().forEach(function (s) {
      s.hidden = parseInt(s.getAttribute("data-fr-step"), 10) !== n;
    });
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-step-link]"),
      function (a) {
        if (parseInt(a.getAttribute("data-step-link"), 10) === n) {
          a.setAttribute("aria-current", "step");
        } else {
          a.removeAttribute("aria-current");
        }
      }
    );
    var status = statusEl();
    if (status) {
      status.textContent =
        "Preview step " + n + " of " + STEP_COUNT + ": " + TITLES[n] +
        ". No live backend.";
    }
    if (window.location.hash !== "#fr-step-" + n) {
      window.history.replaceState(null, "", "#fr-step-" + n);
    }
    var card = document.getElementById("fr-step-" + n);
    if (card) {
      var h = card.querySelector("h2");
      if (h) {
        h.setAttribute("tabindex", "-1");
        h.focus({ preventScroll: false });
      }
    }
  }

  function tickProgress() {
    var bar = document.getElementById("fr-bar");
    if (!bar || !bar.parentElement) {
      return;
    }
    bar.style.width = "65%";
    bar.parentElement.setAttribute("aria-valuenow", "65");
    var status = statusEl();
    if (status) {
      status.textContent =
        "Preview: progress illustration at 65%. No job executed, nothing transmitted.";
    }
  }

  function init() {
    var nav = document.getElementById("fr-stepnav");
    if (nav) {
      nav.addEventListener("click", function (e) {
        var a = e.target && e.target.closest
          ? e.target.closest("[data-step-link]")
          : null;
        if (!a) {
          return;
        }
        e.preventDefault();
        show(parseInt(a.getAttribute("data-step-link"), 10));
      });
    }
    document.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target : null;
      if (!t) {
        return;
      }
      var navBtn = t.closest("[data-nav]");
      if (navBtn) {
        show(parseInt(navBtn.getAttribute("data-nav"), 10));
        return;
      }
      if (t.closest("[data-demo-tick]")) {
        tickProgress();
      }
    });
    window.addEventListener("hashchange", function () {
      show(currentFromHash());
    });
    show(currentFromHash());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
