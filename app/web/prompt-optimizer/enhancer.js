/* ============================================================
 * 提示词优化系统 · 第三层（上层）：输入语义实时增强系统（显性优化）
 * ------------------------------------------------------------
 *  - 口语化 → 结构化工程指令引擎（模糊需求识别 + 标准要素补全）
 *  - 快捷指令系统：/spec /plan + /test /review /refactor /fix /doc /explain
 *    （共 8 个，另提供 registerCommand 开放扩展接口）
 *  - 输入错误自动修正：歧义识别 + 缺失条件补充 + 常见代码约束库
 *  - 纯同步、零 IO，可在浏览器 oninput 中实时执行
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});
  var estimateTokens = PO.estimateTokens;

  function dedupe(arr) {
    var seen = {}, out = [];
    (arr || []).forEach(function (x) { if (!seen[x]) { seen[x] = 1; out.push(x); } });
    return out;
  }

  /* ============================================================
   * 一、快捷指令系统（可扩展注册表）
   * ============================================================ */
  var COMMANDS = {};

  function registerCommand(name, def) {
    if (!name || !/^[a-z]{2,12}$/.test(name)) throw new Error("非法指令名: " + name);
    COMMANDS[name] = {
      name: name,
      label: def.label || name,
      description: def.description || "",
      directive: def.directive || "",
    };
    return COMMANDS[name];
  }

  function listCommands() {
    return Object.keys(COMMANDS).map(function (k) { return COMMANDS[k]; });
  }

  registerCommand("spec", {
    label: "/spec 规范模式",
    description: "强制遵循项目规范、保证功能不变性，并输出验证步骤",
    directive:
      "【/spec 规范模式】\n" +
      "1. 严格遵循项目既有规范：目录结构、命名约定、错误处理与日志风格，不得引入与现状不一致的新模式。\n" +
      "2. 功能不变性：只实现本指令描述的需求，不改动需求范围以外的任何既有行为，不夹带顺手重构。\n" +
      "3. 完成后必须输出：变更文件清单 → 自测验证步骤（含命令）→ 回归影响面分析。",
  });
  registerCommand("plan", {
    label: "/plan 方案先行",
    description: "强制生成多步骤执行方案，确认后再输出代码",
    directive:
      "【/plan 方案先行】\n" +
      "1. 本回合禁止直接输出最终代码，只产出多步骤执行方案：步骤拆解（含涉及文件）、关键技术决策、风险点与回滚方案、工作量预估。\n" +
      "2. 方案需明确每一步的输入/产出与验收标准。\n" +
      "3. 用户确认方案后，再在后续回合按方案逐步实现。",
  });
  registerCommand("test", {
    label: "/test 补测试",
    description: "补充正常/边界/异常三类单元测试并运行",
    directive:
      "【/test 测试驱动】\n" +
      "1. 为目标代码补充可独立运行的单元测试，覆盖：正常路径、边界值、异常分支三类用例。\n" +
      "2. 断言必须可验证，禁止空断言；测试文件与项目现有测试目录/框架保持一致。\n" +
      "3. 实际运行测试并贴出结果；失败用例先修代码而不是修改断言。",
  });
  registerCommand("review", {
    label: "/review 代码评审",
    description: "按正确性/安全/性能/可维护性四维分级评审",
    directive:
      "【/review 代码评审】\n" +
      "1. 按 正确性 / 安全性 / 性能 / 可维护性 四个维度逐文件审查，问题标注文件路径与行号。\n" +
      "2. 每个问题按 [阻塞] / [建议] / [提问] 分级，并给出具体修改方案。\n" +
      "3. 没有问题的维度也要显式给出「通过」结论，不得遗漏。",
  });
  registerCommand("refactor", {
    label: "/refactor 重构",
    description: "行为不变前提下重构，测试保持通过",
    directive:
      "【/refactor 安全重构】\n" +
      "1. 先列出现有代码的坏味道（重复/长函数/耦合/命名等）与对应的重构手法，经确认或逐项说明后再改。\n" +
      "2. 外部可观察行为必须保持不变；公开接口签名不得修改。\n" +
      "3. 每处重构后运行既有测试，全部通过才算完成；无测试时先补 Characterization Test。",
  });
  registerCommand("fix", {
    label: "/fix 缺陷修复",
    description: "复现→根因→最小修复→回归",
    directive:
      "【/fix 根因修复】\n" +
      "1. 先给出复现步骤与根因分析（定位到具体代码行），禁止未定位根因的猜测式修改。\n" +
      "2. 采用影响面最小的修复方案，不顺手重构无关代码。\n" +
      "3. 修复后补充回归测试或复现验证步骤，证明缺陷不再发生且未引入新问题。",
  });
  registerCommand("doc", {
    label: "/doc 文档补全",
    description: "只补文档与注释，不改可执行逻辑",
    directive:
      "【/doc 文档模式】\n" +
      "1. 只新增/修正 README、接口说明、函数注释与类型说明，不修改任何可执行代码逻辑。\n" +
      "2. 文档示例必须与当前代码行为一致，过期描述同步更新。\n" +
      "3. 使用与现有文档一致的语言与排版风格。",
  });
  registerCommand("explain", {
    label: "/explain 原理讲解",
    description: "由浅入深解释原理并给出示例",
    directive:
      "【/explain 讲解模式】\n" +
      "1. 按「是什么 → 解决什么问题 → 原理/执行流程 → 最小可运行示例 → 常见坑」结构讲解。\n" +
      "2. 术语首次出现时给出通俗解释，假设读者只具备基础编程知识。\n" +
      "3. 示例代码需可直接复制运行并标注运行环境。",
  });

  /* 解析输入中的 /指令（仅识别已注册指令；开头或空白后，排除 URL 中的 /path） */
  var COMMAND_TOKEN_RE = /(^|[\s，。;；])\/([a-z]{2,12})\b/g;

  function parseCommands(text) {
    var found = [];
    var unknown = [];
    if (!text) return { commands: found, unknownCommands: unknown, cleanText: "" };
    text = String(text);
    var clean = text.replace(COMMAND_TOKEN_RE, function (_, pre, name) {
      if (COMMANDS[name]) { found.push(name); return pre + " "; }
      unknown.push(name);
      return _;
    }).replace(/[ \t]{2,}/g, " ").replace(/^\s+|\s+$/g, "");
    return { commands: dedupe(found), unknownCommands: dedupe(unknown), cleanText: clean };
  }

  /* ============================================================
   * 二、口语化需求 → 意图识别
   * ============================================================ */
  var INTENTS = [
    { id: "fix", label: "缺陷修复", verbs: ["修", "bug", "报错", "崩", "异常", "失败", "不行了", "不工作", "不起作用", "挂了", "报错了", "错了", "失败了"] },
    { id: "review", label: "代码评审", verbs: ["评审", "review", "cr", "挑毛病", "把把关", "有没有问题", "审查", "检查", "分析"] },
    { id: "test", label: "补充测试", verbs: ["单测", "测试用例", "补测", "写测试", "覆盖率", "单元测试"] },
    { id: "explain", label: "原理解释", verbs: ["解释", "啥意思", "讲讲", "说说", "什么原理", "看不懂", "讲解", "为什么"] },
    { id: "refactor", label: "重构", verbs: ["重构", "优雅", "简洁一点", "整理下代码"] },
    { id: "optimize", label: "性能优化", verbs: ["优化", "太慢", "卡", "性能", "耗时", "提速", "内存", "泄漏"] },
    { id: "doc", label: "文档编写", verbs: ["文档", "注释", "readme", "说明"] },
    { id: "feature", label: "新增功能", verbs: ["加个", "新增", "增加", "添加", "支持", "接入", "上线"] },
    { id: "implement", label: "功能实现", verbs: ["写个", "实现", "做个", "搞个", "弄个", "写一个", "做一个", "帮我写", "来一个", "开发", "搭建", "封装"] },
  ];

  var TECH_KEYWORDS = [
    ["Python", /\b(python|py|django|flask|fastapi|pandas)\b/i],
    ["JavaScript", /\b(js|javascript|node|nodejs|npm)\b/i],
    ["TypeScript", /\b(ts|typescript)\b/i],
    ["Vue", /\b(vue|vue3|pinia|vuex)\b/i],
    ["React", /\b(react|jsx|tsx|hooks|redux)\b/i],
    ["Go", /\b(go|golang|go语言|gin|go\s*mod)\b/i],
    ["Java", /\b(java|spring|maven|pom)\b/i],
    ["Rust", /\brust\b/i],
    ["SQL", /\b(sql|mysql|postgres|查询语句)\b/i],
    ["CSS", /\b(css|样式|布局|flex|grid)\b/i],
  ];

  function detectIntent(text) {
    if (!text) return null;
    var lower = text.toLowerCase();
    for (var i = 0; i < INTENTS.length; i++) {
      for (var j = 0; j < INTENTS[i].verbs.length; j++) {
        var v = INTENTS[i].verbs[j];
        if (lower.indexOf(v.toLowerCase()) !== -1) {
          return { id: INTENTS[i].id, label: INTENTS[i].label, confidence: v.length >= 3 ? 0.9 : 0.6, matched: v };
        }
      }
    }
    return null;
  }

  function detectTechHints(text) {
    var hits = [];
    if (!text) return hits;
    TECH_KEYWORDS.forEach(function (pair) {
      if (pair[1].test(text) && hits.indexOf(pair[0]) === -1) hits.push(pair[0]);
    });
    return hits;
  }

  /* ============================================================
   * 三、常见代码约束库（缺失条件自动补充）
   * ============================================================ */
  var CONSTRAINT_LIB = {
    "参数校验": "参数校验：所有外部入参（函数参数 / 请求体 / 文件内容）必须做空值、类型与取值范围校验，非法输入抛出明确错误。",
    "异常处理": "异常处理：外部依赖（IO / 网络 / 子进程）必须 try/catch 并附带上下文信息，禁止吞异常；错误信息可定位问题。",
    "类型定义": "类型定义：补全函数签名、接口/结构体与返回值类型；TypeScript 禁止 any，Python 新增公开函数补类型注解。",
    "边界条件": "边界条件：显式处理空集合、零值、负数、超长输入与并发重入等边界场景。",
    "日志记录": "日志记录：关键路径与异常分支补充结构化日志（含关键标识），但禁止打印密钥与完整敏感数据。",
    "性能基线": "性能基线：改动后给出可对比的性能验证方式（计时/基准用例），证明优化有效且未引入退化。",
    "最小改动": "最小改动：只修改与本任务直接相关的代码，保持公开接口与既有行为不变。",
    "回归验证": "回归验证：补充/运行回归测试，证明问题消失且未引入新问题。",
    "安全校验": "安全校验：外部输入需防注入（SQL/命令/路径穿越/XSS），禁止硬编码密钥与令牌。",
  };

  var INTENT_CONSTRAINTS = {
    implement: ["参数校验", "异常处理", "类型定义", "边界条件"],
    feature: ["参数校验", "异常处理", "类型定义", "安全校验"],
    fix: ["最小改动", "回归验证", "异常处理"],
    optimize: ["性能基线", "回归验证", "边界条件"],
    refactor: ["最小改动", "回归验证"],
    review: [],
    test: ["边界条件", "异常处理"],
    explain: [],
    doc: [],
  };

  /* ============================================================
   * 四、输入错误自动修正（安全的错字表 + 口语压缩）
   * ============================================================ */
  var TYPO_MAP = {
    "接囗": "接口", "函救": "函数", "数剧库": "数据库", "配罝": "配置",
    "返囙": "返回", "代吗": "代码", "环竟": "环境", "部属": "部署",
    "重起": "重启", "报过错": "报错", "响影": "影响", "传参输": "传参",
    "异长": "异常", "线呈": "线程", "进成": "进程", "缓从": "缓存",
  };

  function correctTypos(text) {
    var corrections = [];
    var fixed = String(text || "");
    Object.keys(TYPO_MAP).forEach(function (wrong) {
      if (fixed.indexOf(wrong) !== -1) {
        fixed = fixed.split(wrong).join(TYPO_MAP[wrong]);
        corrections.push({ type: "typo", from: wrong, to: TYPO_MAP[wrong], message: "疑似错别字：「" + wrong + "」→「" + TYPO_MAP[wrong] + "」" });
      }
    });
    return { text: fixed, corrections: corrections };
  }

  /* ============================================================
   * 五、歧义识别 + 缺失条件自动补充
   * ============================================================ */
  var VAGUE_RE = /那个|这个|一下|随便|好像|应该|啥的|之类|弄下|搞下|差不多|看着办|有些|有时候/;
  var FILE_REF_RE = /[A-Za-z0-9_\-./\\]+\.(js|ts|tsx|jsx|vue|py|go|java|rs|json|html|css|md|yaml|yml|sh|sql)\b|当前文件|这个文件|该文件/;
  var ERROR_INFO_RE = /error|exception|traceback|报错信息|错误信息|异常栈|堆栈|日志里|控制台/i;

  function detectAmbiguities(text, intent, ctx) {
    var issues = [];
    ctx = ctx || {};
    if (!text) return issues;

    if (VAGUE_RE.test(text)) {
      issues.push({ type: "vague", level: "warn", message: "包含模糊指代词（这个/那个/一下/好像…），模型可能误判操作对象", suggestion: "明确目标文件、函数名或输入输出" });
    }
    if (intent && (intent.id === "fix" || intent.id === "optimize" || intent.id === "refactor" || intent.id === "review")) {
      if (!FILE_REF_RE.test(text) && !ctx.hasOpenFile && !ctx.hasSelection) {
        issues.push({ type: "missing-target", level: "warn", message: "未指明操作的文件/函数，且编辑器没有活动文件", suggestion: "使用 #file 路径 追加文件，或直接写明目标文件名" });
      }
    }
    if (intent && intent.id === "fix") {
      if (!ERROR_INFO_RE.test(text) && !ctx.hasLinter && !ctx.hasTerminal) {
        issues.push({ type: "missing-error", level: "warn", message: "缺陷描述缺少报错信息/堆栈，且无 Linter 与终端上下文", suggestion: "粘贴完整报错，或先在编辑器中复现使终端/Linter 捕获到错误" });
      }
    }
    if (intent && intent.id === "optimize" && !/[0-9]+\s*(ms|秒|qps|内存|mb|%)|耗时|指标/.test(text.toLowerCase())) {
      issues.push({ type: "missing-metric", level: "info", message: "性能问题缺少量化指标（当前耗时 / 目标耗时）", suggestion: "补充如「接口当前 800ms，期望 200ms 内」" });
    }
    if (intent && (intent.id === "implement" || intent.id === "feature") && !/输入|输出|参数|返回|给定|当.*时/.test(text)) {
      issues.push({ type: "missing-io", level: "info", message: "未描述函数/功能的输入与输出契约", suggestion: "补充输入参数与期望输出，系统已自动按通用契约兜底" });
    }
    return issues;
  }

  function buildMissingConstraints(intent, text) {
    var need = {};
    (INTENT_CONSTRAINTS[intent && intent.id] || []).forEach(function (k) { need[k] = CONSTRAINT_LIB[k]; });
    /* 口语中提到“安全/登录/权限/钱”等词时自动追加安全约束 */
    if (/安全|登录|鉴权|权限|密码|支付|金额|token|密钥/i.test(text)) need["安全校验"] = CONSTRAINT_LIB["安全校验"];
    return Object.keys(need).map(function (k) { return "- " + k + "：" + need[k]; });
  }

  /* ============================================================
   * 六、口语化 → 结构化工程指令（任务目标/技术栈/质量/格式/验证）
   * ============================================================ */
  function normalizeGoal(text, intent) {
    var goal = String(text || "").replace(/^\s*(请|麻烦|帮我|帮忙|能不能|可以|能否|老铁|大佬)[\s，,]*/g, "").trim();
    if (intent) {
      var prefix = {
        implement: "实现", feature: "新增", fix: "修复", optimize: "性能优化",
        refactor: "重构", test: "补充测试", review: "评审", explain: "讲解", doc: "编写文档",
      }[intent.id];
      if (prefix && goal.indexOf(prefix) !== 0) goal = prefix + "：" + goal;
    }
    return goal || "(用户未提供明确描述，请根据上下文判断)";
  }

  function buildStructuredTask(params) {
    params = params || {};
    var intent = params.intent;
    var techStack = params.techStack || "";
    var techHints = params.techHints || [];
    var constraints = params.constraints || [];
    var commandDirectives = params.commandDirectives || [];

    var sections = [];
    sections.push("【任务目标】\n" + normalizeGoal(params.cleanText, intent) +
      (intent ? "\n（意图识别：" + intent.label + "，置信度 " + intent.confidence + "）" : ""));

    var stackLine = techStack || (techHints.length ? "用户提及技术：" + techHints.join("、") : "未显式声明，以项目实际依赖与活动文件为准，禁止擅自更换技术栈");
    sections.push("【技术栈约束】\n" + stackLine);

    sections.push("【上下文依据】\n以上方「IDE 自动注入的项目上下文」为事实来源：活动文件、选中片段、Linter 报错与终端输出的优先级高于用户记忆描述；上下文未覆盖的信息列为待确认假设，不得编造。");

    if (commandDirectives.length) sections.push(commandDirectives.join("\n\n"));

    if (constraints.length) sections.push("【质量要求（自动补全的缺失条件）】\n" + constraints.join("\n"));

    sections.push("【输出格式】\n1) 先结论后细节；2) 代码块标注语言与文件路径；3) 多文件按文件分节给出完整改动；4) 命令与执行结果分块展示。");
    sections.push("【验证步骤】\n给出可复制执行的验证命令、预期结果，以及本次改动的回归影响范围。");

    return sections.join("\n\n");
  }

  /* ============================================================
   * 七、实时分析总入口（编辑器 oninput 调用，纯同步）
   * ============================================================ */
  function analyze(rawText, ctx) {
    ctx = ctx || {};
    var typoResult = correctTypos(rawText);
    var parsed = parseCommands(typoResult.text);
    var directives = PO.context ? PO.context.ContextDirectives.parse(parsed.cleanText) : [];
    var intent = detectIntent(parsed.cleanText);
    var techHints = detectTechHints(parsed.cleanText);
    var ambiguities = detectAmbiguities(parsed.cleanText, intent, ctx);
    var constraints = buildMissingConstraints(intent, parsed.cleanText);

    return {
      originalText: rawText || "",
      correctedText: typoResult.text,
      corrections: typoResult.corrections,
      commands: parsed.commands,
      unknownCommands: parsed.unknownCommands,
      directives: directives,
      intent: intent,
      techHints: techHints,
      ambiguities: ambiguities,
      missingConstraints: constraints,
      cleanText: parsed.cleanText,
      estimatedTokens: estimateTokens(rawText),
    };
  }

  /** 同步产出结构化任务（异步的 #指令解析由 optimizer 层负责） */
  function enhance(rawText, ctx) {
    var a = analyze(rawText, ctx);
    var commandDirectives = a.commands.map(function (name) { return COMMANDS[name].directive; });
    var structuredTask = buildStructuredTask({
      cleanText: a.cleanText,
      intent: a.intent,
      techStack: ctx.techStackText || "",
      techHints: a.techHints,
      constraints: a.missingConstraints,
      commandDirectives: commandDirectives,
    });
    return { analysis: a, structuredTask: structuredTask };
  }

  PO.enhancer = {
    registerCommand: registerCommand,
    listCommands: listCommands,
    parseCommands: parseCommands,
    detectIntent: detectIntent,
    detectTechHints: detectTechHints,
    correctTypos: correctTypos,
    detectAmbiguities: detectAmbiguities,
    buildMissingConstraints: buildMissingConstraints,
    buildStructuredTask: buildStructuredTask,
    normalizeGoal: normalizeGoal,
    analyze: analyze,
    enhance: enhance,
    CONSTRAINT_LIB: CONSTRAINT_LIB,
  };
})(typeof window !== "undefined" ? window : globalThis);
