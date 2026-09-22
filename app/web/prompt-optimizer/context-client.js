/* ============================================================
 * IDE 上下文 Client：向后台 Daemon 上报编辑器/终端状态
 * 仅绑定文件遮罩编辑器；无 UI、无弹窗。
 * ============================================================ */
(function (global) {
  "use strict";

  var DEBOUNCE_MS = 300;
  var timer = null;
  var pending = null;

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(function () { /* 上报失败不打扰用户 */ });
  }

  function textareaState(ta) {
    var text = ta.value.slice(0, ta.selectionStart);
    var lines = text.split(/\r?\n/);
    var line = lines.length;
    var col = (lines[lines.length - 1] || "").length + 1;
    var selection = "";
    if (ta.selectionStart !== ta.selectionEnd) {
      selection = ta.value.slice(ta.selectionStart, ta.selectionEnd);
    }
    return { line: line, col: col, selection: selection };
  }

  function reportEditor(path, ta) {
    if (!ta) return;
    var pos = textareaState(ta);
    postJSON("/api/context/editor", {
      path: path || "",
      content: ta.value,
      line: pos.line,
      col: pos.col,
      selection: pos.selection,
    });
  }

  function scheduleReport(path, ta) {
    pending = { path: path, ta: ta };
    clearTimeout(timer);
    timer = setTimeout(function () {
      if (pending) reportEditor(pending.path, pending.ta);
      pending = null;
    }, DEBOUNCE_MS);
  }

  function bindFileEditor() {
    var overlay = document.getElementById("file-overlay");
    var editor = document.getElementById("file-editor");
    if (!editor) return;

    function activePath() {
      return overlay && !overlay.hidden ? editor.dataset.ctxPath || "" : "";
    }

    function onChange() {
      var path = activePath();
      if (!path) return;
      scheduleReport(path, editor);
    }

    editor.addEventListener("input", onChange);
    editor.addEventListener("keyup", onChange);
    editor.addEventListener("select", onChange);
    editor.addEventListener("click", onChange);
  }

  function onFileOpened(path) {
    var editor = document.getElementById("file-editor");
    if (!editor) return;
    editor.dataset.ctxPath = path || "";
    reportEditor(path, editor);
  }

  function onFileSaved(path) {
    postJSON("/api/context/edit", { path: path || "", summary: "用户保存文件" });
  }

  function appendTerminalLine(line) {
    if (!line || !String(line).trim()) return;
    postJSON("/api/context/terminal", { line: String(line) });
  }

  function init() {
    bindFileEditor();
  }

  global.POContextClient = {
    init: init,
    onFileOpened: onFileOpened,
    onFileSaved: onFileSaved,
    appendTerminalLine: appendTerminalLine,
  };
})(typeof window !== "undefined" ? window : globalThis);
