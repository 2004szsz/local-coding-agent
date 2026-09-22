/* ============================================================
 * 模块协同前端：读取规则文件、渲染链路状态、可点击优化入口、
 * RAG 待索引状态由后端 Daemon 自动处理，前端仅轮询刷新展示。
 * ============================================================ */
(function (global) {
  "use strict";

  var deps = {};
  var moduleData = null;
  var rulesData = null;
  var pollTimer = null;
  var POLL_MS = 30000;

  function $(sel) { return document.querySelector(sel); }

  function findModule(id) {
    if (!moduleData || !moduleData.modules) return null;
    for (var i = 0; i < moduleData.modules.length; i++) {
      if (moduleData.modules[i].id === id) return moduleData.modules[i];
    }
    return null;
  }

  function statusClass(st) {
    if (st === "ready") return "ready";
    if (st === "pending" || st === "warn" || st === "indexing") return "warn";
    return "off";
  }

  function applyToDom() {
    if (!moduleData) return;
    var rag = findModule("rag");
    var access = findModule("access");
    var fs = findModule("fs");
    var agent = findModule("agent");

    if (fs) {
      var fsNode = $("#chain-fs");
      if (fsNode) fsNode.className = "chain-node " + statusClass(fs.status);
      var fsVal = $("#chain-fs-val");
      if (fsVal) fsVal.textContent = fs.value || "—";
    }
    if (rag) {
      var ragNode = $("#chain-rag");
      if (ragNode) ragNode.className = "chain-node " + statusClass(rag.status);
      var ragVal = $("#chain-rag-val");
      if (ragVal) {
        ragVal.textContent = rag.status === "indexing" ? "索引中…" : (rag.value || "—");
      }
      var ragEl = $("#cf-rag-status");
      var ragText = $("#cf-rag-text");
      if (ragText) {
        var label = rag.status === "ready"
          ? ((typeof deps.t === "function" ? deps.t("chain.ragReady") : "知识库已就绪") + " · " + (rag.value || ""))
          : (rag.detail || (typeof deps.t === "function" ? deps.t("chain.ragIdle") : "知识库待索引"));
        ragText.textContent = label;
      }
      if (ragEl) ragEl.className = "cf-item cf-status cf-clickable " + statusClass(rag.status);
    }
    if (agent) {
      var agentNode = $("#chain-agent");
      if (agentNode) agentNode.className = "chain-node " + statusClass(agent.status);
      var agentVal = $("#chain-agent-val");
      if (agentVal) agentVal.textContent = agent.value || "—";
    }
    if (access) {
      var accessEl = $("#cf-access-status");
      var accessText = $("#cf-access-text");
      if (accessText) accessText.textContent = access.value || "—";
      if (accessEl) accessEl.className = "cf-item cf-status cf-clickable " + statusClass(access.status);
    }
  }

  function navigateEntry(entry) {
    if (!entry || !deps) return;
    if (entry === "files" && deps.setSidebarPanel) deps.setSidebarPanel("groups");
    else if (entry === "rag-index" && deps.setSidebarPanel) deps.setSidebarPanel("groups");
    else if (entry === "subagents" && deps.showSettings) deps.showSettings("subagents");
    else if (entry === "skills" && deps.showSettings) deps.showSettings("skills");
  }

  function isMissingRoute(err) {
    var msg = String((err && err.message) || err || "");
    return msg === "Not Found" || /HTTP 404/.test(msg) || msg.indexOf("404") === 0;
  }

  function fallbackRagModule(rag) {
    var enabled = !!(rag && rag.enabled);
    var chunks = Number((rag && (rag.chunks != null ? rag.chunks : rag.count)) || 0);
    var ready = enabled && chunks > 0;
    return {
      id: "rag",
      label: "知识库",
      status: ready ? "ready" : (enabled ? "pending" : "off"),
      detail: ready ? "知识库已就绪" : (enabled ? "知识库待索引" : "RAG 未启用"),
      value: ready ? (chunks + " 块") : (enabled ? "待索引" : "未启用"),
      optimize_entry: "rag-index",
    };
  }

  function fallbackAgentModule(health) {
    var fw = String((health && health.framework) || "");
    var ready = fw.indexOf("state_loop") >= 0 || fw.indexOf("react") >= 0;
    return {
      id: "agent",
      label: "循环",
      status: ready ? "ready" : (fw ? "warn" : "off"),
      detail: fw ? ("框架 " + fw) : "Agent 未装配",
      value: fw.replace("state_loop", "循环").replace("native_react", "ReAct") || "—",
      optimize_entry: "subagents",
    };
  }

  function fallbackAccessModule(runtime) {
    var enabled = !!(runtime && runtime.local_access && runtime.local_access.enabled);
    var roots = (runtime && runtime.local_access && runtime.local_access.roots) || [];
    if (!enabled && roots && roots.length) enabled = true;
    return {
      id: "access",
      label: "本机访问",
      status: enabled ? "ready" : "warn",
      detail: enabled ? "本机访问已授权" : "本机访问受限",
      value: enabled ? "本机已授权" : "受限访问",
      optimize_entry: "skills",
    };
  }

  function fallbackFsModule(runtime) {
    var fsTools = (runtime && runtime.tools && runtime.tools.fs) || [];
    var wsTools = (runtime && runtime.tools && runtime.tools.workspace) || [];
    var count = fsTools.length + wsTools.length;
    var localOn = !!(runtime && runtime.local_access && runtime.local_access.enabled);
    if (!localOn && runtime && runtime.local_access && (runtime.local_access.roots || []).length) {
      localOn = true;
    }
    if (count > 0 && localOn) {
      return {
        id: "fs", label: "文件", status: "ready",
        detail: "文件工具已挂载", value: count + " 工具", optimize_entry: "files",
      };
    }
    if (count > 0) {
      return {
        id: "fs", label: "文件", status: "warn",
        detail: "工作区工具就绪，本机访问待授权", value: "待授权", optimize_entry: "files",
      };
    }
    return {
      id: "fs", label: "文件", status: "off",
      detail: "未挂载文件工具", value: "未挂载", optimize_entry: "files",
    };
  }

  async function refreshFromCanonicalApis() {
    var rag = { enabled: false, chunks: 0 };
    var health = null;
    var runtime = null;
    try { rag = await deps.api("/api/rag/status"); } catch (_) { rag = { enabled: false, chunks: 0 }; }
    try { health = await deps.api("/api/health"); } catch (_) { health = null; }
    try { runtime = await deps.api("/api/runtime"); } catch (_) { runtime = null; }
    moduleData = {
      modules: [
        fallbackFsModule(runtime),
        fallbackRagModule(rag),
        fallbackAgentModule(health),
        fallbackAccessModule(runtime),
      ],
    };
    applyToDom();
  }

  async function adaptAccessViaRuntime() {
    var runtime = await deps.api("/api/runtime");
    var enabled = !!(runtime && runtime.local_access && runtime.local_access.enabled);
    var roots = (runtime && runtime.local_access && runtime.local_access.roots) || [];
    if (!enabled && roots.length) enabled = true;
    var gate = { fs_read: "L0", fs_write: "L4" };
    var detail = enabled ? "本机访问已授权" : "本机访问受限";
    var mode = (runtime && runtime.exec_mode) || "";
    var writePolicy = mode === "full_access" ? "自动（full_access）" : "需确认";
    return {
      ok: enabled,
      module: "access",
      action: "verify_access",
      status: enabled ? "ready" : "warn",
      gate: gate,
      message: enabled
        ? (detail + (mode ? "｜执行档 " + mode : "") +
           "｜闸门 fs_read=L0（自动）/fs_write=L4（" + writePolicy + "）")
        : detail,
      optimize_entry: "skills",
    };
  }

  async function adaptModule(id, silent) {
    if (!deps.api) return null;
    try {
      var result;
      try {
        result = await deps.api("/api/modules/adapt/" + encodeURIComponent(id), { method: "POST" });
      } catch (err) {
        if (id === "rag" && isMissingRoute(err)) {
          var indexed = await deps.api("/api/rag/index", { method: "POST" });
          result = {
            ok: indexed.ok !== false,
            module: "rag",
            action: "incremental_index",
            stats: indexed,
            message: indexed.message || (
              "索引完成：扫描 " + (indexed.scanned || 0) +
              "，新增 " + (indexed.added || 0) +
              "，共 " + (indexed.chunks_total || indexed.chunks || 0) + " 切片"
            ),
            optimize_entry: "rag-index",
          };
        } else if (id === "access" && isMissingRoute(err)) {
          result = await adaptAccessViaRuntime();
        } else {
          throw err;
        }
      }
      if (!silent && deps.toast) {
        deps.toast(result.message || (result.ok ? "模块已适配" : "适配失败"), result.ok ? "ok" : "error");
      }
      if (id === "rag" && deps.refreshChainStatus) await deps.refreshChainStatus();
      await refresh();
      if (result.optimize_entry) navigateEntry(result.optimize_entry);
      return result;
    } catch (err) {
      if (!silent && deps.toast) deps.toast("模块适配失败: " + err.message, "error");
      return null;
    }
  }

  async function refresh() {
    if (!deps.api) return;
    try {
      var data = await deps.api("/api/modules/status");
      moduleData = data;
      applyToDom();
    } catch (err) {
      if (isMissingRoute(err)) {
        try { await refreshFromCanonicalApis(); } catch (_) { /* 静默降级 */ }
      }
    }
  }

  async function loadRules() {
    if (!deps.api) return;
    try {
      var data = await deps.api("/api/modules/rules");
      rulesData = data;
      global.ModuleRules = {
        version: data.version,
        getContextSnippet: function () { return data.context_snippet || ""; },
        getModule: function (id) { return (data.modules || {})[id] || null; },
        getAll: function () { return data.modules || {}; },
      };
      if (global.PO && global.PO.context) {
        global.PO.context.moduleRulesSnippet = data.context_snippet || "";
      }
    } catch (_) { /* 规则文件不可用时不阻断主流程 */ }
  }

  function handleOptimizeClick(moduleId) {
    var mod = findModule(moduleId);
    var entry = mod && mod.optimize_entry;
    adaptModule(moduleId, false).then(function () {
      if (entry) navigateEntry(entry);
    });
  }

  function bindClicks() {
    var map = {
      "chain-fs": "fs",
      "chain-rag": "rag",
      "chain-agent": "agent",
      "cf-rag-status": "rag",
      "cf-access-status": "access",
    };
    Object.keys(map).forEach(function (elId) {
      var el = document.getElementById(elId);
      if (!el || el.dataset.chainBound) return;
      el.dataset.chainBound = "1";
      el.addEventListener("click", function (e) {
        if (elId === "project-switch") return;
        e.preventDefault();
        handleOptimizeClick(map[elId]);
      });
    });
  }

  function init(options) {
    deps = options || {};
    if (deps.pollMs) POLL_MS = deps.pollMs;
    bindClicks();
    loadRules();
    refresh();
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refresh, POLL_MS);
  }

  function destroy() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  global.ChainModules = {
    init: init,
    refresh: refresh,
    adapt: adaptModule,
    destroy: destroy,
    getRules: function () { return rulesData; },
    getStatus: function () { return moduleData; },
  };
})(typeof window !== "undefined" ? window : globalThis);
