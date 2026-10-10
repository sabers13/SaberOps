/* SaberOps visuals: charts and small motion helpers.
 *
 * Presentation only. Every figure is drawn from values the server already
 * rendered on the page (run rows, <progress> elements, token totals, check
 * badges); nothing is fetched, stored or sent, and no existing event, id or
 * data-* contract changes. If a source element is missing the chart is simply
 * not drawn. Labels are written with textContent, never innerHTML.
 */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function number(value) {
    return Number(value).toLocaleString();
  }

  function compact(value) {
    var n = Number(value);
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e4) return (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K";
    return number(n);
  }

  /* ---------------------------------------------------------------- tooltip */
  var tip = null;
  function showTip(anchor, head, rows) {
    if (!tip) {
      tip = el("div", "so-tip");
      tip.setAttribute("role", "presentation");
      document.body.appendChild(tip);
    }
    tip.replaceChildren();
    if (head) tip.appendChild(el("div", "so-tip-head", head));
    rows.forEach(function (row) {
      var line = el("div", "so-tip-row");
      var key = el("i");
      if (row.color) key.style.setProperty("--c", row.color);
      line.appendChild(key);
      line.appendChild(el("strong", "", row.value));
      line.appendChild(el("span", "", row.label));
      tip.appendChild(line);
    });
    var box = anchor.getBoundingClientRect();
    tip.classList.add("show");
    var size = tip.getBoundingClientRect();
    var left = Math.min(Math.max(8, box.left + box.width / 2 - size.width / 2), window.innerWidth - size.width - 8);
    var top = box.top - size.height - 8;
    if (top < 8) top = box.bottom + 8;
    tip.style.left = Math.round(left) + "px";
    tip.style.top = Math.round(top) + "px";
  }
  function hideTip() {
    if (tip) tip.classList.remove("show");
  }
  function bindTip(node, head, rows) {
    node.addEventListener("pointerenter", function () { showTip(node, head, rows); });
    node.addEventListener("pointerleave", hideTip);
    node.addEventListener("focus", function () { showTip(node, head, rows); });
    node.addEventListener("blur", hideTip);
  }
  window.addEventListener("scroll", hideTip, true);

  function countUp(node, target, suffix) {
    suffix = suffix || "";
    if (reduceMotion || !window.requestAnimationFrame || target === 0) {
      node.textContent = number(target) + suffix;
      return;
    }
    var start = null;
    var duration = 700;
    function frame(now) {
      if (start === null) start = now;
      var t = Math.min(1, (now - start) / duration);
      var eased = 1 - Math.pow(1 - t, 3);
      node.textContent = number(Math.round(target * eased)) + suffix;
      if (t < 1) window.requestAnimationFrame(frame);
    }
    node.textContent = "0" + suffix;
    window.requestAnimationFrame(frame);
  }

  function legend(items) {
    var list = el("ul", "so-legend");
    items.forEach(function (item) {
      var li = el("li");
      var swatch = el("i");
      swatch.style.setProperty("--c", item.color);
      li.appendChild(swatch);
      li.appendChild(el("b", "", item.value));
      li.appendChild(document.createTextNode(item.label));
      list.appendChild(li);
    });
    return list;
  }

  /* ------------------------------------------------------------ run history */
  var OUTCOMES = [
    { key: "completed", label: "Completed", color: "var(--viz-good)" },
    { key: "failed", label: "Failed", color: "var(--viz-crit)" },
    { key: "other", label: "Stopped", color: "var(--viz-neutral)" },
    { key: "active", label: "In progress", color: "var(--viz-warn)" }
  ];

  function outcomeOf(status) {
    if (status === "COMPLETED") return "completed";
    if (status === "FAILED") return "failed";
    if (status === "RUNNING" || status === "PENDING") return "active";
    return "other";
  }

  function dayKey(date) {
    return date.getFullYear() + "-" + (date.getMonth() + 1) + "-" + date.getDate();
  }

  function runInsights() {
    var list = document.querySelector('.run-list[aria-label="Run history"]');
    if (!list || document.querySelector(".so-insights")) return;
    var rows = Array.prototype.slice.call(list.querySelectorAll(".run-list-item[data-run-status]"));
    if (rows.length < 2) return;

    var DAYS = 14;
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    var days = [];
    var byKey = {};
    for (var offset = DAYS - 1; offset >= 0; offset -= 1) {
      var date = new Date(today);
      date.setDate(today.getDate() - offset);
      var bucket = { date: date, completed: 0, failed: 0, other: 0, active: 0, total: 0 };
      days.push(bucket);
      byKey[dayKey(date)] = bucket;
    }

    var totals = { completed: 0, failed: 0, other: 0, active: 0 };
    var charted = 0;
    rows.forEach(function (row) {
      var outcome = outcomeOf(row.getAttribute("data-run-status"));
      totals[outcome] += 1;
      var created = new Date(row.getAttribute("data-run-created"));
      if (isNaN(created.getTime())) return;
      var target = byKey[dayKey(created)];
      if (!target) return;
      target[outcome] += 1;
      target.total += 1;
      charted += 1;
    });

    var finished = totals.completed + totals.failed + totals.other;
    var rate = finished ? Math.round((totals.completed / finished) * 100) : null;
    var peak = Math.max.apply(null, days.map(function (day) { return day.total; }));

    var card = el("section", "so-viz so-card so-insights");
    card.setAttribute("aria-label", "Run summary");

    var stats = el("dl", "so-stats");
    function stat(label, hero) {
      var wrap = el("div", "so-stat" + (hero ? " so-stat-hero" : ""));
      wrap.appendChild(el("dt", "", label));
      var value = el("dd");
      wrap.appendChild(value);
      stats.appendChild(wrap);
      return value;
    }
    var rateNode = stat("Completion rate", true);
    if (rate === null) {
      rateNode.textContent = "–";
    } else {
      var rateNumber = el("span");
      rateNode.appendChild(rateNumber);
      countUp(rateNumber, rate, "%");
    }
    rateNode.appendChild(el("small", "", finished ? totals.completed + " of " + finished + " finished" : "No finished runs"));
    var totalNode = stat("Runs listed");
    var totalNumber = el("span");
    totalNode.appendChild(totalNumber);
    countUp(totalNumber, rows.length);
    var activeNode = stat("In progress");
    activeNode.appendChild(el("span", "", number(totals.active)));
    card.appendChild(stats);

    var chart = el("div", "so-chart");
    var head = el("div", "so-chart-head");
    var titles = el("div");
    titles.appendChild(el("h3", "so-viz-title", "Runs per day"));
    titles.appendChild(el("p", "so-viz-note", "Last " + DAYS + " days, by current status · " + charted + " of the " + rows.length + " runs listed"));
    head.appendChild(titles);
    head.appendChild(legend(OUTCOMES.filter(function (o) { return totals[o.key] > 0; }).map(function (o) {
      return { color: o.color, value: totals[o.key], label: o.label };
    })));
    chart.appendChild(head);

    var plot = el("div", "so-plot");
    var axisMax = Math.max(2, peak % 2 ? peak + 1 : peak);
    var yaxis = el("div", "so-yaxis");
    yaxis.setAttribute("aria-hidden", "true");
    [0, axisMax / 2, axisMax].forEach(function (tick) {
      var label = el("span", "", tick);
      label.style.bottom = (tick / axisMax) * 100 + "%";
      yaxis.appendChild(label);
    });
    plot.appendChild(yaxis);

    var cols = el("div", "so-cols");
    cols.setAttribute("role", "list");
    var grid = el("div", "so-grid");
    grid.setAttribute("aria-hidden", "true");
    [0, 0.5, 1].forEach(function (fraction) {
      var line = el("i");
      line.style.bottom = fraction * 100 + "%";
      grid.appendChild(line);
    });
    cols.appendChild(grid);

    var dayFormat = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
    days.forEach(function (day, index) {
      var col = el("div", "so-col");
      col.setAttribute("role", "listitem");
      col.tabIndex = 0;
      col.style.setProperty("--i", index);
      var isToday = index === days.length - 1;
      if (isToday) col.setAttribute("data-today", "");
      var title = isToday ? "Today" : dayFormat.format(day.date);
      var parts = OUTCOMES.filter(function (o) { return day[o.key] > 0; });
      col.setAttribute("aria-label", title + ": " + (day.total
        ? parts.map(function (o) { return day[o.key] + " " + o.label.toLowerCase(); }).join(", ")
        : "no runs"));
      if (day.total) {
        var stack = el("div", "so-stack");
        parts.forEach(function (o) {
          var segment = el("span", "so-seg");
          segment.style.setProperty("--c", o.color);
          segment.style.height = (day[o.key] / axisMax) * 100 + "%";
          stack.appendChild(segment);
        });
        col.appendChild(stack);
      } else {
        col.appendChild(el("span", "so-empty-day"));
      }
      var showLabel = isToday || (days.length - 1 - index) % 2 === 0;
      var xlabel = el("span", "so-xlabel", isToday ? "Today" : day.date.getDate());
      if (!showLabel) xlabel.style.visibility = "hidden";
      col.appendChild(xlabel);
      bindTip(col, title, day.total
        ? parts.map(function (o) { return { color: o.color, value: day[o.key], label: o.label }; })
        : [{ value: "0", label: "runs" }]);
      cols.appendChild(col);
    });
    plot.appendChild(cols);
    chart.appendChild(plot);
    card.appendChild(chart);
    list.parentNode.insertBefore(card, list);
  }

  /* ----------------------------------------------------------------- meters */
  function tone(remaining) {
    if (remaining < 20) return "crit";
    if (remaining <= 50) return "warn";
    return "ok";
  }

  function meters(root) {
    Array.prototype.forEach.call(root.querySelectorAll("progress:not(.so-meter-source)"), function (progress) {
      if (!progress.closest(".usage-window, .quick-usage-window, .quick-usage-provider")) return;
      var max = Number(progress.max) || 100;
      var value = Math.max(0, Math.min(100, (Number(progress.value) / max) * 100));
      var meter = el("div", "so-meter");
      meter.setAttribute("aria-hidden", "true");
      meter.setAttribute("data-tone", tone(value));
      var fill = el("span");
      fill.style.setProperty("--v", value);
      meter.appendChild(fill);
      progress.classList.add("so-meter-source");
      progress.insertAdjacentElement("afterend", meter);
    });
  }

  function usageRings() {
    var host = document.getElementById("cli-usage-cards");
    if (!host) return;
    var existing = host.parentNode.querySelector(".so-rings");
    if (existing) existing.remove();
    var entries = [];
    Array.prototype.forEach.call(host.querySelectorAll(".usage-card"), function (card) {
      var tightest = null;
      Array.prototype.forEach.call(card.querySelectorAll(".usage-window"), function (windowNode) {
        var progress = windowNode.querySelector("progress");
        var label = windowNode.querySelector(".usage-window-label strong");
        if (!progress) return;
        var value = Math.max(0, Math.min(100, (Number(progress.value) / (Number(progress.max) || 100)) * 100));
        if (tightest === null || value < tightest.value) {
          tightest = { value: value, label: label ? label.textContent.trim() : "" };
        }
      });
      var name = card.querySelector("h4");
      if (tightest && name) entries.push({ name: name.textContent.trim(), value: tightest.value, label: tightest.label });
    });
    if (!entries.length) return;

    var wrap = el("section", "so-viz so-card so-rings");
    wrap.setAttribute("aria-label", "Remaining allowance by provider, tightest limit");
    var radius = 36;
    var circumference = 2 * Math.PI * radius;
    var svgNS = "http://www.w3.org/2000/svg";
    entries.forEach(function (entry) {
      var item = el("div", "so-ring");
      item.setAttribute("data-tone", tone(entry.value));
      var figure = el("div", "so-ring-figure");
      var svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("viewBox", "0 0 84 84");
      svg.setAttribute("aria-hidden", "true");
      ["so-ring-track", "so-ring-value"].forEach(function (name) {
        var circle = document.createElementNS(svgNS, "circle");
        circle.setAttribute("class", name);
        circle.setAttribute("cx", "42");
        circle.setAttribute("cy", "42");
        circle.setAttribute("r", String(radius));
        if (name === "so-ring-value") {
          var target = circumference * (1 - entry.value / 100);
          circle.style.strokeDasharray = circumference.toFixed(2);
          circle.style.strokeDashoffset = reduceMotion ? target.toFixed(2) : circumference.toFixed(2);
          circle.style.setProperty("--so-dash-to", target.toFixed(2));
          if (entry.value <= 0) circle.style.visibility = "hidden";
        }
        svg.appendChild(circle);
      });
      figure.appendChild(svg);
      var numberNode = el("div", "so-ring-number");
      var inner = el("span");
      inner.appendChild(document.createTextNode(String(Math.round(entry.value))));
      inner.appendChild(el("small", "", "%"));
      numberNode.appendChild(inner);
      figure.appendChild(numberNode);
      item.appendChild(figure);
      item.appendChild(el("strong", "", entry.name));
      item.appendChild(el("small", "", entry.label ? entry.label + " · remaining" : "Remaining"));
      wrap.appendChild(item);
    });
    host.parentNode.insertBefore(wrap, host);
  }

  /* ------------------------------------------------------- proportion bars */
  function splitBar(title, note, parts, label) {
    var total = parts.reduce(function (sum, part) { return sum + part.value; }, 0);
    var card = el("section", "so-viz so-card so-split");
    card.setAttribute("aria-label", label);
    var titles = el("div");
    titles.appendChild(el("h3", "so-viz-title", title));
    if (note) titles.appendChild(el("p", "so-viz-note", note));
    card.appendChild(titles);
    var bar = el("div", "so-split-bar");
    parts.forEach(function (part) {
      if (!part.value) return;
      var segment = el("span");
      segment.tabIndex = 0;
      segment.style.setProperty("--v", part.value);
      segment.style.setProperty("--c", part.color);
      var share = total ? Math.round((part.value / total) * 100) + "%" : "";
      segment.setAttribute("aria-label", part.label + ": " + number(part.value) + (share ? " (" + share + ")" : ""));
      bindTip(segment, null, [{ color: part.color, value: number(part.value), label: part.label + (share ? " · " + share : "") }]);
      bar.appendChild(segment);
    });
    card.appendChild(bar);
    card.appendChild(legend(parts.map(function (part) {
      return { color: part.color, value: compact(part.value), label: part.label };
    })));
    return card;
  }

  function tokenMix() {
    var panel = document.querySelector('[data-panel="usage"]');
    if (!panel) return;
    var old = panel.querySelector(".so-split");
    var values = {};
    Array.prototype.forEach.call(panel.querySelectorAll(".so-kv dt"), function (dt) {
      var dd = dt.nextElementSibling;
      if (!dd) return;
      var raw = dd.textContent.trim().replace(/,/g, "");
      if (/^\d+$/.test(raw)) values[dt.textContent.trim().toLowerCase()] = Number(raw);
    });
    var input = values["known input tokens"];
    var output = values["known output tokens"];
    var cached = values["known cached tokens"];
    var known = values["known total tokens"];
    var parts;
    if (typeof input === "number" && typeof output === "number" && typeof cached === "number" &&
        typeof known === "number" && known === input + output && cached <= input) {
      /* Cached input is a subset of input (the known total is input + output), so
         split input into its uncached and cached parts: the segments sum to the total. */
      parts = [
        { label: "Input", value: input - cached, color: "var(--viz-s1)" },
        { label: "Cached input", value: cached, color: "var(--viz-s3)" },
        { label: "Output", value: output, color: "var(--viz-s2)" }
      ];
    } else if (typeof input === "number" && typeof output === "number" && typeof cached === "number" &&
               typeof known === "number" && known === input + output + cached) {
      parts = [
        { label: "Input", value: input, color: "var(--viz-s1)" },
        { label: "Output", value: output, color: "var(--viz-s2)" },
        { label: "Cached", value: cached, color: "var(--viz-s3)" }
      ];
    } else {
      /* Cached tokens cannot be placed without double counting; show what is certain. */
      parts = [
        { label: "Input", value: input, color: "var(--viz-s1)" },
        { label: "Output", value: output, color: "var(--viz-s2)" }
      ].filter(function (part) { return typeof part.value === "number"; });
    }
    var total = parts.reduce(function (sum, part) { return sum + part.value; }, 0);
    if (old) old.remove();
    if (parts.length < 2 || !total) return;
    var kv = panel.querySelector(".so-kv");
    if (!kv) return;
    kv.parentNode.insertBefore(
      splitBar("Token mix", "Provider-reported tokens only; calls with unknown usage are not counted.", parts, "Token mix for this run"),
      kv
    );
  }

  function diagnosticsHealth() {
    if (!/^\/diagnostics/.test(window.location.pathname)) return;
    var table = document.querySelector(".settings-section .so-table");
    if (!table || document.querySelector(".so-split")) return;
    var parts = [
      { label: "OK", value: table.querySelectorAll(".badge-pass").length, color: "var(--viz-good)" },
      { label: "Warnings", value: table.querySelectorAll(".badge-warning").length, color: "var(--viz-warn)" },
      { label: "Failures", value: table.querySelectorAll(".badge-fail").length, color: "var(--viz-crit)" }
    ];
    var total = parts.reduce(function (sum, part) { return sum + part.value; }, 0);
    if (total < 2) return;
    var wrap = table.closest(".so-table-wrap") || table;
    wrap.parentNode.insertBefore(splitBar("Readiness checks", total + " checks run when this page loaded", parts, "Readiness check results"), wrap);
  }

  /* ---------------------------------------------------------- theme fade */
  function themeFade() {
    if (reduceMotion || !window.MutationObserver) return;
    var root = document.documentElement;
    var timer = null;
    new MutationObserver(function () {
      root.classList.add("so-theme-fade");
      window.clearTimeout(timer);
      timer = window.setTimeout(function () { root.classList.remove("so-theme-fade"); }, 320);
    }).observe(root, { attributes: true, attributeFilter: ["data-theme", "data-palette"] });
  }

  /* Regions that other scripts re-render get their figures redrawn. */
  function watch(node, redraw) {
    if (!node || !window.MutationObserver) return;
    var queued = false;
    new MutationObserver(function (records) {
      var external = records.some(function (record) {
        return Array.prototype.some.call(record.addedNodes, function (added) {
          return !(added.nodeType === 1 && /(^|\s)so-(meter|split|rings)(\s|$)/.test(added.className || ""));
        });
      });
      if (!external || queued) return;
      queued = true;
      window.setTimeout(function () { queued = false; redraw(); }, 0);
    }).observe(node, { childList: true, subtree: true });
  }

  function start() {
    try {
      themeFade();
      runInsights();
      meters(document);
      usageRings();
      tokenMix();
      diagnosticsHealth();
      watch(document.getElementById("cli-usage-cards"), function () { meters(document); usageRings(); });
      watch(document.getElementById("quickUsageContent"), function () { meters(document); });
      watch(document.querySelector('[data-panel="usage"]'), tokenMix);
    } catch (error) {
      if (window.console && window.console.warn) window.console.warn("SaberOps visuals skipped:", error);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
