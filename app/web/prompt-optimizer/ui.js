/* ============================================================
 * 提示词优化系统 · 前端绑定层
 * ------------------------------------------------------------
 *  默认静默模式：模板匹配 / 上下文采集 / 三层优化在后台自动运行，
 *  聊天页不注入任何工具条或预览 UI。完整调试界面仅在测试台
 *  （tests.html 设置 PO_UI_VISIBLE=true）中启用。
 *  - 发送时通过 window.__promptOptimize(text) 钩子完成三层转换
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO;
  if (!PO || !PO.createOptimizer) return;

  var LS_UI = "po-ui-settings-v1";
  var LS_CTX = "po-context-state-v1";
  var LS_AGENT = "po-agent-config-v1";
  var UI_VISIBLE = global.PO_UI_VISIBLE === true;
  var DEBUG_CTX_MODAL = global.PO_DEBUG_CONTEXT_MODAL === true;
  var lastMatchFamily = "";

  var optimizer = null;
  var lastResult = null;
  var previewTimer = null;

  function $(sel, root) { return (root || document).querySelector(sel); }

  function toast(msg, type) {
    if (typeof global.toast === "function") return global.toast(msg, type);
    var t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast show " + (type || "");
    t.hidden = false;
    setTimeout(function () { t.hidden = true; }, 2400);
  }

  function loadJSON(key, fallback) {
    try { var raw = localStorage.getItem(key); return raw ? JSON.parse(raw) : fallback; }
    catch (e) { return fallback; }
  }
  function saveJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 配额满：忽略 */ }
  }

  function el(tag, className, html) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (html != null) node.innerHTML = html;
    return node;
  }

  function closeModals() {
    if (els.ctxModal) els.ctxModal.hidden = true;
    if (els.agentModal) els.agentModal.hidden = true;
  }

  /* ============================================================
   * 初始化
   * ============================================================ */
  function init() {
    var wrap = $(".composer-wrap");
    var input = $("#input-box");
    if (!wrap || !input) return;

    optimizer = PO.createOptimizer({});
    var settings = loadJSON(LS_UI, { enabled: true });

    if (UI_VISIBLE) {
      if (DEBUG_CTX_MODAL) {
        var ctxState = loadJSON(LS_CTX, null);
        if (ctxState) applyCaptureState(ctxState);
      }
      buildBar(wrap, settings);
      buildChips(wrap);
      buildPreview(wrap);
      if (DEBUG_CTX_MODAL) buildContextModal();
      buildAgentModal();
      bindInput(input);
      updateEnabledUI();
    } else {
      bindSilentHook(settings);
      document.body.classList.add("po-enabled");
    }

    if (PO.contextBridge) PO.contextBridge.init(optimizer.capture);
    if (global.POContextClient) global.POContextClient.init();
    if (PO.matchClient) {
      PO.matchClient.init(optimizer);
      if (UI_VISIBLE) PO.matchClient.onStatus(renderMatchStatus);
    }
  }

  var els = {};

  /* ============================================================
   * 顶部工具条
   * ============================================================ */
  function buildBar(wrap, settings) {
    var bar = el("div", "po-bar");

    var sw = el("label", "po-switch");
    sw.innerHTML = '<input type="checkbox" ' + (settings.enabled ? "checked" : "") + ' /><span class="po-switch-track"></span><span class="po-switch-text">✨ 提示词优化</span>';
    els.toggle = $("input", sw);

    var matchHint = el("span", "po-match-bg-hint po-match-pending", "🎯 模板后台匹配中…");
    matchHint.title = "提示词模板由后台 Daemon 根据 Agent 当前模型自动匹配，无需手动选择";
    els.matchHint = matchHint;
    els.modelLabel = el("span", "po-model-label", "");

    var ctxHint = el("span", "po-ctx-bg-hint", "🧩 上下文后台采集中");
    ctxHint.title = "IDE 上下文由后台 Daemon 自动采集，无需手动配置";
    els.ctxHint = ctxHint;

    var btnAgent = el("button", "po-mini-btn", "🤖 智能体");
    var btnPreview = el("button", "po-mini-btn", "👁 预览优化结果");
    var linkTest = el("a", "po-mini-btn po-test-link", "🧪 测试台");
    linkTest.href = "/static/tests.html";
    linkTest.target = "_blank";

    bar.appendChild(sw);
    bar.appendChild(matchHint);
    if (DEBUG_CTX_MODAL) {
      var btnCtx = el("button", "po-mini-btn", "🧩 上下文");
      bar.appendChild(btnCtx);
      btnCtx.addEventListener("click", openContextModal);
    } else {
      bar.appendChild(ctxHint);
    }
    bar.appendChild(btnAgent);
    bar.appendChild(btnPreview);
    bar.appendChild(linkTest);
    bar.appendChild(els.modelLabel);
    wrap.insertBefore(bar, wrap.firstChild);

    els.toggle.addEventListener("change", function () {
      saveJSON(LS_UI, { enabled: els.toggle.checked });
      updateEnabledUI();
      toast(els.toggle.checked ? "✨ 提示词优化已开启（三层架构生效）" : "提示词优化已关闭，发送原始输入");
    });
    btnAgent.addEventListener("click", openAgentModal);
    btnPreview.addEventListener("click", function () {
      var text = $("#input-box").value;
      if (!text.trim()) { toast("请先在输入框中填写需求～"); return; }
      runFullOptimize(text, true);
    });
  }

  function renderMatchStatus(status) {
    if (!els.matchHint || !els.modelLabel) return;
    var text = status.matched_text || status.message || "模板后台匹配中…";
    var matched = status.status === "matched";
    els.matchHint.textContent = matched ? "🎯 模板已自动匹配" : "🎯 模板后台匹配中…";
    els.matchHint.className = "po-match-bg-hint " + (matched ? "po-match-ready" : "po-match-pending");
    if (els.toggle && els.toggle.checked) {
      els.modelLabel.textContent = text;
    }
    if (matched && status.family && status.family !== lastMatchFamily) {
      lastMatchFamily = status.family;
      if (els.toggle && els.toggle.checked) {
        toast("🎯 已自动匹配 " + (status.label || status.family) + " 模板");
      }
    }
  }

  function updateEnabledUI() {
    var on = els.toggle.checked;
    document.body.classList.toggle("po-enabled", on);
    if (on) {
      var st = PO.matchClient && PO.matchClient.getStatus();
      if (st) renderMatchStatus(st);
      else if (els.modelLabel) els.modelLabel.textContent = "模板后台匹配中…";
    } else {
      if (els.modelLabel) els.modelLabel.textContent = "";
    }
    if (!on) { els.chips.innerHTML = ""; els.preview.hidden = true; }
  }

  /* ============================================================
   * 实时语义 chips
   * ============================================================ */
  function buildChips(wrap) {
    els.chips = el("div", "po-chips");
    var hint = $(".composer-hint", wrap);
    wrap.insertBefore(els.chips, hint);
  }

  function chip(text, cls, title) {
    var c = el("span", "po-chip " + cls, text);
    if (title) c.title = title;
    return c;
  }

  function renderChips(analysis) {
    els.chips.innerHTML = "";
    if (!analysis) return;
    if (analysis.intent) {
      els.chips.appendChild(chip("🎯 意图：" + analysis.intent.label + " (" + analysis.intent.confidence + ")", "po-chip-intent"));
    }
    analysis.commands.forEach(function (c) {
      els.chips.appendChild(chip("⚡ /" + c, "po-chip-cmd"));
    });
    analysis.unknownCommands.forEach(function (c) {
      els.chips.appendChild(chip("⚠ 未知指令 /" + c, "po-chip-warn"));
    });
    analysis.directives.forEach(function (d) {
      els.chips.appendChild(chip("#" + d.type + " " + d.target, "po-chip-dir"));
    });
    analysis.corrections.forEach(function (c) {
      els.chips.appendChild(chip("✓ " + c.from + "→" + c.to, "po-chip-fix", c.message));
    });
    analysis.ambiguities.forEach(function (a) {
      els.chips.appendChild(chip((a.level === "info" ? "💡 " : "⚠ ") + a.message, a.level === "info" ? "po-chip-info" : "po-chip-warn", "建议：" + a.suggestion));
    });
    if (analysis.missingConstraints.length) {
      els.chips.appendChild(chip("🛡 自动补全 " + analysis.missingConstraints.length + " 条质量约束", "po-chip-fix",
        analysis.missingConstraints.join("\n")));
    }
    if (analysis.techHints.length) {
      els.chips.appendChild(chip("🧰 技术线索：" + analysis.techHints.join("、"), "po-chip-info"));
    }
    els.chips.appendChild(chip("≈" + analysis.estimatedTokens + " tokens（原始输入）", "po-chip-token"));
  }

  /* ============================================================
   * 优化结果预览面板
   * ============================================================ */
  function buildPreview(wrap) {
    els.preview = el("div", "po-preview");
    els.preview.hidden = true;
    els.preview.innerHTML =
      '<div class="po-preview-head">' +
      '  <div class="po-preview-title">✨ 工程化指令预览（三层优化结果）</div>' +
      '  <div class="po-preview-stats" id="po-stats"></div>' +
      '  <div class="po-preview-actions">' +
      '    <button class="po-mini-btn" id="po-copy-sys">复制系统提示词</button>' +
      '    <button class="po-mini-btn" id="po-copy-user">复制用户指令</button>' +
      '    <button class="po-mini-btn" id="po-apply">写回输入框</button>' +
      '    <button class="po-mini-btn po-close" id="po-preview-close">收起 ✕</button>' +
      '  </div>' +
      '</div>' +
      '<div class="po-preview-body">' +
      '  <details open><summary>① 系统提示词（底层模型模板 + 智能体专项，静默生效）</summary><pre id="po-pre-sys"></pre></details>' +
      '  <details open><summary>② 发送给模型的用户指令（IDE 上下文 + 结构化任务）</summary><pre id="po-pre-user"></pre></details>' +
      '  <div class="po-preview-meta" id="po-meta"></div>' +
      '</div>';
    wrap.insertBefore(els.preview, wrap.firstChild);

    $("#po-preview-close", els.preview).addEventListener("click", function () { els.preview.hidden = true; });
    $("#po-copy-sys", els.preview).addEventListener("click", function () { copyText($("#po-pre-sys", els.preview).textContent, "系统提示词"); });
    $("#po-copy-user", els.preview).addEventListener("click", function () { copyText($("#po-pre-user", els.preview).textContent, "用户指令"); });
    $("#po-apply", els.preview).addEventListener("click", function () {
      if (!lastResult) return;
      var input = $("#input-box");
      input.value = lastResult.userPrompt;
      input.dispatchEvent(new Event("input"));
      toast("已写回输入框，可直接发送或继续编辑");
    });
  }

  function copyText(text, label) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast(label + "已复制 ✓"); }, function () { fallbackCopy(text, label); });
    } else fallbackCopy(text, label);
  }
  function fallbackCopy(text, label) {
    var ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast(label + "已复制 ✓"); } catch (e) { toast("复制失败，请手动选择", "error"); }
    document.body.removeChild(ta);
  }

  function renderPreview(r) {
    if (!els.preview) return;
    $("#po-stats", els.preview).textContent =
      r.modelLabel + " · temp=" + r.hyperparams.temperature + " / top_p=" + r.hyperparams.top_p +
      " · 系统 " + r.tokens.system + " + 用户 " + r.tokens.user + " = " + r.tokens.total + " tokens";
    $("#po-pre-sys", els.preview).textContent = r.systemPrompt;
    $("#po-pre-user", els.preview).textContent = r.userPrompt;

    var meta = [];
    if (r.context.sections.length) {
      meta.push("<div><b>已注入上下文（" + r.context.sections.length + " 段，≈" + r.context.estimatedTokens + " tokens / 预算 " + r.context.budgetTokens + "）：</b>" +
        r.context.sections.map(function (s) { return '<span class="po-meta-tag">' + s.title.split("（")[0] + " · " + s.estimatedTokens + "t</span>"; }).join("") + "</div>");
    }
    if (r.context.dropped.length) meta.push("<div class='po-meta-warn'>⏬ 已省略：" + r.context.dropped.join("；") + "</div>");
    if (r.context.warnings.length) meta.push("<div class='po-meta-warn'>⚠ " + r.context.warnings.join("；") + "</div>");
    if (r.agent.mcpServers) meta.push("<div>🔌 MCP 服务 " + r.agent.mcpServers + " 个 · 🛠 工具 " + r.agent.toolCount + " 个" + (r.agent.styleDetected ? " · 🎨 已注入项目代码风格" : "") + "</div>");
    if (r.agent.mcpErrors.length) meta.push("<div class='po-meta-warn'>MCP 告警：" + r.agent.mcpErrors.join("；") + "</div>");
    $("#po-meta", els.preview).innerHTML = meta.join("");
    els.preview.hidden = false;
  }

  /* ============================================================
   * 静默模式：仅注册发送钩子，不挂载任何可见 UI
   * ============================================================ */
  function bindSilentHook(settings) {
    var enabled = settings.enabled !== false;

    global.__promptOptimize = function (text) {
      if (!enabled || !text || !text.trim()) return Promise.resolve(text);
      return runFullOptimize(text, false).then(function (r) { return r.userPrompt; })
        .catch(function () { return text; });
    };
  }

  /* ============================================================
   * 输入框实时增强 + 发送钩子（可见 UI 模式）
   * ============================================================ */
  function bindInput(input) {
    input.addEventListener("input", function () {
      if (!els.toggle.checked) { els.chips.innerHTML = ""; return; }
      var analysis;
      try { analysis = optimizer.analyzeLive(input.value); } catch (e) { return; }
      renderChips(analysis);
      if (!els.preview.hidden) {
        clearTimeout(previewTimer);
        var text = input.value;
        previewTimer = setTimeout(function () { runFullOptimize(text, false); }, 400);
      }
    });

    /* app.js 两处发送点统一调用本钩子；任何异常都回退原始输入，绝不阻塞发送 */
    global.__promptOptimize = function (text) {
      if (!els.toggle.checked || !text || !text.trim()) return Promise.resolve(text);
      return runFullOptimize(text, false).then(function (r) {
        toast("✨ 已注入 " + r.context.sections.length + " 段上下文 · " + r.tokens.total + " tokens · " + r.modelLabel);
        return r.userPrompt;
      }).catch(function () { return text; });
    };
  }

  function runFullOptimize(text, openPreview) {
    var cfg = loadJSON(LS_AGENT, {});
    var styleSources = [];
    if (cfg.styleSourcesText) {
      try { styleSources = JSON.parse(cfg.styleSourcesText); } catch (e) { styleSources = []; }
    }
    return optimizer.optimize(text, {
      mcpConfigText: cfg.mcpConfigText || "",
      toolDefsText: cfg.toolDefsText || "",
      styleSources: styleSources,
    }).then(function (r) {
      lastResult = r;
      renderPreview(r);
      if (openPreview) els.preview.hidden = false;
      return r;
    }).catch(function (err) {
      toast("优化失败：" + (err.message || err) + "（将发送原始输入）", "error");
      throw err;
    });
  }

  /* ============================================================
   * IDE 上下文模拟器弹窗（8 类上下文手动维护）
   * ============================================================ */
  function buildContextModal() {
    var m = el("div", "po-modal-mask");
    m.innerHTML =
      '<div class="po-modal po-modal-context">' +
      '  <h3>🧩 IDE 上下文模拟器（实时捕获的 8 类上下文）</h3>' +
      '  <div class="po-form-row"><label>① 活动文件路径</label><input id="po-ctx-path" placeholder="src/index.js" /></div>' +
      '  <div class="po-form-row po-inline"><label>光标行</label><input id="po-ctx-line" type="number" value="1" /><label>列</label><input id="po-ctx-col" type="number" value="1" /></div>' +
      '  <div class="po-form-row"><label>① 活动文件完整内容</label><textarea id="po-ctx-content" rows="8" placeholder="粘贴当前编辑器中的完整文件内容…"></textarea></div>' +
      '  <div class="po-form-row"><label>③ 用户选中片段</label><textarea id="po-ctx-selection" rows="3"></textarea></div>' +
      '  <div class="po-form-row"><label>④ 技术栈识别（清单文件 JSON，留空则点“自动识别”走后端工作区）</label>' +
      '    <textarea id="po-ctx-manifests" rows="3" placeholder=\'{"package.json":"{\\"dependencies\\":{\\"react\\":\\"^18\\"}}"}\'></textarea>' +
      '    <div class="po-modal-actions"><button class="po-mini-btn" id="po-ctx-detect">🔍 从工作区自动识别</button><span id="po-ctx-stack" class="po-stack-out"></span></div></div>' +
      '  <div class="po-form-row"><label>⑤ 最近编辑（最多 5 条，每行格式：路径 | 说明）</label><textarea id="po-ctx-edits" rows="3" placeholder="src/a.js | 新增登录校验函数"></textarea></div>' +
      '  <div class="po-form-row"><label>⑥ Linter 实时报错 JSON 数组</label><textarea id="po-ctx-linter" rows="3" placeholder=\'[{"file":"src/a.js","line":12,"severity":"error","message":"x is not defined","rule":"no-undef"}]\'></textarea></div>' +
      '  <div class="po-form-row"><label>⑦ 终端最新输出（自动截取最后 10 行）</label><textarea id="po-ctx-terminal" rows="3"></textarea></div>' +
      '  <div class="po-modal-actions po-foot">' +
      '    <button class="po-mini-btn" id="po-ctx-demo">填充示例数据</button>' +
      '    <button class="po-primary-btn" id="po-ctx-save">保存并注入捕获器</button>' +
      '    <button class="po-mini-btn" id="po-ctx-cancel">取消</button></div>' +
      '</div>';
    document.body.appendChild(m);
    m.hidden = true;
    els.ctxModal = m;
    m.addEventListener("click", function (e) { if (e.target === m) m.hidden = true; });
    $("#po-ctx-cancel", m).addEventListener("click", function () { m.hidden = true; });
    $("#po-ctx-save", m).addEventListener("click", saveContextForm);
    $("#po-ctx-detect", m).addEventListener("click", function () {
      $("#po-ctx-stack", m).textContent = "识别中…";
      optimizer.capture.detectTechStack().then(function (stack) {
        $("#po-ctx-stack", m).textContent = PO.context.formatTechStack(stack);
        toast("技术栈自动识别完成");
      });
    });
    $("#po-ctx-demo", m).addEventListener("click", fillContextDemo);
  }

  function openContextModal() {
    closeModals();
    var s = loadJSON(LS_CTX, {});
    $("#po-ctx-path").value = s.path || "";
    $("#po-ctx-line").value = s.line || 1;
    $("#po-ctx-col").value = s.col || 1;
    $("#po-ctx-content").value = s.content || "";
    $("#po-ctx-selection").value = s.selection || "";
    $("#po-ctx-manifests").value = s.manifests || "";
    $("#po-ctx-edits").value = s.edits || "";
    $("#po-ctx-linter").value = s.linter || "";
    $("#po-ctx-terminal").value = s.terminal || "";
    $("#po-ctx-stack").textContent = s.stackText || "";
    els.ctxModal.hidden = false;
  }

  function saveContextForm() {
    var m = els.ctxModal;
    var data = {
      path: $("#po-ctx-path", m).value.trim(),
      line: parseInt($("#po-ctx-line", m).value, 10) || 1,
      col: parseInt($("#po-ctx-col", m).value, 10) || 1,
      content: $("#po-ctx-content", m).value,
      selection: $("#po-ctx-selection", m).value,
      manifests: $("#po-ctx-manifests", m).value.trim(),
      edits: $("#po-ctx-edits", m).value,
      linter: $("#po-ctx-linter", m).value.trim(),
      terminal: $("#po-ctx-terminal", m).value,
      stackText: $("#po-ctx-stack", m).textContent,
    };

    var linter = [];
    if (data.linter) {
      try { linter = JSON.parse(data.linter); if (!Array.isArray(linter)) throw new Error("必须是数组"); }
      catch (e) { toast("Linter JSON 解析失败：" + e.message, "error"); return; }
    }

    var entries = null;
    if (data.manifests) {
      try { entries = JSON.parse(data.manifests); } catch (e) { toast("清单文件 JSON 解析失败：" + e.message, "error"); return; }
    }

    applyCaptureState({ data: data, linter: linter, entries: entries });
    saveJSON(LS_CTX, data);
    m.hidden = true;
    toast("🧩 IDE 上下文已注入捕获器（8 类实时生效）");
  }

  function applyCaptureState(state) {
    /* 兼容两种形态：直接保存的表单数据 / 完整恢复包 */
    var data = state.data || state;
    var linter = state.linter !== undefined ? state.linter : undefined;
    var entries = state.entries;

    var cap = optimizer.capture;
    cap.reset();
    if (data.path || data.content) cap.setOpenFile(data.path, data.content, data.line, data.col);
    if (data.selection) cap.setSelection(data.selection);

    if (entries) cap.setTechStack(PO.context.parseTechStack(entries));

    if (data.edits) {
      var rows = data.edits.split(/\r?\n/).filter(function (x) { return x.trim(); }).slice(0, 5);
      rows.reverse().forEach(function (row) {
        var parts = row.split("|");
        cap.recordEdit(parts[0].trim(), (parts[1] || "").trim());
      });
    }
    if (linter !== undefined) cap.setLinterErrors(linter);
    else if (data.linter) {
      try { var arr = JSON.parse(data.linter); if (Array.isArray(arr)) cap.setLinterErrors(arr); } catch (e) { /* 忽略 */ }
    }
    if (data.terminal) cap.setTerminalOutput(data.terminal);
  }

  function fillContextDemo() {
    var m = els.ctxModal;
    $("#po-ctx-path", m).value = "src/auth/login.js";
    $("#po-ctx-line", m).value = 12;
    $("#po-ctx-col", m).value = 18;
    $("#po-ctx-content", m).value =
      "import request from '../http';\n\n// 用户登录\nexport function login(user) {\n  return request.post('/login', user);\n}\n\nexport function logout() {\n  return request.post('/logout');\n}\n";
    $("#po-ctx-selection", m).value = "export function login(user) {\n  return request.post('/login', user);\n}";
    $("#po-ctx-manifests", m).value = '{\n  "package.json": "{\\"dependencies\\":{\\"vue\\":\\"^3.4.0\\",\\"axios\\":\\"^1.6.0\\"},\\"devDependencies\\":{\\"vite\\":\\"^5.0.0\\"}}"\n}';
    $("#po-ctx-edits", m).value = "src/http/index.js | 新增 401 拦截器\nsrc/auth/login.js | 调整登录参数名";
    $("#po-ctx-linter", m).value = '[{"file":"src/auth/login.js","line":12,"severity":"error","message":"Unexpected var","rule":"no-var"}]';
    $("#po-ctx-terminal", m).value = "$ npm test\n> login.test.js  ✗ expected 201 but got 400\nAssertionError: password is required";
  }

  /* ============================================================
   * 智能体配置弹窗（MCP / 工具定义 / 风格样本）
   * ============================================================ */
  function buildAgentModal() {
    var m = el("div", "po-modal-mask");
    m.innerHTML =
      '<div class="po-modal po-modal-agent">' +
      '  <h3>🤖 智能体专项配置</h3>' +
      '  <div class="po-form-row"><label>MCP 配置 JSON（mcpServers 风格；env 只会注入键名，值不上屏）</label>' +
      '    <textarea id="po-agent-mcp" rows="5" placeholder=\'{"mcpServers":{"files":{"command":"npx","args":["-y","@modelcontext/server-files"],"env":{"ROOT":"."}}}}\'></textarea></div>' +
      '  <div class="po-form-row"><label>工具定义 JSON（function calling schema 数组，留空则不注入工具清单）</label>' +
      '    <textarea id="po-agent-tools" rows="6" placeholder=\'[{"name":"read_file","description":"读取文件","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}]\'></textarea></div>' +
      '  <div class="po-form-row"><label>代码风格分析样本 JSON 数组（项目内 Agent 源码，留空跳过风格注入）</label>' +
      '    <textarea id="po-agent-style" rows="5" placeholder=\'[{"path":"app/agents/langgraph_agent.py","content":"def run_agent(self):\\n    pass"}]\'></textarea>' +
      '    <div class="po-modal-actions"><button class="po-mini-btn" id="po-agent-use-active">用活动文件作为样本</button>' +
      '    <button class="po-mini-btn" id="po-agent-validate">校验并预览</button><span id="po-agent-check" class="po-stack-out"></span></div></div>' +
      '  <pre id="po-agent-preview" class="po-agent-preview"></pre>' +
      '  <div class="po-modal-actions po-foot"><button class="po-primary-btn" id="po-agent-save">保存（发送时自动注入）</button>' +
      '    <button class="po-mini-btn" id="po-agent-cancel">取消</button></div>' +
      '</div>';
    document.body.appendChild(m);
    m.hidden = true;
    els.agentModal = m;
    m.addEventListener("click", function (e) { if (e.target === m) m.hidden = true; });
    $("#po-agent-cancel", m).addEventListener("click", function () { m.hidden = true; });
    $("#po-agent-save", m).addEventListener("click", saveAgentForm);
    $("#po-agent-use-active", m).addEventListener("click", function () {
      var f = optimizer.capture.state.openFile;
      if (!f || !f.content) { toast("请先在「上下文」弹窗中设置活动文件内容", "error"); return; }
      $("#po-agent-style", m).value = JSON.stringify([{ path: f.path, content: f.content }], null, 2);
    });
    $("#po-agent-validate", m).addEventListener("click", validateAgentForm);
  }

  function openAgentModal() {
    closeModals();
    var cfg = loadJSON(LS_AGENT, {});
    $("#po-agent-mcp").value = cfg.mcpConfigText || "";
    $("#po-agent-tools").value = cfg.toolDefsText || "";
    $("#po-agent-style").value = cfg.styleSourcesText || "";
    $("#po-agent-preview").textContent = "";
    els.agentModal.hidden = false;
  }

  function parseAgentForm() {
    var m = els.agentModal;
    var mcpText = $("#po-agent-mcp", m).value.trim();
    var toolsText = $("#po-agent-tools", m).value.trim();
    var styleText = $("#po-agent-style", m).value.trim();
    var styleSources = [];
    if (styleText) {
      try { styleSources = JSON.parse(styleText); if (!Array.isArray(styleSources)) throw new Error("风格样本必须是数组"); }
      catch (e) { return { error: "风格样本 JSON 错误：" + e.message }; }
    }
    return { mcpText: mcpText, toolsText: toolsText, styleText: styleText, styleSources: styleSources };
  }

  function validateAgentForm() {
    var p = parseAgentForm();
    if (p.error) { toast(p.error, "error"); return; }
    var r = PO.agent.buildAgentBlock({
      mcpConfigText: p.mcpText, toolDefsText: p.toolsText, styleSources: p.styleSources,
    });
    var out = [];
    if (r.mcp) out.push("MCP：" + (r.mcp.valid ? "✓ 合法，" + r.mcp.servers.length + " 个服务" : "✗ " + r.mcp.errors.join("；")));
    if (r.tools) out.push("工具：" + (r.tools.valid ? "✓ 合法，" + r.tools.tools.length + " 个工具" : "✗ " + r.tools.errors.join("；")));
    if (r.style) out.push("风格：" + (r.style.detected ? "✓ " + r.style.dominantLang + " / " + r.style.dominantNaming + " / " + r.style.dominantIndent : "未提供样本"));
    $("#po-agent-check").textContent = out.join(" ｜ ");
    $("#po-agent-preview").textContent = r.promptBlock || "（未填写任何配置，发送时仅注入标准工作流规范）";
  }

  function saveAgentForm() {
    var p = parseAgentForm();
    if (p.error) { toast(p.error, "error"); return; }
    saveJSON(LS_AGENT, { mcpConfigText: p.mcpText, toolDefsText: p.toolsText, styleSourcesText: p.styleText });
    els.agentModal.hidden = true;
    toast("🤖 智能体配置已保存，发送时自动注入");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})(typeof window !== "undefined" ? window : globalThis);
