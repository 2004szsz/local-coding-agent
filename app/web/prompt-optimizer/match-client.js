/* ============================================================
 * 提示词模板匹配 Client：订阅后台 PromptMatchDaemon
 * 无 DOM；SSE 优先，断线降级 REST 轮询。
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  var optimizer = null;
  var es = null;
  var pollTimer = null;
  var lastSeq = -1;
  var currentStatus = null;
  var listeners = [];
  var POLL_MS = 30000;

  function notify(status) {
    listeners.forEach(function (fn) {
      try { fn(status); } catch (e) { /* ignore */ }
    });
  }

  function applyStatus(status) {
    if (!status) return;
    var seq = status.seq != null ? status.seq : lastSeq + 1;
    if (status.seq == null && currentStatus && currentStatus.model === status.model
        && currentStatus.family === status.family && currentStatus.status === status.status) {
      return;
    }
    if (status.seq != null && seq <= lastSeq) return;
    if (status.seq != null) lastSeq = seq;
    currentStatus = status;
    if (optimizer && typeof optimizer.applyMatchStatus === "function") {
      optimizer.applyMatchStatus(status);
    }
    notify(status);
  }

  function fetchStatus() {
    return fetch("/api/prompt-match/status", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.status) {
          var st = data.status;
          st.seq = data.seq;
          applyStatus(st);
        }
      })
      .catch(function () { /* 后台不可用：静默降级 */ });
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(fetchStatus, POLL_MS);
    fetchStatus();
  }

  function connectSSE() {
    if (typeof EventSource === "undefined") {
      startPoll();
      return;
    }
    if (es) return;
    es = new EventSource("/api/prompt-match/stream");
    es.onmessage = function (ev) {
      try {
        var data = JSON.parse(ev.data);
        if (data.type === "status" && data.status) {
          var st = data.status;
          st.seq = data.seq;
          applyStatus(st);
        }
      } catch (e) { /* ignore malformed frame */ }
    };
    es.onerror = function () {
      if (es) {
        es.close();
        es = null;
      }
      startPoll();
    };
  }

  function init(opt) {
    optimizer = opt || null;
    lastSeq = -1;
    connectSSE();
    fetchStatus();
  }

  function onStatus(fn) {
    if (typeof fn === "function") listeners.push(fn);
    if (currentStatus) fn(currentStatus);
  }

  function destroy() {
    if (es) {
      es.close();
      es = null;
    }
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    optimizer = null;
    listeners = [];
    lastSeq = -1;
    currentStatus = null;
  }

  PO.matchClient = {
    init: init,
    destroy: destroy,
    refresh: fetchStatus,
    onStatus: onStatus,
    getStatus: function () { return currentStatus; },
  };
})(typeof window !== "undefined" ? window : globalThis);
