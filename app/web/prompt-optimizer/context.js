/* ============================================================
 * 提示词优化系统 · 第二层（中层）：IDE 上下文动态注入系统（核心能力）
 * ------------------------------------------------------------
 *  - 实时捕获 8 类上下文：活动文件全文 / 光标位置与周边 / 选中片段 /
 *    技术栈识别 / 最近 5 次编辑 / Linter 报错 / 终端最新 10 行 / 手动追加
 *  - #file #folder #repo #url 手动扩展上下文语法（解析器 + 结构化提取）
 *  - 智能拼接：去冗余、按优先级分配 Token 预算、超预算自动截断
 *  - 数据来源通过 Adapter 抽象注入：浏览器默认走 /api/files/*，
 *    测试可注入内存 FakeAdapter，核心逻辑不依赖 DOM。
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  /* 粗略 Token 估算：中日韩全角字符约 1 token，其余非空白字符约 3 字符/token */
  function estimateTokens(text) {
    if (!text) return 0;
    var cjk = (text.match(/[　-〿぀-ヿ一-鿿＀-￯]/g) || []).length;
    var other = text.replace(/[　-〿぀-ヿ一-鿿＀-￯\s]/g, "").length;
    return cjk + Math.ceil(other / 3);
  }
  PO.estimateTokens = estimateTokens;

  var MANIFEST_FILES = ["package.json", "go.mod", "requirements.txt", "pom.xml", "Cargo.toml"];

  /* ============================================================
   * 一、技术栈自动识别（package.json / go.mod / requirements.txt /
   * pom.xml / Cargo.toml 等依赖文件提取）——纯函数，便于单测
   * fileEntries: { "package.json": "{...}", "go.mod": "..." }
   * ============================================================ */
  var NPM_FRAMEWORK_MAP = {
    react: "React", "vue": "Vue", "@angular/core": "Angular",
    express: "Express", koa: "Koa", "@nestjs/core": "NestJS",
    next: "Next.js", nuxt: "Nuxt", gatsby: "Gatsby",
    "react-dom": "React DOM", pinia: "Pinia", vuex: "Vuex",
    "react-router-dom": "React Router", axios: "Axios",
  };

  function parseTechStack(fileEntries) {
    var result = { languages: [], frameworks: [], runtimes: [], manifestFiles: [] };
    function addLanguage(l) { if (result.languages.indexOf(l) === -1) result.languages.push(l); }
    function addRuntime(r) { if (result.runtimes.indexOf(r) === -1) result.runtimes.push(r); }
    function addFramework(name, version, source) {
      result.frameworks.push({ name: name, version: version || "unknown", source: source });
    }

    if (!fileEntries || typeof fileEntries !== "object") return result;

    /* ---------- package.json ---------- */
    if (fileEntries["package.json"]) {
      result.manifestFiles.push("package.json");
      var pkg;
      try { pkg = JSON.parse(fileEntries["package.json"]); } catch (e) { pkg = null; }
      if (pkg) {
        addLanguage("JavaScript");
        var deps = {};
        Object.keys(pkg.dependencies || {}).forEach(function (k) { deps[k] = pkg.dependencies[k]; });
        Object.keys(pkg.devDependencies || {}).forEach(function (k) { deps[k] = pkg.devDependencies[k]; });
        if (deps["typescript"] || (pkg.devDependencies && pkg.devDependencies["typescript"])) addLanguage("TypeScript");
        if (deps.vite) addFramework("Vite", deps.vite, "package.json");
        if (deps.webpack) addFramework("Webpack", deps.webpack, "package.json");
        Object.keys(NPM_FRAMEWORK_MAP).forEach(function (dep) {
          if (deps[dep]) addFramework(NPM_FRAMEWORK_MAP[dep], deps[dep], "package.json");
        });
        if (pkg.engines && pkg.engines.node) addRuntime("Node " + pkg.engines.node); else addRuntime("Node");
      }
    }

    /* ---------- go.mod ---------- */
    if (fileEntries["go.mod"]) {
      result.manifestFiles.push("go.mod");
      addLanguage("Go");
      var goMod = fileEntries["go.mod"];
      var goVer = goMod.match(/^go\s+([\d.]+)/m);
      if (goVer) addRuntime("Go " + goVer[1]);
      var goMap = { gin: "Gin", echo: "Echo", fiber: "Fiber", gorm: "GORM", grpc: "gRPC", kratos: "Kratos" };
      Object.keys(goMap).forEach(function (mod) {
        var modRe = new RegExp("[\\w.-]+/" + mod + "\\b");
        if (modRe.test(goMod)) {
          var v = goMod.match(new RegExp("[\\w.-]+/" + mod + "\\s+(v?[\\d.\\w+-]+)"));
          addFramework(goMap[mod], v ? v[1] : "unknown", "go.mod");
        }
      });
    }

    /* ---------- requirements.txt ---------- */
    if (fileEntries["requirements.txt"]) {
      result.manifestFiles.push("requirements.txt");
      addLanguage("Python");
      addRuntime("Python 3");
      var pyMap = { fastapi: "FastAPI", flask: "Flask", django: "Django", tornado: "Tornado", pydantic: "Pydantic", sqlalchemy: "SQLAlchemy" };
      fileEntries["requirements.txt"].split(/\r?\n/).forEach(function (line) {
        var m = line.trim().match(/^([A-Za-z0-9_.\-]+)\s*(?:[=<>!~]=?\s*([\w.\-*]+))?/);
        if (!m) return;
        var pkg2 = m[1].toLowerCase();
        if (pyMap[pkg2]) addFramework(pyMap[pkg2], m[2] || "unknown", "requirements.txt");
      });
    }

    /* ---------- pom.xml ---------- */
    if (fileEntries["pom.xml"]) {
      result.manifestFiles.push("pom.xml");
      addLanguage("Java");
      addRuntime("JVM (Maven)");
      var pom = fileEntries["pom.xml"];
      var javaMap = { "spring-boot": "Spring Boot", springframework: "Spring Framework", mybatis: "MyBatis", lombok: "Lombok" };
      Object.keys(javaMap).forEach(function (art) {
        if (pom.indexOf(art) !== -1) addFramework(javaMap[art], "unknown", "pom.xml");
      });
    }

    /* ---------- Cargo.toml ---------- */
    if (fileEntries["Cargo.toml"]) {
      result.manifestFiles.push("Cargo.toml");
      addLanguage("Rust");
      addRuntime("Cargo");
      var cargo = fileEntries["Cargo.toml"];
      var rsMap = { "actix-web": "Actix-web", tokio: "Tokio", rocket: "Rocket", serde: "Serde", warp: "Warp" };
      Object.keys(rsMap).forEach(function (crate) {
        if (new RegExp("^" + crate.replace("-", "\\-") + "\\s*=", "m").test(cargo)) {
          addFramework(rsMap[crate], "unknown", "Cargo.toml");
        }
      });
    }

    /* 框架去重（同名保留首个带版本号的） */
    var seen = {};
    result.frameworks = result.frameworks.filter(function (f) {
      var k = f.name + "@" + f.source;
      if (seen[k]) return false;
      seen[k] = true;
      return true;
    });
    return result;
  }

  function formatTechStack(stack) {
    if (!stack || (!stack.languages.length && !stack.frameworks.length)) return "未识别（依赖文件缺失）";
    var parts = [];
    if (stack.languages.length) parts.push("语言：" + stack.languages.join("、"));
    if (stack.frameworks.length) parts.push("框架/库：" + stack.frameworks.map(function (f) { return f.name + "(" + f.version + ")"; }).join("、"));
    if (stack.runtimes.length) parts.push("运行时：" + stack.runtimes.join("、"));
    if (stack.manifestFiles.length) parts.push("依据：" + stack.manifestFiles.join("、"));
    return parts.join("；");
  }

  /* ============================================================
   * 二、IDE 上下文实时捕获器（维护最近状态快照）
   * ============================================================ */
  function ContextCapture(adapter) {
    this.adapter = adapter || null;
    this.reset();
  }

  ContextCapture.prototype.reset = function () {
    this.state = {
      openFile: null,            // {path, content, language, cursor:{line,col}}
      selection: "",             // 用户选中的代码片段
      recentEdits: [],           // 最近 5 次修改 [{path,time,summary}]
      linterErrors: [],          // Linter 实时报错 [{file,line,col,severity,message,rule}]
      terminalLines: [],         // 终端最新 10 行 [string]
      techStack: null,           // parseTechStack 结果
    };
    return this.state;
  };

  /** 设置当前打开文件（完整代码内容 + 光标精确位置，行号从 1 开始） */
  ContextCapture.prototype.setOpenFile = function (path, content, cursorLine, cursorCol) {
    var ext = String(path || "").split(".").pop().toLowerCase();
    var langMap = { js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", vue: "vue", py: "python", go: "go", java: "java", rs: "rust", json: "json", html: "html", css: "css", md: "markdown", yaml: "yaml", yml: "yaml", sh: "bash", sql: "sql" };
    this.state.openFile = {
      path: path || "<未命名>",
      content: content || "",
      language: langMap[ext] || ext || "plaintext",
      cursor: { line: cursorLine || 1, col: cursorCol || 1 },
    };
    return this.state.openFile;
  };

  ContextCapture.prototype.setSelection = function (text) {
    this.state.selection = text || "";
    return this.state.selection;
  };

  /** 记录一次编辑，自动只保留最近 5 次 */
  ContextCapture.prototype.recordEdit = function (path, summary, time) {
    this.state.recentEdits.unshift({
      path: path || "<未知文件>",
      time: time || new Date().toISOString(),
      summary: summary || "(无描述)",
    });
    if (this.state.recentEdits.length > 5) this.state.recentEdits.length = 5;
    return this.state.recentEdits;
  };

  ContextCapture.prototype.setLinterErrors = function (errors) {
    this.state.linterErrors = Array.isArray(errors) ? errors.slice() : [];
    return this.state.linterErrors;
  };

  /** 设置终端输出（整段文本或字符串数组），自动只保留最新 10 行 */
  ContextCapture.prototype.setTerminalOutput = function (output) {
    var lines;
    if (Array.isArray(output)) lines = output;
    else lines = String(output || "").split(/\r?\n/);
    this.state.terminalLines = lines.filter(function (l) { return l.trim().length > 0; }).slice(-10);
    return this.state.terminalLines;
  };

  ContextCapture.prototype.setTechStack = function (stack) {
    this.state.techStack = stack;
    return this.state.techStack;
  };

  /** 通过适配器读取工作区内清单文件并识别技术栈；任何失败都安全降级 */
  ContextCapture.prototype.detectTechStack = function () {
    var self = this;
    if (!this.adapter || !this.adapter.readFile) {
      this.state.techStack = parseTechStack({});
      return Promise.resolve(this.state.techStack);
    }
    var entries = {};
    return MANIFEST_FILES.reduce(function (p, file) {
      return p.then(function () {
        return self.adapter.readFile(file).then(function (res) {
          if (res && typeof res.content === "string") entries[file] = res.content;
        }, function () { /* 清单不存在：忽略 */ });
      });
    }, Promise.resolve()).then(function () {
      self.state.techStack = parseTechStack(entries);
      return self.state.techStack;
    });
  };

  /** 提取光标周边代码片段（默认上下各 12 行，带行号） */
  ContextCapture.prototype.cursorSnippet = function (radius) {
    radius = typeof radius === "number" ? radius : 12;
    var f = this.state.openFile;
    if (!f || !f.content) return null;
    var all = f.content.split(/\r?\n/);
    var line = Math.min(Math.max(f.cursor.line, 1), all.length);
    var start = Math.max(1, line - radius);
    var end = Math.min(all.length, line + radius);
    var width = String(end).length;
    var body = [];
    for (var i = start; i <= end; i++) {
      var marker = i === line ? ">>" : "  ";
      var num = String(i).length < width ? new Array(width - String(i).length + 1).join(" ") + i : String(i);
      body.push(marker + " " + num + " | " + (all[i - 1] != null ? all[i - 1] : ""));
    }
    return { path: f.path, language: f.language, line: line, col: f.cursor.col, snippet: body.join("\n") };
  };

  /** 生成 8 类上下文的结构化快照 */
  ContextCapture.prototype.capture = function () {
    var s = this.state;
    return {
      openFile: s.openFile ? {
        path: s.openFile.path,
        language: s.openFile.language,
        content: s.openFile.content,
        totalLines: s.openFile.content ? s.openFile.content.split(/\r?\n/).length : 0,
      } : null,
      cursor: s.openFile ? this.cursorSnippet() : null,
      selection: s.selection || null,
      techStack: s.techStack || null,
      recentEdits: s.recentEdits.slice(),
      linterErrors: s.linterErrors.slice(),
      terminalLines: s.terminalLines.slice(),
      present: {
        openFile: !!s.openFile,
        cursor: !!s.openFile,
        selection: !!s.selection,
        techStack: !!(s.techStack && (s.techStack.languages.length || s.techStack.frameworks.length)),
        recentEdits: s.recentEdits.length > 0,
        linterErrors: s.linterErrors.length > 0,
        terminalLines: s.terminalLines.length > 0,
      },
    };
  };

  /* ============================================================
   * 三、#file / #folder / #repo / #url 手动扩展上下文
   * ============================================================ */
  /* 指令词只吃掉紧跟的一个非空白目标 token，避免吞掉行末的需求描述 */
  var DIRECTIVE_RE = /#(file|folder|repo|url)\b(?:[ \t]*([^\s#]+))?/gi;

  var ContextDirectives = {
    /** 解析输入文本中的全部 #指令 */
    parse: function (text) {
      var list = [];
      if (!text) return list;
      var m;
      DIRECTIVE_RE.lastIndex = 0;
      while ((m = DIRECTIVE_RE.exec(text)) !== null) {
        var target = (m[2] || "").trim();
        list.push({ type: m[1].toLowerCase(), target: target, raw: m[0], start: m.index, end: m.index + m[0].length });
      }
      return list;
    },

    /** 从文本中剥离 #指令（用于后续语义增强，避免指令被当作需求内容） */
    strip: function (text) {
      return String(text || "").replace(DIRECTIVE_RE, " ").replace(/[ \t]{2,}/g, " ").replace(/^\s+|\s+$/g, "");
    },

    /** 关键信息提取：把追加内容结构化为可注入提示词的紧凑块 */
    structureEntry: function (entry) {
      if (!entry || entry.ok === false) {
        var errMsg = (entry && entry.error) || "未知错误";
        return { key: (entry && entry.key) || "error", title: (entry && entry.title) || "读取失败", body: "[上下文读取失败] " + errMsg, chars: 0, ok: false, error: errMsg };
      }
      var CAP_CODE = 4000, CAP_TEXT = 3000;
      if (entry.kind === "file" || entry.kind === "folder" || entry.kind === "repo") {
        var lang = entry.language || "";
        var content = entry.content || "";
        var truncated = false;
        if (content.length > CAP_CODE) { content = content.slice(0, CAP_CODE) + "\n// … 内容过长，已截断，共 " + entry.content.length + " 字符 …"; truncated = true; }
        return {
          key: entry.kind + ":" + (entry.path || entry.root || ""),
          title: "📄 " + (entry.path || entry.root || "仓库根目录") + (entry.kind === "folder" ? "/（目录）" : entry.kind === "repo" ? "/（整个仓库）" : ""),
          body: "```" + lang + "\n" + content + "\n```",
          chars: content.length,
          truncated: truncated,
          ok: true,
        };
      }
      if (entry.kind === "url") {
        var raw = String(entry.content || "");
        var title = entry.title || raw.match(/<title[^>]*>([^<]+)<\/title>/i);
        title = title ? (title[1] || title).trim() : entry.url;
        var headings = (raw.match(/^#{1,3}\s+(.+)$/gm) || []).slice(0, 10).map(function (h) { return h.replace(/^#+\s*/, ""); });
        var text2 = raw.replace(/<script[\s\S]*?<\/script>/gi, "").replace(/<style[\s\S]*?<\/style>/gi, "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
        if (text2.length > CAP_TEXT) text2 = text2.slice(0, CAP_TEXT) + " …（已截断，原始长度 " + raw.length + " 字符）";
        var body = "来源：" + entry.url + (headings.length ? "\n页面大纲：\n- " + headings.join("\n- ") : "") + "\n正文摘录：\n" + text2;
        return { key: "url:" + entry.url, title: "🔗 " + title, body: body, chars: body.length, ok: true };
      }
      return { key: "unknown", title: "未知上下文", body: String(entry && entry.content || ""), chars: 0, ok: true };
    },

    /** 使用适配器解析全部指令，单个失败不影响其它项 */
    resolveAll: function (directives, adapter) {
      if (!Array.isArray(directives)) return Promise.resolve([]);
      return Promise.all(directives.map(function (d) {
        if (!adapter) return Promise.resolve({ ok: false, error: "未配置上下文适配器", key: d.type + ":" + d.target });
        var job;
        if (d.type === "file") {
          job = Promise.resolve(adapter.readFile(d.target)).then(function (r) {
            return { kind: "file", path: (r && r.path) || d.target, content: r && r.content, language: extToLang(d.target) };
          });
        } else if (d.type === "folder") {
          job = Promise.resolve(adapter.listDir(d.target)).then(function (r) {
            return { kind: "folder", path: d.target, content: renderTree(r) };
          });
        } else if (d.type === "repo") {
          job = Promise.resolve(adapter.repoOverview ? adapter.repoOverview() : repoOverview(adapter)).then(function (r) {
            return { kind: "repo", root: ".", content: r };
          });
        } else {
          job = Promise.resolve(adapter.fetchUrl(d.target)).then(function (r) {
            return { kind: "url", url: d.target, title: r && r.title, content: r && r.content };
          });
        }
        return job.then(null, function (err) {
          return { ok: false, key: d.type + ":" + d.target, error: (err && err.message) || String(err), title: d.raw };
        });
      })).then(function (entries) {
        return entries.map(ContextDirectives.structureEntry);
      });
    },
  };

  function extToLang(path) {
    var ext = String(path || "").split(".").pop().toLowerCase();
    var map = { js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript", vue: "vue", py: "python", go: "go", java: "java", rs: "rust", json: "json", html: "html", css: "css", md: "markdown", yaml: "yaml", yml: "yaml", sh: "bash" };
    return map[ext] || ext || "plaintext";
  }

  /** 目录清单渲染为树状文本（适配器返回 {items:[...]}，兼容字符串/对象元素） */
  function renderTree(listResult) {
    if (!listResult) return "(空目录)";
    var items = listResult.items || listResult;
    if (typeof items === "string") items = items.split(/\r?\n/);
    if (!items.length) return "(空目录)";
    var lines = [];
    var truncated = items.length > 50;
    items.slice(0, 50).forEach(function (it) {
      if (typeof it === "string") { lines.push("├─ " + it); return; }
      var name = it.name || it.path || JSON.stringify(it);
      lines.push((it.is_dir || it.type === "dir" ? "├─ 📁 " : "├─ ") + name);
    });
    if (truncated) lines.push("└─ … 其余 " + (items.length - 50) + " 项已省略");
    return lines.join("\n");
  }

  function repoOverview(adapter) {
    var tree;
    return Promise.resolve(adapter.listDir(".")).then(function (r) {
      tree = renderTree(r);
      var reads = MANIFEST_FILES.map(function (f) {
        return Promise.resolve(adapter.readFile(f)).then(function (res) {
          return f + ":\n" + (res && res.content ? res.content.slice(0, 1500) : "");
        }, function () { return ""; });
      });
      return Promise.all(reads);
    }).then(function (manifests) {
      return "仓库目录树（根目录）:\n" + tree + "\n\n依赖清单:\n" + manifests.filter(Boolean).join("\n\n");
    });
  }

  /* ============================================================
   * 四、上下文智能拼接：优先级填充 + 去重 + Token 预算截断
   * ============================================================ */
  function assembleContext(captured, manualEntries, tokenBudget) {
    tokenBudget = tokenBudget || 6000;
    var sections = [];
    var warnings = [];
    var dropped = [];
    var fileFingerprints = {};

    function pushSection(title, body, priority) {
      if (!body || !body.trim()) return;
      sections.push({ title: title, body: body.trim(), priority: priority, estimatedTokens: estimateTokens(body) });
    }

    /* 1. 当前打开文件的完整代码内容 */
    if (captured.openFile) {
      var f = captured.openFile;
      fileFingerprints[f.path] = hashText(f.content);
      pushSection("① 当前打开文件（完整内容 · " + f.language + " · " + f.totalLines + " 行 · " + f.path + "）",
        "```" + f.language + " path=" + f.path + "\n" + f.content + "\n```", 10);
    }

    /* 2. 光标精确位置及周边代码 */
    if (captured.cursor) {
      var c = captured.cursor;
      pushSection("② 光标位置（" + c.path + ":" + c.line + ":" + c.col + "，>> 标记为光标所在行）",
        "```" + c.language + "\n" + c.snippet + "\n```", 20);
    }

    /* 3. 用户选中的代码片段 */
    if (captured.selection) {
      pushSection("③ 用户选中的代码片段（以此为主要操作对象）",
        "```\n" + captured.selection + "\n```", 15);
    }

    /* 4. 项目技术栈 */
    if (captured.techStack) {
      pushSection("④ 项目技术栈（自动识别）", formatTechStack(captured.techStack), 30);
    }

    /* 5. 最近编辑记录 */
    if (captured.recentEdits && captured.recentEdits.length) {
      pushSection("⑤ 最近编辑记录（最新在前，最多 5 条）",
        captured.recentEdits.map(function (e, i) {
          return (i + 1) + ". [" + e.time + "] " + e.path + " — " + e.summary;
        }).join("\n"), 40);
    }

    /* 6. Linter 实时报错 */
    if (captured.linterErrors && captured.linterErrors.length) {
      pushSection("⑥ Linter / 编译器实时报错（修复时必须逐条消除）",
        captured.linterErrors.map(function (e) {
          return "- [" + (e.severity || "error") + "] " + (e.file || "?") + ":" + (e.line || "?") + " " + (e.message || "") + (e.rule ? " (" + e.rule + ")" : "");
        }).join("\n"), 18);
    }

    /* 7. 终端最新输出 */
    if (captured.terminalLines && captured.terminalLines.length) {
      pushSection("⑦ 终端最新输出（最近 10 行，作为运行/报错事实依据）",
        "```\n" + captured.terminalLines.join("\n") + "\n```", 25);
    }

    /* 8. 模块协同规则（来自 config/module-rules.yaml + module-cohesion.mdc） */
    var rulesSnippet = PO.context.moduleRulesSnippet
      || (global.ModuleRules && global.ModuleRules.getContextSnippet && global.ModuleRules.getContextSnippet())
      || "";
    if (rulesSnippet && rulesSnippet.trim()) {
      pushSection("⑧ 模块协同规则（全栈工程师须遵守的架构约束）", rulesSnippet.trim(), 12);
    }

    /* 9. 手动追加的 #指令上下文（去冗余：与活动文件同源/内容重复则丢弃） */
    (manualEntries || []).forEach(function (entry) {
      if (!entry || !entry.ok) {
        warnings.push("手动上下文获取失败：" + (entry && (entry.title || entry.error) || "未知项"));
        return;
      }
      var fp = hashText(entry.body);
      var sameAsOpen = false;
      if (entry.key.indexOf("file:") === 0) {
        var p = entry.key.slice(5);
        if (fileFingerprints[p] === fp) sameAsOpen = true;
      }
      if (fileFingerprints["__manual_" + fp]) {
        dropped.push(entry.title + "（重复内容已去重）");
        return;
      }
      fileFingerprints["__manual_" + fp] = true;
      if (sameAsOpen) { dropped.push(entry.title + "（与当前打开文件重复，已去重）"); return; }
      pushSection("⑨ 手动追加上下文 · " + entry.title, entry.body, 5);
    });

    /* 按优先级（数字小=更重要）排序，贪心填充预算，超预算的低优先级内容整体丢弃 */
    sections.sort(function (a, b) { return a.priority - b.priority; });
    var used = 0;
    var kept = [];
    sections.forEach(function (s) {
      if (used + s.estimatedTokens <= tokenBudget) { kept.push(s); used += s.estimatedTokens; }
      else dropped.push(s.title + "（Token 预算不足，已省略）");
    });

    /* 输出时仍按 ①~⑧ 的自然顺序展示 */
    var orderMap = { "①": 1, "②": 2, "③": 3, "④": 4, "⑤": 5, "⑥": 6, "⑦": 7, "⑧": 8, "⑨": 9 };
    kept.sort(function (a, b) {
      var ka = a.title.charAt(0), kb = b.title.charAt(0);
      return (orderMap[ka] || 9) - (orderMap[kb] || 9);
    });

    var block = kept.length ? "## IDE 自动注入的项目上下文（以下内容为编辑器实时捕获，视为事实依据）\n\n" +
      kept.map(function (s) { return "### " + s.title + "\n" + s.body; }).join("\n\n") : "";

    return {
      sections: kept,
      dropped: dropped,
      warnings: warnings,
      promptBlock: block,
      estimatedTokens: used,
      budgetTokens: tokenBudget,
      present: captured.present || {},
    };
  }

  function hashText(text) {
    var h = 0;
    var s = String(text || "");
    for (var i = 0; i < s.length; i++) {
      h = ((h << 5) - h + s.charCodeAt(i)) | 0;
    }
    return "h" + h;
  }

  /* ============================================================
   * 五、浏览器默认 HTTP 适配器（复用现有 /api/files 接口）
   * ============================================================ */
  function createHttpAdapter(baseUrl) {
    baseUrl = baseUrl || "";
    function getJson(url) {
      return fetch(url, { headers: { "Accept": "application/json" } }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
    }
    return {
      readFile: function (path) {
        return getJson(baseUrl + "/api/files/read?path=" + encodeURIComponent(path));
      },
      listDir: function (path) {
        return getJson(baseUrl + "/api/files/list?path=" + encodeURIComponent(path || "."));
      },
      fetchUrl: function (url) {
        return fetch(url).then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.text().then(function (content) {
            var title = content.match(/<title[^>]*>([^<]+)<\/title>/i);
            return { url: url, title: title ? title[1].trim() : url, content: content };
          });
        });
      },
      repoOverview: function () { return repoOverview(this); },
    };
  }

  /** 将后台 Daemon 推送的快照同步到本地 ContextCapture（无 UI） */
  function applyRemoteSnapshot(capture, snap) {
    if (!capture || !snap) return;
    capture.reset();
    if (snap.openFile) {
      var cur = snap.cursor || {};
      capture.setOpenFile(
        snap.openFile.path,
        snap.openFile.content,
        cur.line || 1,
        cur.col || 1
      );
    }
    if (snap.selection) capture.setSelection(snap.selection);
    if (snap.techStack) capture.setTechStack(snap.techStack);
    if (snap.recentEdits && snap.recentEdits.length) {
      snap.recentEdits.slice().reverse().forEach(function (e) {
        capture.recordEdit(e.path, e.summary, e.time);
      });
    }
    if (snap.linterErrors) capture.setLinterErrors(snap.linterErrors);
    if (snap.terminalLines && snap.terminalLines.length) {
      capture.setTerminalOutput(snap.terminalLines);
    }
  }

  PO.context = {
    estimateTokens: estimateTokens,
    parseTechStack: parseTechStack,
    formatTechStack: formatTechStack,
    ContextCapture: ContextCapture,
    ContextDirectives: ContextDirectives,
    assembleContext: assembleContext,
    createHttpAdapter: createHttpAdapter,
    applyRemoteSnapshot: applyRemoteSnapshot,
    MANIFEST_FILES: MANIFEST_FILES.slice(),
  };
})(typeof window !== "undefined" ? window : globalThis);
