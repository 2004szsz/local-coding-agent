/* ============================================================
 * 提示词优化系统 · 主编排器（三层管线）
 * ------------------------------------------------------------
 *   上层 enhancer（语义增强）
 *     → 中层 context（#指令解析 + 8 类上下文捕获 + 智能拼接）
 *       → 底层 templates（模型专属模板静默渲染系统提示词）
 *   智能体专项 agent 块（MCP/工具/风格/工作流）注入系统提示词尾部
 *
 * 浏览器与 Node 均可运行；网络通过 ContextCapture 的 Adapter 注入。
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  function PromptOptimizer(options) {
    options = options || {};
    var adapter = options.adapter !== undefined
      ? options.adapter
      : (typeof fetch === "function" ? PO.context.createHttpAdapter(options.baseUrl || "") : null);
    this.adapter = adapter;
    this.capture = new PO.context.ContextCapture(adapter);
    this.modelName = options.model || "auto";
    this.resolvedFamily = null;
  }

  /** 设置/切换当前模型（模型自动匹配数据源，可传完整模型名或 "auto"） */
  PromptOptimizer.prototype.setModel = function (name) {
    this.modelName = name || "auto";
    this.resolvedFamily = null;
    return this.modelName;
  };

  /** 应用后台 Daemon 推送的匹配结果（前台不再探测 /api/health） */
  PromptOptimizer.prototype.applyMatchStatus = function (status) {
    if (!status) return;
    if (status.model) this.modelName = status.model;
    if (status.family) this.resolvedFamily = status.family;
  };

  /** @deprecated 保留供 Node 测试；生产 UI 由 matchClient 注入匹配结果 */
  PromptOptimizer.prototype.detectModel = function () {
    var self = this;
    if (!this.adapter || typeof fetch !== "function") return Promise.resolve("auto");
    return fetch((this.adapter && "") + "/api/health", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data && data.model) {
          self.modelName = data.model;
          self.resolvedFamily = PO.templates.matchModel(data.model);
        }
        return self.modelName;
      })
      .catch(function () { return self.modelName; });
  };

  PromptOptimizer.prototype.resolveFamily = function () {
    if (this.resolvedFamily) return this.resolvedFamily;
    if (this.modelName && this.modelName !== "auto") {
      this.resolvedFamily = PO.templates.matchModel(this.modelName);
    } else {
      this.resolvedFamily = "general";
    }
    return this.resolvedFamily;
  };

  function contextFlags(snapshot) {
    return {
      hasOpenFile: !!(snapshot.openFile),
      hasSelection: !!snapshot.selection,
      hasLinter: snapshot.linterErrors.length > 0,
      hasTerminal: snapshot.terminalLines.length > 0,
      techStackText: snapshot.techStack ? PO.context.formatTechStack(snapshot.techStack) : "",
    };
  }

  /**
   * 实时语义分析（纯同步，输入框 oninput 时调用）
   * 返回指令、#上下文、意图、歧义词、缺失条件、错字修正与 token 估算
   */
  PromptOptimizer.prototype.analyzeLive = function (rawText) {
    var snapshot = this.capture.capture();
    return PO.enhancer.analyze(rawText, contextFlags(snapshot));
  };

  /**
   * 主入口：把简短口语输入转化为工程化指令
   * @param {string} rawInput 用户原始输入
   * @param {object} [opts] { model, tokenBudget, mcpConfigText, toolDefsText, styleSources, agentWorkflow }
   * @returns {Promise<object>} 完整优化结果（系统提示词 + 用户提示词 + 诊断报告）
   */
  PromptOptimizer.prototype.optimize = function (rawInput, opts) {
    opts = opts || {};
    var self = this;
    var safeInput = rawInput == null ? "" : String(rawInput);

    /* —— 第 1 层（上层）：语义实时增强 —— */
    var modelName = opts.model !== undefined ? opts.model : this.modelName;
    if (modelName) this.modelName = modelName;

    var snapshot0 = this.capture.capture();
    var enhanced = PO.enhancer.enhance(safeInput, contextFlags(snapshot0));
    var analysis = enhanced.analysis;

    /* #指令属于上下文操作，需从任务正文中剥离后再生成结构化目标 */
    var goalText = PO.context.ContextDirectives.strip(analysis.cleanText);
    var commandDirectives = analysis.commands.map(function (n) {
      return PO.enhancer.listCommands().filter(function (c) { return c.name === n; })[0].directive;
    });
    var techStackText = snapshot0.techStack ? PO.context.formatTechStack(snapshot0.techStack) : "";
    var structuredTask = PO.enhancer.buildStructuredTask({
      cleanText: goalText,
      intent: analysis.intent,
      techStack: techStackText,
      techHints: analysis.techHints,
      constraints: analysis.missingConstraints,
      commandDirectives: commandDirectives,
    });

    /* —— 第 2 层（中层）：手动 #指令解析 + 上下文智能拼接 —— */
    var resolveJob = this.adapter
      ? PO.context.ContextDirectives.resolveAll(analysis.directives, this.adapter)
      : Promise.resolve(analysis.directives.map(function (d) {
          return { ok: false, key: d.type + ":" + d.target, title: d.raw, error: "未配置上下文适配器", body: "" };
        }));

    return resolveJob.then(function (manualEntries) {
      var snapshot = self.capture.capture();
      var assembly = PO.context.assembleContext(snapshot, manualEntries, opts.tokenBudget || 6000);

      /* —— 智能体专项块（MCP/工具/风格/工作流） —— */
      var family = self.resolveFamily();
      var agentResult = PO.agent.buildAgentBlock({
        mcpConfigText: opts.mcpConfigText,
        toolDefsText: opts.toolDefsText,
        styleSources: opts.styleSources,
        workflow: opts.agentWorkflow !== false,
      });

      /* —— 第 3 层（底层）：模型专属模板静默渲染 —— */
      var systemPrompt = PO.templates.renderSystemPrompt(family, {
        scene: "IDE 内代码工程任务" + (agentResult.tools && agentResult.tools.tools.length ? "（智能体工具调用模式）" : ""),
        techStack: techStackText || undefined,
        extra: agentResult.promptBlock || undefined,
      });

      /* —— 用户提示词总装：上下文块 + 结构化任务 + 注入告警 —— */
      var userParts = [];
      if (assembly.promptBlock) userParts.push(assembly.promptBlock);
      userParts.push("## 工程化任务指令（由口语输入实时增强生成）\n\n" + structuredTask);
      var notices = [];
      if (analysis.corrections.length) {
        notices.push("已自动修正：" + analysis.corrections.map(function (c) { return c.from + "→" + c.to; }).join("、"));
      }
      if (assembly.warnings.length) notices.push.apply(notices, assembly.warnings);
      if (assembly.dropped.length) notices.push("以下上下文因预算/重复被省略：" + assembly.dropped.join("；"));
      if (notices.length) userParts.push("## 优化器附注\n- " + notices.join("\n- "));

      var userPrompt = userParts.join("\n\n");
      var tpl = PO.templates.getTemplate(family);

      return {
        ok: true,
        timestamp: new Date().toISOString(),
        model: self.modelName,
        family: family,
        modelLabel: tpl.label,
        hyperparams: tpl.hyperparams,
        systemPrompt: systemPrompt,
        userPrompt: userPrompt,
        structuredTask: structuredTask,
        analysis: {
          intent: analysis.intent,
          commands: analysis.commands,
          unknownCommands: analysis.unknownCommands,
          directives: analysis.directives,
          techHints: analysis.techHints,
          ambiguities: analysis.ambiguities,
          missingConstraints: analysis.missingConstraints,
          corrections: analysis.corrections,
        },
        context: {
          present: assembly.present,
          sections: assembly.sections.map(function (s) { return { title: s.title, estimatedTokens: s.estimatedTokens, priority: s.priority }; }),
          dropped: assembly.dropped,
          warnings: assembly.warnings,
          estimatedTokens: assembly.estimatedTokens,
          budgetTokens: assembly.budgetTokens,
        },
        agent: {
          mcpServers: agentResult.mcp ? agentResult.mcp.servers.length : 0,
          mcpErrors: agentResult.mcp ? agentResult.mcp.errors : [],
          toolCount: agentResult.tools ? agentResult.tools.tools.length : 0,
          styleDetected: agentResult.style ? agentResult.style.detected : false,
        },
        tokens: {
          system: PO.estimateTokens(systemPrompt),
          user: PO.estimateTokens(userPrompt),
          total: PO.estimateTokens(systemPrompt) + PO.estimateTokens(userPrompt),
        },
      };
    });
  };

  PO.PromptOptimizer = PromptOptimizer;
  PO.createOptimizer = function (options) { return new PromptOptimizer(options); };
})(typeof window !== "undefined" ? window : globalThis);
