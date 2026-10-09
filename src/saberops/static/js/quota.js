"use strict";
(function () {
  var form = document.querySelector("[data-usage-refresh]");
  var container = document.getElementById("cli-usage-cards");
  if (!form || !container) return;
  var status = form.querySelector("[data-usage-status]");
  var button = form.querySelector("button");
  var busy = false;
  async function refresh(force) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    container.setAttribute("aria-busy", "true");
    status.textContent = "Reading usage from your CLIs…";
    try {
      var response = await fetch("/quota/refresh", {
        method: "POST", credentials: "same-origin",
        body: new URLSearchParams({fragment: "1", force: force ? "1" : "0"})
      });
      if (!response.ok) throw new Error("refresh failed");
      container.innerHTML = await response.text();
      status.textContent = "Usage refreshed. Each card shows its source and availability.";
    } catch (_) {
      status.textContent = "Could not refresh usage. Your previous observations remain visible.";
    } finally {
      busy = false;
      button.disabled = false;
      container.removeAttribute("aria-busy");
    }
  }
  form.addEventListener("submit", function (event) { event.preventDefault(); refresh(true); });
  refresh(false);
})();
