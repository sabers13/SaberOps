"use strict";
(function () {
  var status = document.getElementById("routing-save-status");
  var lists = document.querySelectorAll("[data-routing-order]");
  document.querySelectorAll("[data-routing-assign]").forEach(function (form) {
    var target = form.querySelector("[data-routing-target]");
    function selectBinding() {
      form.querySelector("button[type=submit]").textContent =
        "Add model";
    }
    target.addEventListener("change", selectBinding);
    form.addEventListener("submit", selectBinding);
    selectBinding();
  });
  if (!status || !lists.length) return;
  function rows(list) { return Array.from(list.querySelectorAll(".routing-candidate")); }
  function ids(list) { return rows(list).map(function (row) { return row.dataset.candidateId; }); }
  function number(list) {
    rows(list).forEach(function (row, i) { row.querySelector(".routing-position").textContent = i + 1; });
  }
  function restore(list, previous) { previous.forEach(function (row) { list.appendChild(row); }); number(list); }
  function announce(message, failed) { status.textContent = message; status.classList.toggle("routing-save-error", !!failed); }
  async function save(list, previous, focus) {
    var expected = previous.map(function (row) { return row.dataset.candidateId; });
    var order = ids(list);
    if (JSON.stringify(order) === JSON.stringify(expected)) return;
    list.dataset.busy = "true";
    list.setAttribute("aria-busy", "true");
    announce("Saving model order…");
    try {
      var body = new URLSearchParams({context: list.dataset.context, order: JSON.stringify(order), expected_order: JSON.stringify(expected)});
      var response = await fetch("/routing/reorder", {method: "POST", body: body, credentials: "same-origin"});
      var result = await response.json();
      if (!response.ok) throw new Error(result.error || "Order not saved. Refresh and try again.");
      number(list);
      var role = list.closest(".routing-role");
      var primary = role && role.querySelector(".routing-role-primary");
      if (primary) primary.textContent = rows(list)[0].querySelector(".routing-candidate-name").textContent;
      announce("Model order saved.");
    } catch (error) {
      restore(list, previous);
      announce(error.message || "Order not saved. Check your connection and refresh the page.", true);
    } finally {
      delete list.dataset.busy;
      list.removeAttribute("aria-busy");
      if (focus) focus.focus({preventScroll: true});
    }
  }
  lists.forEach(function (list) {
    var drag = null;
    list.addEventListener("pointerdown", function (event) {
      var handle = event.target.closest(".routing-drag");
      if (!handle || list.dataset.busy || event.button !== 0 || !event.isPrimary) return;
      event.preventDefault();
      handle.focus({preventScroll: true});
      drag = {row: handle.closest(".routing-candidate"), previous: rows(list), pointer: event.pointerId, handle: handle};
      drag.row.classList.add("is-dragging");
      list.setPointerCapture(event.pointerId);
      announce("Moving model. Drop to save; Escape to cancel.");
    });
    list.addEventListener("pointermove", function (event) {
      if (!drag || event.pointerId !== drag.pointer) return;
      var target = document.elementFromPoint(event.clientX, event.clientY);
      target = target && target.closest(".routing-candidate");
      if (target && target.parentElement === list && target !== drag.row) {
        var bounds = target.getBoundingClientRect();
        list.insertBefore(drag.row, event.clientY < bounds.top + bounds.height / 2 ? target : target.nextSibling);
        number(list);
      }
      var scroller = list.closest(".conversation");
      if (scroller) {
        var box = scroller.getBoundingClientRect();
        if (event.clientY < box.top + 55) scroller.scrollTop -= 18;
        else if (event.clientY > box.bottom - 55) scroller.scrollTop += 18;
      }
    });
    function finish(event, cancelled) {
      if (!drag || (event.pointerId !== undefined && event.pointerId !== drag.pointer)) return;
      var ended = drag;
      drag = null;
      ended.row.classList.remove("is-dragging");
      if (list.hasPointerCapture(ended.pointer)) list.releasePointerCapture(ended.pointer);
      if (cancelled) { restore(list, ended.previous); announce("Reordering cancelled."); }
      else save(list, ended.previous, ended.handle);
    }
    list.addEventListener("pointerup", function (event) { finish(event, false); });
    list.addEventListener("pointercancel", function (event) { finish(event, true); });
    list.addEventListener("lostpointercapture", function (event) { if (drag) finish(event, true); });
    list.addEventListener("keydown", function (event) {
      if (drag && event.key === "Escape") { event.preventDefault(); finish(event, true); return; }
      var handle = event.target.closest(".routing-drag");
      if (!handle || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      if (list.dataset.busy || drag) return;
      var previous = rows(list);
      var row = handle.closest(".routing-candidate");
      var index = previous.indexOf(row);
      var next = event.key === "ArrowUp" ? index - 1 : index + 1;
      if (next < 0 || next >= previous.length) return;
      list.insertBefore(row, next < index ? previous[next] : previous[next].nextSibling);
      number(list);
      save(list, previous, handle);
    });
    list.addEventListener("submit", function (event) {
      if (list.dataset.busy || drag) { event.preventDefault(); announce("Finish reordering before removing a model."); }
    });
  });
})();
