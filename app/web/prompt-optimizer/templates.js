/* ============================================================
 * 提示词优化系统 · 第一层（底层）：模型专属预设模板系统（静默生效）
 * ------------------------------------------------------------
 *  - Seed / GLM / Claude / GPT / DeepSeek 五大主流模型族独立模板
 *  - 根据当前使用模型名称自动匹配对应优化模板
 *  - 代码场景默认超参数：temperature=0.1, top_p=0.9
 *  - 零依赖：浏览器 / Node 均可直接加载
 * ============================================================ */
(function (global) {
  "use strict";

  var PO = global.PO || (global.PO = {});

  /* 代码场景默认超参数：低发散、高稳定 */
  PO.DEFAULT_HYPERPARAMS = Object.freeze({
    temperature: 0.1,
    top_p: 0.9,
  });

  /* ------------------------------------------------------------
   * 各模型族的差异化指令（需求书第一条第 4 小点）
   * ------------------------------------------------------------ */
  var FAMILY_DIRECTIVES = {
    deepseek: [
      "【工具调用规范】每次调用工具必须输出严格合法的 JSON：{\"name\": 工具名, \"arguments\": {...}}，禁止输出注释、尾随逗号或多余说明文字。",
      "【工具调用规范】调用前先用一句话说明调用意图；收到工具返回后先观察结果再决定下一步，禁止凭空假设返回内容。",
      "【长上下文处理】跨文件/长代码任务中，统一使用「文件路径 + 行号」引用代码，超过 500 行的改动必须分文件、分模块组织输出。",
      "【多轮连贯】多轮对话中严格延续上一轮的任务计划与命名约定，未完成项在每轮思考开头显式回顾，禁止中途更换技术方案。",
    ],
    seed: [
      "【工具调用规范】工具调用必须使用标准 function call 结构（name + arguments JSON），arguments 字段类型必须与工具 schema 完全一致。",
      "【工具调用规范】一次只发起一个工具调用，待结果返回后再继续；文件类工具的路径参数统一使用相对工作区根目录的正斜杠路径。",
      "【结果校验】工具返回错误时，依据错误信息修正参数后重试，同一错误连续出现 2 次必须停止并向用户说明。",
    ],
    glm: [
      "【终端操作约束】所有终端命令必须放在 ```bash 代码块中，命令需可在 bash/zsh 环境直接执行，Windows 专属命令需单独标注。",
      "【脚本输出约束】终端/脚本输出必须如实引用，禁止伪造或臆测执行结果；没有实际执行时必须明确声明「未执行」。",
      "【脚本输出约束】涉及破坏性命令（rm、drop、强制覆盖）时，先输出命令影响范围说明，等待用户确认后再执行。",
    ],
    claude: [
      "【长上下文处理】优先复用上下文中已有的类型、函数与导入，避免重复定义；引用代码时给出文件路径与起止行号。",
      "【长上下文处理】大范围改动前先输出受影响文件清单与改动顺序，逐文件落地，保证跨文件接口签名一致。",
      "【长上下文处理】当上下文接近模型窗口上限时，主动提示用户并优先保留：当前任务目标 > 活动文件 > 相关类型定义 > 历史对话。",
    ],
    gpt: [
      "【多轮连贯】每轮回复先回顾未完成的任务与上一轮结论，再继续执行；用户追问默认承接当前文件与技术方案。",
      "【多轮连贯】跨轮次保持同一套命名风格、错误处理策略与目录结构，新提出的假设必须显式标注「假设」。",
      "【多轮连贯】任务较长时分阶段输出，每阶段结束给出「下一步建议」，由用户确认后继续。",
    ],
    general: [
      "【通用约束】工具调用使用严格 JSON（name + arguments）；终端命令放入代码块并如实反馈输出。",
      "【通用约束】引用代码给出文件路径；长任务先给方案再分步实现；跨轮次保持方案与风格一致。",
    ],
  };

  /* ------------------------------------------------------------
   * 模板定义：标准化角色设定 / 输出规范 / 代码质量要求 / 格式约束
   * ------------------------------------------------------------ */
  function buildTemplate(family, label, matchKeywords) {
    return {
      family: family,
      label: label,
      matchKeywords: matchKeywords,
      hyperparams: PO.DEFAULT_HYPERPARAMS,
      role:
        "你是一名资深代码工程师，服务于 IDE 内的代码场景。你需要依据用户给出的工程化指令与项目上下文，" +
        "完成代码编写、缺陷修复、重构、测试、评审与解释工作。所有结论必须基于上下文中的真实代码，" +
        "禁止臆造不存在的 API、文件或依赖。",
      outputRules: [
        "使用中文回复，先给结论/方案，再给细节；与任务无关的内容一律省略。",
        "代码块必须标注语言与目标文件路径（如 ```js // src/a.js），禁止提交无法运行的伪代码。",
        "优先给出可直接落地的最小完整改动；修改既有代码时保持原有缩进、命名与整体风格。",
        "不确定的需求显式列为「待确认假设」，不要默默扩大改动范围。",
      ],
      qualityRules: [
        "正确性：覆盖正常路径、边界值与异常分支；函数有明确的前置/后置条件。",
        "健壮性：入参校验、异常捕获与错误信息齐备；外部调用必须处理失败与超时。",
        "可维护性：单一职责、命名达意、复杂逻辑附带注释；避免重复代码。",
        "类型安全：静态类型语言补全类型定义；动态语言在接口边界做运行时校验。",
        "安全性：禁止硬编码密钥；校验外部输入；路径、SQL、命令注入风险必须规避。",
        "向后兼容：不破坏既有公开接口签名；必须破坏时给出迁移说明。",
      ],
      formatRules: [
        "多文件改动按文件分节，每节标题为文件路径，节内给出完整代码块或最小差异说明。",
        "终端命令与执行结果分别用代码块展示；清单类内容使用编号列表。",
        "结尾固定输出「验证步骤」小节：如何运行、如何确认改动生效、回归影响范围。",
      ],
      familyDirectives: FAMILY_DIRECTIVES[family] || FAMILY_DIRECTIVES.general,
    };
  }

  PO.TEMPLATES = {
    seed: buildTemplate("seed", "Seed（豆包/字节）系列", ["seed", "doubao", "bytedance", "skylark", "豆包"]),
    glm: buildTemplate("glm", "GLM（智谱）系列", ["glm", "chatglm", "zhipu", "bigmodel", "智谱"]),
    claude: buildTemplate("claude", "Claude（Anthropic）系列", ["claude", "anthropic", "opus", "sonnet", "haiku"]),
    gpt: buildTemplate("gpt", "GPT（OpenAI）系列", ["gpt", "openai", "chatgpt", "o1-", "o3-", "o4-", "gpt-"]),
    deepseek: buildTemplate("deepseek", "DeepSeek 系列", ["deepseek", "deep-seek", "深度求索"]),
    general: buildTemplate("general", "通用模型（兜底）", []),
  };

  /* ------------------------------------------------------------
   * 模型自动匹配：根据当前使用模型名称动态加载对应优化模板
   * 命中规则：模型名小写后包含任一关键词即命中；无命中回落 general
   * ------------------------------------------------------------ */
  function matchModel(modelName) {
    if (!modelName || typeof modelName !== "string") return "general";
    var name = modelName.toLowerCase();
    var families = ["deepseek", "seed", "glm", "claude", "gpt"];
    for (var i = 0; i < families.length; i++) {
      var kws = PO.TEMPLATES[families[i]].matchKeywords;
      for (var j = 0; j < kws.length; j++) {
        if (name.indexOf(kws[j]) !== -1) return families[i];
      }
    }
    return "general";
  }

  function getTemplate(familyOrModel) {
    var family = PO.TEMPLATES[familyOrModel] ? familyOrModel : matchModel(familyOrModel);
    return PO.TEMPLATES[family] || PO.TEMPLATES.general;
  }

  function listFamilies() {
    return Object.keys(PO.TEMPLATES);
  }

  /* ------------------------------------------------------------
   * 渲染系统提示词（静默拼入，不暴露给用户输入框）
   * @param {string} familyOrModel 模型族名或完整模型名
   * @param {object} [meta] { scene, techStack, extra }
   * ------------------------------------------------------------ */
  function renderSystemPrompt(familyOrModel, meta) {
    meta = meta || {};
    var tpl = getTemplate(familyOrModel);
    var lines = [];

    lines.push("# 角色设定");
    lines.push(tpl.role);
    lines.push("");

    lines.push("# 场景");
    lines.push("- 场景类型：" + (meta.scene || "IDE 内代码工程任务"));
    if (meta.techStack) {
      lines.push("- 项目技术栈：" + meta.techStack);
    }
    lines.push("- 采样超参数：temperature=" + tpl.hyperparams.temperature +
      "，top_p=" + tpl.hyperparams.top_p + "（低发散、高稳定，禁止随意提高）");
    lines.push("");

    lines.push("# 输出规范");
    tpl.outputRules.forEach(function (r, i) { lines.push((i + 1) + ". " + r); });
    lines.push("");

    lines.push("# 代码质量要求");
    tpl.qualityRules.forEach(function (r, i) { lines.push((i + 1) + ". " + r); });
    lines.push("");

    lines.push("# 格式约束");
    tpl.formatRules.forEach(function (r, i) { lines.push((i + 1) + ". " + r); });
    lines.push("");

    lines.push("# " + tpl.label + " 专属优化指令");
    tpl.familyDirectives.forEach(function (r, i) { lines.push((i + 1) + ". " + r); });

    if (meta.extra) {
      lines.push("");
      lines.push(meta.extra);
    }
    return lines.join("\n");
  }

  /** 便捷入口：给完整模型名，返回族名 + 系统提示词 + 超参数 */
  function buildSystemPrompt(modelName, meta) {
    var family = matchModel(modelName);
    return {
      family: family,
      label: PO.TEMPLATES[family].label,
      hyperparams: PO.TEMPLATES[family].hyperparams,
      systemPrompt: renderSystemPrompt(family, meta),
    };
  }

  PO.templates = {
    matchModel: matchModel,
    getTemplate: getTemplate,
    listFamilies: listFamilies,
    renderSystemPrompt: renderSystemPrompt,
    buildSystemPrompt: buildSystemPrompt,
  };
})(typeof window !== "undefined" ? window : globalThis);
