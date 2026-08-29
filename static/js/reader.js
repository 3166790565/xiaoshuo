/* 阅读体验：字号 / 行距 / 主题 / 阅读位置，全部存 localStorage。
   书籍页复用同一份记录，用来显示"继续阅读"。 */
(function () {
  "use strict";

  var PREFS_KEY = "reader:prefs";
  var POS_PREFIX = "reader:pos:";
  var FONT = { min: 15, max: 26, step: 1 };
  var LEAD = { min: 1.5, max: 2.4, step: 0.1 };

  function readJSON(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) || fallback;
    } catch (err) {
      return fallback;
    }
  }

  function writeJSON(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (err) {
      /* 隐私模式下写不进去就算了，不影响阅读 */
    }
  }

  var prefs = Object.assign({ font: 18, lead: 1.9, theme: "paper" }, readJSON(PREFS_KEY, {}));

  function clamp(value, range) {
    return Math.min(range.max, Math.max(range.min, value));
  }

  function applyPrefs() {
    var root = document.documentElement;
    root.style.setProperty("--reader-font", prefs.font + "px");
    root.style.setProperty("--reader-lead", String(prefs.lead));
    root.setAttribute("data-theme", prefs.theme);
    document.querySelectorAll(".reader-bar [data-theme]").forEach(function (button) {
      button.classList.toggle("is-on", button.getAttribute("data-theme") === prefs.theme);
    });
  }

  function bindControls(reader) {
    reader.querySelectorAll("[data-font]").forEach(function (button) {
      button.addEventListener("click", function () {
        var delta = button.getAttribute("data-font") === "+" ? FONT.step : -FONT.step;
        prefs.font = clamp(prefs.font + delta, FONT);
        writeJSON(PREFS_KEY, prefs);
        applyPrefs();
      });
    });

    reader.querySelectorAll("[data-lead]").forEach(function (button) {
      button.addEventListener("click", function () {
        var delta = button.getAttribute("data-lead") === "+" ? LEAD.step : -LEAD.step;
        prefs.lead = Math.round(clamp(prefs.lead + delta, LEAD) * 10) / 10;
        writeJSON(PREFS_KEY, prefs);
        applyPrefs();
      });
    });

    reader.querySelectorAll("[data-theme]").forEach(function (button) {
      button.addEventListener("click", function () {
        prefs.theme = button.getAttribute("data-theme");
        writeJSON(PREFS_KEY, prefs);
        applyPrefs();
      });
    });
  }

  function bindKeyboard(reader) {
    document.addEventListener("keydown", function (event) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      var tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      var selector = event.key === "ArrowLeft" ? "[data-prev]" : event.key === "ArrowRight" ? "[data-next]" : "";
      if (!selector) return;
      var link = reader.querySelector(selector);
      if (link) {
        event.preventDefault();
        window.location.href = link.getAttribute("href");
      }
    });
  }

  function rememberPosition(reader) {
    var bookId = reader.getAttribute("data-book-id");
    writeJSON(POS_PREFIX + bookId, {
      idx: Number(reader.getAttribute("data-idx")),
      title: reader.getAttribute("data-chapter-title") || "",
      at: Date.now()
    });
  }

  function showResume(detail) {
    var bookId = detail.getAttribute("data-book-id");
    var saved = readJSON(POS_PREFIX + bookId, null);
    var link = detail.querySelector("[data-resume]");
    if (!link || !saved || !saved.idx) return;
    link.setAttribute("href", "/book/" + bookId + "/read/" + saved.idx);
    link.textContent = "继续读 " + (saved.title || "第 " + saved.idx + " 章");
    link.classList.remove("is-hidden");
  }

  applyPrefs();

  var reader = document.querySelector(".reader");
  if (reader) {
    bindControls(reader);
    bindKeyboard(reader);
    rememberPosition(reader);
  }

  var detail = document.querySelector(".book-detail");
  if (detail) showResume(detail);
})();
