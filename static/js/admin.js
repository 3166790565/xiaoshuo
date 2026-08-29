/* 后台交互：拖拽上传 + 任务进度轮询 + 批量重新分章。 */
(function () {
  "use strict";

  var MODE_LABEL = { marked: "标记分章", single: "整本单章", auto: "按字数分节" };
  var STATUS_LABEL = { ok: "成功", skip: "跳过", error: "失败" };
  var POLL_MS = 800;

  function escapeText(value) {
    var node = document.createElement("span");
    node.textContent = value == null ? "" : String(value);
    return node.innerHTML;
  }

  function pollJob(jobId, onTick) {
    var timer = setInterval(function () {
      fetch("/admin/api/jobs/" + encodeURIComponent(jobId), { credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) throw new Error("读取任务失败（" + response.status + "）");
          return response.json();
        })
        .then(function (job) {
          onTick(job, null);
          if (job.status === "done" || job.status === "error") clearInterval(timer);
        })
        .catch(function (error) {
          clearInterval(timer);
          onTick(null, error);
        });
    }, POLL_MS);
  }

  /* ------------------------------------------------------------ 上传页 */

  function setupUpload() {
    var form = document.getElementById("upload-form");
    if (!form) return;

    var input = document.getElementById("file-input");
    var zone = document.getElementById("dropzone");
    var list = document.getElementById("file-list");
    var button = document.getElementById("upload-btn");
    var note = document.getElementById("upload-note");
    var picked = [];

    function render() {
      list.innerHTML = picked
        .map(function (file) {
          return "<li><span>" + escapeText(file.name) + "</span><span>" +
            (file.size / 1024).toFixed(0) + " KB</span></li>";
        })
        .join("");
      button.disabled = picked.length === 0;
      note.textContent = picked.length ? "已选 " + picked.length + " 个文件" : "";
    }

    function accept(files) {
      Array.prototype.forEach.call(files, function (file) {
        var name = file.name.toLowerCase();
        if (name.endsWith(".txt") || name.endsWith(".zip")) picked.push(file);
      });
      render();
    }

    zone.addEventListener("click", function () { input.click(); });
    zone.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); }
    });
    input.addEventListener("change", function () { accept(input.files); input.value = ""; });

    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.remove("is-over");
      });
    });
    zone.addEventListener("drop", function (event) {
      if (event.dataTransfer && event.dataTransfer.files) accept(event.dataTransfer.files);
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!picked.length) return;
      var payload = new FormData();
      picked.forEach(function (file) { payload.append("files", file, file.name); });
      payload.append("target_chars", document.getElementById("target_chars").value || "0");
      var mode = form.querySelector('input[name="force_mode"]:checked');
      payload.append("force_mode", mode ? mode.value : "");

      button.disabled = true;
      note.textContent = "上传中…";

      fetch("/admin/upload", { method: "POST", body: payload, credentials: "same-origin" })
        .then(function (response) {
          return response.json().catch(function () {
            throw new Error("上传失败（" + response.status + "），可能需要重新登录");
          });
        })
        .then(function (data) {
          if (!data.ok) throw new Error(data.message || "上传失败");
          note.textContent = "已入队，正在解析…";
          watch(data.job_id);
        })
        .catch(function (error) {
          note.textContent = error.message;
          button.disabled = false;
        });
    });

    function watch(jobId) {
      var card = document.getElementById("progress-card");
      var bar = document.getElementById("progress-bar");
      var text = document.getElementById("progress-text");
      var status = document.getElementById("progress-status");
      var rows = document.getElementById("progress-rows");
      card.classList.remove("is-hidden");

      pollJob(jobId, function (job, error) {
        if (error) { status.textContent = error.message; return; }
        var percent = job.total ? Math.round((job.done / job.total) * 100) : 0;
        bar.style.width = percent + "%";
        text.textContent = job.done + " / " + job.total + " · 成功 " + job.ok + " · 失败 " + job.failed;
        status.textContent = job.status === "done" ? "完成" : job.status === "error" ? "出错" : "进行中";
        rows.innerHTML = job.log.map(function (row) {
          return "<tr class=\"row-" + row.status + "\">" +
            "<td>" + escapeText(row.file) + "</td>" +
            "<td>" + (STATUS_LABEL[row.status] || row.status) + "</td>" +
            "<td>" + escapeText(row.title || "") + "</td>" +
            "<td>" + (row.mode ? (MODE_LABEL[row.mode] || row.mode) : "") + "</td>" +
            "<td>" + (row.chapters || "") + "</td>" +
            "<td>" + (row.words || "") + "</td>" +
            "<td>" + escapeText(row.message || "") + "</td>" +
            "</tr>";
        }).join("");
        if (job.status === "done") {
          note.textContent = "导入完成，共成功 " + job.ok + " 本";
          button.disabled = picked.length === 0;
        }
      });
    }

    if (window.RESUME_JOB) watch(window.RESUME_JOB);
    render();
  }

  /* ------------------------------------------------------------ 批量重新分章 */

  function setupResplit() {
    var selectedBtn = document.getElementById("rs-selected");
    if (!selectedBtn) return;

    var filteredBtn = document.getElementById("rs-filtered");
    var box = document.getElementById("rs-progress");
    var bar = document.getElementById("rs-bar");
    var text = document.getElementById("rs-text");
    var checkAll = document.getElementById("check-all");

    if (checkAll) {
      checkAll.addEventListener("change", function () {
        document.querySelectorAll(".row-check").forEach(function (item) {
          item.checked = checkAll.checked;
        });
      });
    }

    function send(ids) {
      var payload = new FormData();
      payload.append("ids", ids.join(","));
      payload.append("split_mode", document.getElementById("rs-filter-mode").value);
      payload.append("q", document.getElementById("rs-filter-q").value);
      payload.append("force_mode", document.getElementById("rs-mode").value);
      payload.append("target_chars", document.getElementById("rs-target").value || "0");

      box.classList.remove("is-hidden");
      text.textContent = "提交中…";

      fetch("/admin/books/resplit", { method: "POST", body: payload, credentials: "same-origin" })
        .then(function (response) {
          return response.json().catch(function () {
            throw new Error("提交失败（" + response.status + "）");
          });
        })
        .then(function (data) {
          if (!data.ok) throw new Error(data.message || "提交失败");
          text.textContent = "共 " + data.books + " 本，开始重跑…";
          pollJob(data.job_id, function (job, error) {
            if (error) { text.textContent = error.message; return; }
            var percent = job.total ? Math.round((job.done / job.total) * 100) : 0;
            bar.style.width = percent + "%";
            text.textContent = job.done + " / " + job.total + " · 成功 " + job.ok + " · 失败 " + job.failed;
            if (job.status === "done") {
              text.textContent += " · 刷新页面看新结果";
              setTimeout(function () { window.location.reload(); }, 1200);
            }
          });
        })
        .catch(function (error) { text.textContent = error.message; });
    }

    selectedBtn.addEventListener("click", function () {
      var ids = Array.prototype.map.call(
        document.querySelectorAll(".row-check:checked"),
        function (item) { return item.value; }
      );
      if (!ids.length) { alert("先勾选要重跑的书"); return; }
      if (!confirm("对选中的 " + ids.length + " 本重新分章？原有章节会被替换。")) return;
      send(ids);
    });

    filteredBtn.addEventListener("click", function () {
      if (!confirm("对当前筛选条件下的全部书籍重新分章？原有章节会被替换。")) return;
      send([]);
    });
  }

  setupUpload();
  setupResplit();
})();
