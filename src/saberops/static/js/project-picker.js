"use strict";
(function () {
  document.querySelectorAll("[data-project-browse]").forEach(function (button) {
    var form = button.closest("form");
    var status = form.querySelector("[data-project-picker-status]");
    button.addEventListener("click", async function () {
      if (button.disabled) return;
      button.disabled = true;
      status.textContent = "Choose a folder in the window that opens…";
      try {
        var response = await fetch("/projects/browse", {method: "POST", credentials: "same-origin"});
        var data = await response.json();
        if (!response.ok) throw new Error(data.error || "Folder chooser unavailable. You can use a path instead.");
        if (!data.path) { status.textContent = "Folder selection cancelled."; return; }
        form.querySelector("input[name=path]").value = data.path;
        status.textContent = "Selecting project…";
        form.requestSubmit();
      } catch (error) {
        status.textContent = error.message || "Folder chooser unavailable. You can use a path instead.";
      } finally { button.disabled = false; }
    });
  });
})();

(function () {
  var form = document.querySelector("[data-project-workflow]");
  if (!form) return;
  function update() {
    var disabled = form.elements.review_policy.value === "disabled";
    form.querySelector("[data-workflow-review]").hidden = disabled;
    form.querySelector("[data-workflow-limit]").hidden = disabled || form.elements.review_mode.value === "max";
    form.elements.review_limit.required = !disabled && form.elements.review_mode.value === "bounded";
  }
  form.addEventListener("change", function () {
    form.querySelector("[data-workflow-label]").textContent = "Custom";
    form.querySelectorAll("[name=preset]").forEach(function (button) { button.classList.remove("primary-button"); button.setAttribute("aria-pressed", "false"); });
    update();
  });
  form.querySelectorAll("[name=preset]").forEach(function (button) {
    button.addEventListener("click", function () {
      // Presets replace manual values; normalize before browser validation.
      if (button.value !== "custom" || !form.elements.review_limit.required) {
        if (!form.elements.review_limit.value || !form.elements.review_limit.validity.valid) form.elements.review_limit.value = "3";
        if (button.value !== "custom") form.elements.review_limit.required = false;
      }
    });
  });
  update();
})();

(function () {
  function revealPolicy() {
    if (window.location.hash !== "#execution") return;
    var panel = document.getElementById("execution");
    if (panel && panel.tagName === "DETAILS") {
      panel.open = true;
      panel.scrollIntoView({block: "start"});
    }
  }
  window.addEventListener("hashchange", revealPolicy);
  revealPolicy();
})();

(function () {
  var form = document.querySelector("[data-setup-policy]");
  if (!form) return;
  var gate = form.querySelector("[data-setup-gate]");
  function update() {
    if (!gate) return;
    var quick = form.elements.preset.value === "quick";
    gate.hidden = quick;
    form.elements.gate_command.required = !quick;
  }
  form.addEventListener("change", update);
  update();
})();
