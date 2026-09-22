/* ============================================================
 * IDE 上下文 Bridge：订阅后台 Daemon SSE，同步至 ContextCapture
 * 无 DOM、无弹窗；断线自动降级为 REST 轮询。
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  var capture = null;
  var es = null;
  var lastSeq = -1;
  var pollTimer = null;
  var POLL_MS = 30000;

  function applySnapshot(snap) {
    if (!capture || !snap || !PO.context || !PO.context.applyRemoteSnapshot) return;
    var seq = snap.seq != null ? snap.seq : lastSeq + 1;
    if (seq <= lastSeq) return;
    lastSeq = seq;
    PO.context.applyRemoteSnapshot(capture, snap);
  }

  function fetchSnapshot() {
    if (!capture) return Promise.resolve();
    return fetch("/api/context/snapshot", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.snapshot) applySnapshot(data.snapshot);
      })
      .catch(function () { /* 后台不可用：静默降级 */ });
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(fetchSnapshot, POLL_MS);
    fetchSnapshot();
  }

  function connectSSE() {
    if (typeof EventSource === "undefined") {
      startPoll();
      return;
    }
    if (es) return;
    es = new EventSource("/api/context/stream");
    es.onmessage = function (ev) {
      try {
        var data = JSON.parse(ev.data);
        if (data.type === "snapshot" && data.snapshot) {
          applySnapshot(data.snapshot);
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

  function init(cap) {
    capture = cap;
    lastSeq = -1;
    connectSSE();
    fetchSnapshot();
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
    capture = null;
    lastSeq = -1;
  }

  PO.contextBridge = {
    init: init,
    destroy: destroy,
    refresh: fetchSnapshot,
  };
})(typeof window !== "undefined" ? window : globalThis);
