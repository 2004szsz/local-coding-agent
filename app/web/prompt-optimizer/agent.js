/* ============================================================
 * 提示词优化系统 · 智能体应用开发专项优化
 * ------------------------------------------------------------
 *  1. MCP 配置与工具定义自动识别（mcpServers / JSON Schema 工具表）
 *  2. 智能体工作流规范注入：任务拆解 → 工具调用 → 结果校验 → 迭代修正
 *  3. 项目内 Agent 代码风格分析器：架构风格、命名规范 → 注入提示词
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  /* ============================================================
   * 一、MCP 配置自动识别
   * 兼容两种形态：{"mcpServers": {name: {...}}}（Claude Desktop 风格）
   * 以及 {servers:[...]} / 裸 map；stdio 与 http/sse 两种传输
   * ============================================================ */
  function parseMcpConfig(input) {
    var errors = [];
    var raw = input;
    if (typeof input === "string") {
      var text = input.trim();
      if (!text) return { valid: true, servers: [], raw: null, errors: [] };
      try {
        raw = JSON.parse(text);
      } catch (e) {
        return { valid: false, servers: [], raw: null, errors: ["MCP 配置不是合法 JSON：" + e.message] };
      }
    }
    if (!raw || typeof raw !== "object") {
      return { valid: false, servers: [], raw: raw, errors: ["MCP 配置根节点必须是对象"] };
    }

    var map = raw.mcpServers || raw.servers || raw;
    var servers = [];

    if (Array.isArray(map)) {
      map.forEach(function (item, idx) {
        if (!item || typeof item !== "object") { errors.push("第 " + (idx + 1) + " 个服务不是对象"); return; }
        servers.push(normalizeServer(item.name || "server-" + (idx + 1), item, errors));
      });
    } else if (typeof map === "object") {
      Object.keys(map).forEach(function (name) {
        var conf = map[name];
        if (!conf || typeof conf !== "object") { errors.push("服务 " + name + " 的配置不是对象"); return; }
        servers.push(normalizeServer(name, conf, errors));
      });
    }

    /* 内置校验：stdio 需要 command；http/sse 需要 url */
    servers.forEach(function (s) {
      if (s.transport === "stdio" && !s.command) errors.push("服务 " + s.name + " 缺少 command 字段");
      if (s.transport !== "stdio" && !s.url) errors.push("服务 " + s.name + " 缺少 url 字段");
    });

    return { valid: errors.length === 0, servers: servers, raw: raw, errors: errors };
  }

  function normalizeServer(name, conf, errors) {
    var transport = conf.transport || (conf.url ? (conf.type === "sse" ? "sse" : "http") : "stdio");
    return {
      name: name,
      transport: transport,
      command: conf.command || "",
      args: Array.isArray(conf.args) ? conf.args : [],
      url: conf.url || "",
      envKeys: conf.env && typeof conf.env === "object" ? Object.keys(conf.env) : [],
      toolsHint: Array.isArray(conf.tools) ? conf.tools : (conf.disabledTools ? "disabled:" + conf.disabledTools.length : "auto"),
    };
  }

  /** 渲染 MCP 服务清单（只展示 env 键名，绝不输出 env 的值，避免泄密） */
  function renderMcpCatalog(parsed) {
    if (!parsed || !parsed.servers.length) return "";
    var lines = ["可用 MCP 服务（自动识别）："];
    parsed.servers.forEach(function (s, i) {
      var conn = s.transport === "stdio"
        ? "stdio：" + s.command + (s.args.length ? " " + s.args.join(" ") : "")
        : s.transport + "：" + s.url;
      lines.push((i + 1) + ". " + s.name + "（" + conn + "）" +
        (s.envKeys.length ? "，环境变量：" + s.envKeys.map(function (k) { return k + "=***"; }).join("、") : ""));
    });
    if (parsed.errors.length) lines.push("⚠ 配置告警：" + parsed.errors.join("；"));
    return lines.join("\n");
  }

  /* ============================================================
   * 二、工具定义自动识别（OpenAI function calling JSON Schema 风格）
   * 接受：[{name,description,parameters/inputSchema}] 或 {name:{...}} 或 JSON 字符串
   * ============================================================ */
  function extractToolDefs(input) {
    var errors = [];
    var raw = input;
    if (typeof input === "string") {
      var text = input.trim();
      if (!text) return { valid: true, tools: [], errors: [] };
      try { raw = JSON.parse(text); } catch (e) {
        return { valid: false, tools: [], errors: ["工具定义不是合法 JSON：" + e.message] };
      }
    }
    var list = [];
    if (Array.isArray(raw)) {
      raw.forEach(function (t, i) {
        if (!t || !t.name) { errors.push("第 " + (i + 1) + " 个工具缺少 name"); return; }
        list.push(normalizeTool(t));
      });
    } else if (raw && typeof raw === "object") {
      Object.keys(raw).forEach(function (name) { list.push(normalizeTool(raw[name].name ? raw[name] : Object.assign({ name: name }, raw[name]))); });
    } else {
      return { valid: false, tools: [], errors: ["工具定义必须是数组或对象"] };
    }
    return { valid: errors.length === 0, tools: list, errors: errors };
  }

  function normalizeTool(t) {
    var schema = t.parameters || t.inputSchema || t.schema || {};
    var props = schema.properties || {};
    return {
      name: t.name,
      description: t.description || "",
      required: schema.required || [],
      properties: Object.keys(props).map(function (k) {
        return { name: k, type: props[k] && props[k].type || "any", description: props[k] && props[k].description || "" };
      }),
    };
  }

  function renderToolCatalog(parsed) {
    if (!parsed || !parsed.tools.length) return "";
    var lines = ["可用工具列表（" + parsed.tools.length + " 个，调用时严格遵循 JSON Schema）："];
    parsed.tools.forEach(function (t, i) {
      var params = t.properties.map(function (p) {
        return p.name + ": " + p.type + (t.required.indexOf(p.name) !== -1 ? "(必填)" : "(可选)");
      }).join(", ");
      lines.push((i + 1) + ". " + t.name + "(" + params + ") — " + (t.description || "无描述"));
    });
    lines.push("工具调用规则：一次只调用一个工具；arguments 必须是合法 JSON 且必填参数齐全；" +
      "收到返回后先校验结果再继续；同一错误重试不超过 2 次，随后向用户报告。");
    return lines.join("\n");
  }

  /* ============================================================
   * 三、智能体工作流规范注入
   * 任务拆解 → 工具调用 → 结果校验 → 迭代修正（标准执行链路）
   * ============================================================ */
  function buildWorkflowDirective() {
    return [
      "【智能体标准工作流（必须按此链路执行）】",
      "1. 任务拆解：把用户目标拆为可验证的最小子任务，列出依赖顺序；复杂任务先输出拆解清单。",
      "2. 工具调用：依据子任务选择最合适的工具，调用前说明意图，参数严格遵守工具 schema，禁止猜测参数。",
      "3. 结果校验：对工具返回做完整性与一致性校验（空结果/错误码/字段缺失），关键写操作需二次确认影响面。",
      "4. 迭代修正：校验失败时带着错误证据回到第 1/2 步修正方案；同一方案失败 2 次必须更换思路或向用户求助。",
      "5. 收尾：全部子任务完成后，汇总变更内容、验证结果与遗留风险。",
    ].join("\n");
  }

  /* ============================================================
   * 四、项目内 Agent 代码风格分析器
   * 输入：[{path, content}]，输出：架构风格 + 命名规范等机器指标与指南文本
   * ============================================================ */
  var LANG_BY_EXT = { js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", vue: "vue", py: "python", go: "go", java: "java", rs: "rust" };
  var KEYWORDS = {};
  ("const let var function return if else for while switch case break continue new class extends import from export default try catch finally async await typeof in of true false null undefined this super static get set yield delete void do with interface type enum implements public private protected protected package func def nil None True False panic range struct interface map chan package import fmt").split(" ").forEach(function (w) { KEYWORDS[w] = 1; });

  function classifyName(name) {
    if (/^[A-Z][A-Z0-9_]*$/.test(name) && /[A-Z]/.test(name.slice(1) + "A") && !/[a-z]/.test(name)) return "upper"; // UPPER_CASE
    if (/^[A-Z]/.test(name)) return "pascal";
    if (/_/.test(name) && /^[a-z][a-z0-9_]*$/.test(name)) return "snake";
    if (/^[a-z][A-Za-z0-9]*$/.test(name) && /[A-Z]/.test(name)) return "camel";
    if (/^[a-z][a-z0-9]*$/.test(name)) return "lower";
    return "other";
  }

  function analyzeCodeStyle(sources) {
    var stats = {
      files: 0, totalLines: 0, codeLines: 0, commentLines: 0,
      languages: {}, indents: { tab: 0, space2: 0, space4: 0, other: 0 },
      names: { snake: 0, camel: 0, pascal: 0, upper: 0, lower: 0, other: 0 },
      quotes: { single: 0, double: 0 }, semicolons: 0,
      frameworks: {}, fileNaming: { kebab: 0, snake: 0, camel: 0, pascal: 0, other: 0 },
    };
    if (!Array.isArray(sources) || !sources.length) {
      return { detected: false, stats: stats, guide: "未提供项目源码样本，无法分析代码风格，默认遵循各语言官方风格指南。" };
    }

    sources.forEach(function (src) {
      if (!src || !src.content) return;
      var ext = String(src.path || "").split(".").pop().toLowerCase();
      var lang = LANG_BY_EXT[ext] || "other";
      stats.files++;
      stats.languages[lang] = (stats.languages[lang] || 0) + 1;

      /* 文件名命名 */
      var base = String(src.path || "").split(/[/\\]/).pop().replace(/\.[^.]+$/, "");
      if (/^[a-z][a-z0-9-]*$/.test(base) && base.indexOf("-") !== -1) stats.fileNaming.kebab++;
      else if (/^[a-z][a-z0-9_]*$/.test(base) && base.indexOf("_") !== -1) stats.fileNaming.snake++;
      else if (/^[a-z][A-Za-z0-9]*$/.test(base) && /[A-Z]/.test(base)) stats.fileNaming.camel++;
      else if (/^[A-Z][A-Za-z0-9]*$/.test(base)) stats.fileNaming.pascal++;
      else stats.fileNaming.other++;

      /* 框架特征（import / require / from） */
      var fwPatterns = [
        ["React", /(?:from\s+['"]react['"]|require\(['"]react['"]\))/],
        ["Vue", /(?:from\s+['"]vue['"]|require\(['"]vue['"]\))/],
        ["Express", /(?:from\s+['"]express['"]|require\(['"]express['"]\))/],
        ["FastAPI", /from\s+fastapi\s+import|import\s+fastapi/],
        ["Flask", /from\s+flask\s+import|import\s+flask/],
        ["LangGraph", /from\s+langgraph|import\s+langgraph|require\(['"]langgraph['"]\)/],
      ];
      fwPatterns.forEach(function (p) {
        if (p[1].test(src.content)) stats.frameworks[p[0]] = (stats.frameworks[p[0]] || 0) + 1;
      });

      var lines = src.content.split(/\r?\n/);
      lines.forEach(function (line) {
        stats.totalLines++;
        var trimmed = line.trim();
        if (!trimmed) return;
        if (/^(\/\/|#|\/\*|\*|--)/.test(trimmed)) { stats.commentLines++; return; }
        stats.codeLines++;

        var indent = line.match(/^([ \t]*)/)[1];
        if (indent.indexOf("\t") !== -1) stats.indents.tab++;
        else if (indent.length >= 4) stats.indents.space4++;
        else if (indent.length === 2) stats.indents.space2++;
        else if (indent.length) stats.indents.other++;

        stats.semicolons += (line.match(/;\s*($|\/\/|#)/g) || []).length;
        stats.quotes.single += (line.match(/'/g) || []).length;
        stats.quotes.double += (line.match(/"/g) || []).length;
      });

      /* 标识符采样：函数/变量/类定义 + 通用单词 */
      var idMatches = src.content.match(/(?:function|func|def)\s+(?:\([^)]*\)\s*)?([A-Za-z_$][\w$]*)|(?:const|let|var|class|type|interface)\s+([A-Za-z_$][\w$]*)/g) || [];
      idMatches.forEach(function (m) {
        var name = (m.match(/([A-Za-z_$][\w$]*)$/) || [])[1];
        if (name && !KEYWORDS[name]) stats.names[classifyName(name)]++;
      });
    });

    var dominantLang = pickTop(stats.languages) || "unknown";
    var dominantIndent = pickTop(stats.indents);
    var dominantName = pickTop(stats.names);
    var dominantFileNaming = pickTop(stats.fileNaming);
    var quoteStyle = stats.quotes.single > stats.quotes.double ? "单引号" : stats.quotes.double > stats.quotes.single ? "双引号" : "无明显倾向";
    var frameworks = Object.keys(stats.frameworks);
    var commentDensity = stats.codeLines ? Math.round(stats.commentLines / stats.codeLines * 100) : 0;

    var guide = [
      "【项目代码风格（自动分析 " + stats.files + " 个源码文件得出，新代码必须遵循）】",
      "- 主导语言：" + dominantLang + "；检测到框架：" + (frameworks.length ? frameworks.join("、") : "无明显框架特征"),
      "- 缩进：" + indentLabel(dominantIndent) + "；字符串引号倾向：" + quoteStyle,
      "- 标识符命名：" + nameLabel(dominantName) + "；文件命名：" + fileNamingLabel(dominantFileNaming),
      "- 注释密度：约 " + commentDensity + "%（注释行/代码行），新代码注释密度应保持同量级",
      "- 架构要求：新增文件放入与现有模块一致的分层目录；复用已有工具/基类，禁止平行重复实现。",
    ].join("\n");

    return {
      detected: true,
      stats: stats,
      dominantLang: dominantLang,
      dominantIndent: dominantIndent,
      dominantNaming: dominantName,
      dominantFileNaming: dominantFileNaming,
      quoteStyle: quoteStyle,
      frameworks: frameworks,
      commentDensity: commentDensity,
      guide: guide,
    };
  }

  function pickTop(obj) {
    var top = null, max = -1;
    Object.keys(obj).forEach(function (k) {
      if (obj[k] > max) { max = obj[k]; top = k; }
    });
    return max > 0 ? top : null;
  }
  function indentLabel(k) {
    return { tab: "Tab 制表符", space2: "2 空格", space4: "4 空格", other: "其它缩进" }[k] || "无明显倾向";
  }
  function nameLabel(k) {
    return { snake: "snake_case", camel: "camelCase", pascal: "PascalCase", upper: "UPPER_CASE", lower: "小写单词", other: "无明显倾向" }[k] || "无明显倾向";
  }
  function fileNamingLabel(k) {
    return { kebab: "kebab-case", snake: "snake_case", camel: "camelCase", pascal: "PascalCase", other: "混合/无明显倾向" }[k] || "无明显倾向";
  }

  /* ============================================================
   * 五、智能体专项总装块
   * ============================================================ */
  function buildAgentBlock(options) {
    options = options || {};
    var parts = [];
    var mcp = null;
    if (options.mcpConfigText != null && options.mcpConfigText !== "") {
      mcp = parseMcpConfig(options.mcpConfigText);
      var cat = renderMcpCatalog(mcp);
      if (cat) parts.push(cat);
    }
    var tools = null;
    if (options.toolDefsText != null && options.toolDefsText !== "") {
      tools = extractToolDefs(options.toolDefsText);
      var tcat = renderToolCatalog(tools);
      if (tcat) parts.push(tcat);
    }
    var style = null;
    if (Array.isArray(options.styleSources) && options.styleSources.length) {
      style = analyzeCodeStyle(options.styleSources);
      if (style.detected) parts.push(style.guide);
    }
    if (options.workflow !== false) parts.push(buildWorkflowDirective());

    return {
      mcp: mcp,
      tools: tools,
      style: style,
      promptBlock: parts.length ? "## 智能体专项上下文\n\n" + parts.join("\n\n") : "",
    };
  }

  PO.agent = {
    parseMcpConfig: parseMcpConfig,
    renderMcpCatalog: renderMcpCatalog,
    extractToolDefs: extractToolDefs,
    renderToolCatalog: renderToolCatalog,
    buildWorkflowDirective: buildWorkflowDirective,
    analyzeCodeStyle: analyzeCodeStyle,
    classifyName: classifyName,
    buildAgentBlock: buildAgentBlock,
  };
})(typeof window !== "undefined" ? window : globalThis);
