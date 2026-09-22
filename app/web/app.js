/* ============================================================
   Craftbase 智能体工作台 · 前端逻辑（原生 JavaScript，零框架依赖）
   模块：视图路由 / API 封装 / SSE 流式对话 / Markdown 渲染 /
        项目与任务 / 模型供应商管理 / 设置中心 / 文件与 RAG
   ============================================================ */
"use strict";

/* ---------------- 全局状态 ---------------- */
const state = {
  sessions: [],
  currentSessionId: null,
  currentProjectId: null,
  projects: [],
  runtime: null,
  health: null,
  browsePath: "",
  sending: false,
  abortCtrl: null,
  currentPage: "model",
  cwd: ".",
  currentFile: null,
  ragOk: false,
  reasoningLevel: "medium",
  execMode: "auto_workspace",
  execModes: null,
  activeMode: "office",
  agentRole: "fullstack",
  sidebarPanel: "projects",
  fileTreePath: ".",
  showArchivedTasks: false,
  taskFilterUnread: false,
  ctxTarget: null,
  prefs: { language: "zh", theme: "light", mode: "standard", zoom: "1", accent: "orange", loggedIn: true },
};

const TASK_META_KEY = "craftbase.taskMeta";

const AGENT_ROLES = {
  fullstack: { zh: "全栈工程师", en: "Full-stack Engineer", letter: "A" },
  architect: { zh: "架构师", en: "Architect", letter: "R" },
  reviewer: { zh: "代码评审", en: "Code Reviewer", letter: "V" },
};

const SLASH_COMMANDS = [
  { cmd: "/周报", desc: "生成周报总结", prompt: "帮我生成本周的工作总结，按项目维度梳理进展与风险" },
  { cmd: "/修 Bug", desc: "排查并修复错误", prompt: "分析下面的报错信息，定位原因并给出修复方案" },
  { cmd: "/检索", desc: "RAG 源码检索", prompt: "检索项目源码并解释相关实现" },
  { cmd: "/索引", desc: "增量索引知识库", action: "rag-index" },
  { cmd: "/清空", desc: "清空当前对话", action: "clear-chat" },
];

const PREFS_KEY = "craftbase.prefs";

const I18N = {
  zh: {
    "nav.newTask": "新建任务", "nav.search": "搜索", "nav.knowledge": "知识库", "nav.skills": "技能与连接器",
    "nav.groups": "# 分组", "nav.projects": "项目", "nav.fileTree": "项目文件",
    "nav.projectsLabel": "项目", "nav.tasks": "任务",
    "nav.noTasks": "还没有任务", "nav.addProject": "添加本机项目", "nav.backWorkspace": "返回工作区",
    "nav.expandSidebar": "展开侧边栏", "nav.filterTasks": "筛选未读", "nav.archivedTasks": "已归档",
    "ctx.pin": "置顶任务", "ctx.rename": "重命名任务", "ctx.archive": "归档任务", "ctx.unread": "标记为未读",
    "ctx.split": "在分屏打开", "ctx.explorer": "在资源管理器中打开", "ctx.copyPath": "复制路径",
    "ctx.copyTaskPath": "复制任务路径", "ctx.copyLogPath": "复制日志路径", "ctx.copySession": "复制会话 ID",
    "ctx.config": "前往配置", "ctx.viewTrace": "查看调用轨迹", "ctx.feedback": "反馈问题",
    "brand.tagline": "Craftbase，我帮你",
    "mode.office": "日常办公", "mode.code": "代码开发", "mode.design": "设计创意",
    "chain.fs": "文件", "chain.rag": "知识库", "chain.agent": "循环",
    "chain.ragIdle": "知识库待索引", "chain.ragReady": "知识库已就绪",
    "composer.role": "全栈工程师",
    "set.group.basic": "基础设置", "set.group.agent": "Agent 能力", "set.group.data": "数据与统计",
    "set.general": "常规", "set.appearance": "外观", "set.model": "模型设置", "set.browser": "浏览器控制",
    "set.computer": "电脑控制", "set.shortcuts": "键盘快捷键", "set.memory": "记忆", "set.subagents": "子智能体",
    "set.plugins": "插件", "set.mcp": "MCP 服务器", "set.skills": "技能", "set.commands": "命令",
    "set.hooks": "钩子", "set.stats": "使用统计", "set.guide": "引导",
    "popup.settings": "设置中心", "popup.language": "界面语言", "popup.theme": "界面主题",
    "popup.mode": "界面模式", "popup.zoom": "界面缩放", "popup.stats": "使用统计",
    "popup.upgrade": "升级", "popup.invite": "邀请好友", "popup.reward": "奖励", "popup.disconnect": "断开连接",
    "fly.lang.zh": "简体中文", "fly.lang.en": "English",
    "fly.theme.system": "系统默认", "fly.theme.dark": "深色主题", "fly.theme.light": "浅色主题",
    "fly.mode.standard": "标准模式", "fly.mode.focus": "专注模式", "fly.mode.mini": "极简模式",
    "greeting.morning": "早上好呀，新的一天开始啦", "greeting.noon": "中午好呀，稍作休息吧",
    "greeting.afternoon": "下午好呀，继续加油", "greeting.evening": "晚上好呀，辛苦了",
    "composer.placeholder": "今天帮你做些什么？@ 引用文件与对话，/ 调用技能与指令",
    "composer.hint": "AI 生成内容仅供参考；添加本机项目后可读写桌面、D 盘等目录（高权限操作需确认）",
    "composer.localGranted": "本机已授权", "composer.localRestricted": "受限访问",
    "composer.computer": "电脑操作", "composer.send": "发送", "composer.stop": "停止",
    "exec.plan": "计划模式", "exec.confirm": "变更前确认", "exec.auto": "自动编辑", "exec.full": "完全访问",
    "exec.planHint": "编辑前先出计划。", "exec.confirmHint": "改文件前先问我。",
    "exec.autoHint": "自动编辑文件。", "exec.fullHint": "减少确认次数。",
    "toast.execMode": "执行档已切换为 {n}",
    "quick.weekly": "周报总结", "quick.fix": "报错修复", "quick.ppt": "PPT 制作",
    "quick.idle": "闲时任务", "quick.rag": "源码检索", "quick.design": "界面设计",
    "account.name": "旅行者6172", "account.guest": "未登录", "account.pro": "Pro",
    "account.menu": "账户菜单", "account.settings": "设置中心",
    "chat.task": "任务", "chat.back": "返回", "chat.new": "新建任务",
    "toast.lang": "界面语言已切换", "toast.theme": "主题已切换", "toast.mode": "界面模式已切换",
    "toast.zoom": "缩放已调整为 {n}%", "toast.upgrade": "当前已是最新版本 ✓",
    "toast.invite": "邀请链接已复制，快去分享给好友吧～", "toast.disconnect": "已断开连接",
    "toast.reconnect": "请先登录账户", "toast.accent": "强调色已更换",
    "page.stats.title": "使用统计", "page.stats.desc": "了解你与智能体的协作情况。",
    "page.stats.tasks": "累计任务数", "page.stats.messages": "对话消息数",
    "page.stats.tools": "工具调用次数", "page.stats.hours": "累计使用时长",
    "page.stats.tokens": "累计 Token", "page.stats.reasoning": "推理 Token",
    "page.stats.trend": "近 7 天对话趋势",
    "page.stats.byLevel": "按思考强度分布",
    "page.stats.loading": "加载统计中…",
    "page.stats.loadFail": "统计加载失败",
    "page.appearance.title": "外观", "page.appearance.desc": "调整主题、缩放、字体与强调色，打造你喜欢的界面。",
    "page.appearance.theme": "界面主题", "page.appearance.themeMode": "主题模式",
    "page.appearance.zoom": "界面缩放", "page.appearance.zoomRatio": "缩放比例",
    "theme.light": "浅色", "theme.dark": "深色", "theme.system": "跟随系统",
    "update.downloading": "正在下载更新… 20.6MB / 72.9MB",
    "day.mon": "一", "day.tue": "二", "day.wed": "三", "day.thu": "四",
    "day.fri": "五", "day.sat": "六", "day.sun": "日",
  },
  en: {
    "nav.newTask": "New Task", "nav.search": "Search", "nav.knowledge": "Knowledge", "nav.skills": "Skills & Connectors",
    "nav.groups": "# Groups", "nav.projects": "Projects", "nav.fileTree": "Project Files",
    "nav.projectsLabel": "Projects", "nav.tasks": "Tasks",
    "nav.noTasks": "No tasks yet", "nav.addProject": "Add Local Project", "nav.backWorkspace": "Back to Workspace",
    "nav.expandSidebar": "Expand sidebar", "nav.filterTasks": "Filter unread", "nav.archivedTasks": "Archived",
    "ctx.pin": "Pin task", "ctx.rename": "Rename task", "ctx.archive": "Archive task", "ctx.unread": "Mark unread",
    "ctx.split": "Open in split", "ctx.explorer": "Open in Explorer", "ctx.copyPath": "Copy path",
    "ctx.copyTaskPath": "Copy task path", "ctx.copyLogPath": "Copy log path", "ctx.copySession": "Copy session ID",
    "ctx.config": "Go to settings", "ctx.viewTrace": "View call trace", "ctx.feedback": "Send feedback",
    "brand.tagline": "Craftbase, at your service",
    "mode.office": "Office", "mode.code": "Development", "mode.design": "Design",
    "chain.fs": "Files", "chain.rag": "RAG", "chain.agent": "Loop",
    "chain.ragIdle": "Index pending", "chain.ragReady": "Knowledge ready",
    "composer.role": "Full-stack Engineer",
    "set.group.basic": "Basic", "set.group.agent": "Agent", "set.group.data": "Data & Stats",
    "set.general": "General", "set.appearance": "Appearance", "set.model": "Model Settings", "set.browser": "Browser Control",
    "set.computer": "Computer Control", "set.shortcuts": "Keyboard Shortcuts", "set.memory": "Memory", "set.subagents": "Sub-agents",
    "set.plugins": "Plugins", "set.mcp": "MCP Servers", "set.skills": "Skills", "set.commands": "Commands",
    "set.hooks": "Hooks", "set.stats": "Usage Stats", "set.guide": "Guide",
    "popup.settings": "Settings", "popup.language": "Language", "popup.theme": "Theme",
    "popup.mode": "Display Mode", "popup.zoom": "Zoom", "popup.stats": "Usage Stats",
    "popup.upgrade": "Upgrade", "popup.invite": "Invite Friends", "popup.reward": "Reward", "popup.disconnect": "Sign Out",
    "fly.lang.zh": "简体中文", "fly.lang.en": "English",
    "fly.theme.system": "System Default", "fly.theme.dark": "Dark Theme", "fly.theme.light": "Light Theme",
    "fly.mode.standard": "Standard", "fly.mode.focus": "Focus", "fly.mode.mini": "Minimal",
    "greeting.morning": "Good morning — let's make today count", "greeting.noon": "Good afternoon — take a breather",
    "greeting.afternoon": "Good afternoon — keep going", "greeting.evening": "Good evening — great work today",
    "composer.placeholder": "What can I help with today? @ files & context, / skills & commands.",
    "composer.hint": "AI output is for reference only. Add a local project to access files (high-risk actions require approval).",
    "composer.localGranted": "Local authorized", "composer.localRestricted": "Restricted",
    "composer.computer": "Computer", "composer.send": "Send", "composer.stop": "Stop",
    "exec.plan": "Plan", "exec.confirm": "Ask before edits", "exec.auto": "Auto-edit", "exec.full": "Full access",
    "exec.planHint": "Plan before editing.", "exec.confirmHint": "Ask before changing files.",
    "exec.autoHint": "Edit files automatically.", "exec.fullHint": "Fewer confirmations.",
    "toast.execMode": "Exec mode set to {n}",
    "quick.weekly": "Weekly Report", "quick.fix": "Fix Errors", "quick.ppt": "Create PPT",
    "quick.idle": "Idle Tasks", "quick.rag": "Code Search", "quick.design": "UI Design",
    "account.name": "Traveler 6172", "account.guest": "Signed Out", "account.pro": "Pro",
    "account.menu": "Account menu", "account.settings": "Settings",
    "chat.task": "Task", "chat.back": "Back", "chat.new": "New Task",
    "toast.lang": "Language updated", "toast.theme": "Theme updated", "toast.mode": "Display mode updated",
    "toast.zoom": "Zoom set to {n}%", "toast.upgrade": "You're on the latest version ✓",
    "toast.invite": "Invite link copied — share it with friends!", "toast.disconnect": "Signed out",
    "toast.reconnect": "Please sign in first", "toast.accent": "Accent color updated",
    "page.stats.title": "Usage Stats", "page.stats.desc": "See how you collaborate with the agent.",
    "page.stats.tasks": "Total Tasks", "page.stats.messages": "Messages", "page.stats.tools": "Tool Calls",
    "page.stats.hours": "Total Hours", "page.stats.tokens": "Total Tokens",
    "page.stats.reasoning": "Reasoning Tokens", "page.stats.trend": "Chats — Last 7 Days",
    "page.stats.byLevel": "By Reasoning Level", "page.stats.loading": "Loading stats…",
    "page.stats.loadFail": "Failed to load stats",
    "page.appearance.title": "Appearance", "page.appearance.desc": "Customize theme, zoom, and accent colors.",
    "page.appearance.theme": "Theme", "page.appearance.themeMode": "Theme Mode",
    "page.appearance.zoom": "Zoom", "page.appearance.zoomRatio": "Scale",
    "theme.light": "Light", "theme.dark": "Dark", "theme.system": "System",
    "update.downloading": "Downloading update… 20.6MB / 72.9MB",
    "day.mon": "Mon", "day.tue": "Tue", "day.wed": "Wed", "day.thu": "Thu",
    "day.fri": "Fri", "day.sat": "Sat", "day.sun": "Sun",
  },
};

function t(key, vars = {}) {
  const lang = state.prefs.language === "en" ? "en" : "zh";
  let text = I18N[lang][key] ?? I18N.zh[key] ?? key;
  Object.entries(vars).forEach(([k, v]) => { text = text.replace(`{${k}}`, v); });
  return text;
}

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (raw) Object.assign(state.prefs, JSON.parse(raw));
  } catch (_) { /* ignore corrupt prefs */ }
}

function savePrefs() {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(state.prefs)); } catch (_) { /* ignore */ }
}

function applyI18n() {
  document.documentElement.lang = state.prefs.language === "en" ? "en" : "zh-CN";
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  $$("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  setGreeting();
  updateAccountDock();
  updateAgentRoleLabel();
  updateExecModeUI();
  if ($("#view-settings").classList.contains("active")) renderPage(state.currentPage);
}

function updateAccountDock() {
  const loggedIn = state.prefs.loggedIn !== false;
  const card = $("#user-card");
  if (card) {
    const nameEl = card.querySelector(".account-name");
    const badge = card.querySelector(".account-badge");
    const avatar = card.querySelector(".account-avatar");
    if (nameEl) nameEl.textContent = loggedIn ? t("account.name") : t("account.guest");
    if (badge) { badge.textContent = t("account.pro"); badge.hidden = !loggedIn; }
    if (avatar) avatar.textContent = loggedIn ? (state.prefs.language === "en" ? "T" : "旅") : "?";
  }
  const foot = $(".set-user-foot");
  if (foot) {
    const fn = foot.querySelector(".account-name");
    const fb = foot.querySelector(".account-badge");
    const fa = foot.querySelector(".account-avatar");
    if (fn) fn.textContent = loggedIn ? t("account.name") : t("account.guest");
    if (fb) { fb.textContent = t("account.pro"); fb.hidden = !loggedIn; }
    if (fa) fa.textContent = loggedIn ? (state.prefs.language === "en" ? "T" : "旅") : "?";
  }
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* ---------------- 基础工具 ---------------- */
async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try { detail = (await resp.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(detail);
  }
  return resp.json();
}

let toastTimer = null;
function toast(message, type = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (type ? ` ${type}` : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

function relTime(ts) {
  if (!ts) return "";
  const d = (Date.now() - ts * 1000) / 1000;
  if (d < 3600) return Math.max(1, Math.round(d / 60)) + "分钟";
  if (d < 86400) return Math.round(d / 3600) + "小时";
  if (d < 86400 * 30) return Math.round(d / 86400) + "天";
  if (d < 86400 * 365) return Math.round(d / 86400 / 30) + "个月";
  return Math.round(d / 86400 / 365) + "年";
}

function loadTaskMeta() {
  try { return JSON.parse(localStorage.getItem(TASK_META_KEY) || "{}"); }
  catch (_) { return {}; }
}

function saveTaskMeta(meta) {
  try { localStorage.setItem(TASK_META_KEY, JSON.stringify(meta)); } catch (_) { /* ignore */ }
}

function getTaskMeta(sessionId) {
  const meta = loadTaskMeta();
  return meta[sessionId] || { pinned: false, archived: false, unread: false };
}

function setTaskMeta(sessionId, patch) {
  const meta = loadTaskMeta();
  meta[sessionId] = { ...getTaskMeta(sessionId), ...patch };
  saveTaskMeta(meta);
}

function projectSourceLabel(path) {
  if (!path) return "本地项目";
  const norm = path.replace(/\\/g, "/");
  if (/^[a-z]:\//i.test(norm) || norm.includes("/Users/") || norm.includes("/home/")) {
    return "本地电脑…";
  }
  const name = norm.split("/").filter(Boolean).pop();
  return name ? `${name}…` : "本地项目";
}

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    toast(okMsg || "已复制", "ok");
  } catch (_) {
    toast("复制失败", "error");
  }
}

function closeCtxMenu() {
  const menu = $("#task-ctx-menu");
  if (menu) {
    menu.hidden = true;
    menu.setAttribute("aria-hidden", "true");
  }
  state.ctxTarget = null;
}

function showCtxMenu(x, y, items, onPick) {
  const menu = $("#task-ctx-menu");
  if (!menu) return;
  menu.innerHTML = items.map((item, i) => {
    if (item.sep) return `<div class="ctx-sep" role="separator"></div>`;
    return `<button type="button" class="ctx-item${item.danger ? " danger" : ""}" data-idx="${i}" role="menuitem">${escapeHtml(item.label)}</button>`;
  }).join("");
  menu.hidden = false;
  menu.setAttribute("aria-hidden", "false");
  const rect = menu.getBoundingClientRect();
  const left = Math.min(x, window.innerWidth - (rect.width || 220) - 8);
  const top = Math.min(y, window.innerHeight - 320);
  menu.style.left = `${Math.max(8, left)}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  menu.querySelectorAll(".ctx-item").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      const idx = Number(el.dataset.idx);
      closeCtxMenu();
      onPick(items[idx]);
    });
  });
}

function taskDataPath(sessionId) {
  const db = state.health?.storage?.history_db || state.runtime?.storage?.history_db || "./data/history.db";
  return `${db}#session:${sessionId}`;
}

function logDataPath() {
  return state.health?.storage?.history_db || state.runtime?.storage?.history_db || "./data/history.db";
}

/* ============================================================
   轻量 Markdown 渲染 + 代码语法高亮（零第三方库）
   ============================================================ */
const KEYWORDS = new Set([
  "def", "class", "return", "if", "elif", "else", "for", "while", "import",
  "from", "as", "in", "is", "not", "and", "or", "None", "True", "False",
  "async", "await", "try", "except", "finally", "raise", "with", "lambda",
  "pass", "break", "continue", "global", "nonlocal", "yield", "assert",
  "function", "var", "let", "const", "this", "new", "typeof", "instanceof",
  "public", "private", "protected", "static", "void", "int", "str", "float",
  "bool", "package", "interface", "extends", "implements", "func", "struct",
  "switch", "case", "default", "sizeof", "enum", "namespace", "using",
]);
const HASH_LANGS = new Set(["py", "python", "sh", "bash", "yaml", "yml", "toml", "ruby", "rb", "dockerfile"]);
const SLASH_LANGS = new Set(["js", "javascript", "ts", "typescript", "jsx", "tsx",
  "java", "go", "c", "h", "cpp", "c++", "cc", "css", "jsonc", "swift", "kt"]);

function highlightCode(escaped, lang) {
  if (!lang) return escaped;
  const l = lang.toLowerCase();
  const tokenRe =
    /(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^"\\\n])*'|#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
  return escaped.replace(tokenRe, (m) => {
    const c0 = m[0];
    if (c0 === '"' || c0 === "'") return `<span class="tok-str">${m}</span>`;
    if (c0 === "#" && HASH_LANGS.has(l)) return `<span class="tok-com">${m}</span>`;
    if ((m.startsWith("//") || m.startsWith("/*")) && SLASH_LANGS.has(l))
      return `<span class="tok-com">${m}</span>`;
    if (/^\d/.test(m)) return `<span class="tok-num">${m}</span>`;
    if (KEYWORDS.has(m)) return `<span class="tok-kw">${m}</span>`;
    return m;
  });
}

function renderCodeBlockHtml(lang, code) {
  const lineCount = code.replace(/\n$/, "").split("\n").length;
  const collapsed = lineCount > 18 ? " collapsed" : "";
  const highlighted = highlightCode(escapeHtml(code), lang);
  return `<div class="code-block${collapsed}">
    <div class="code-block-head">
      <span class="code-lang">${escapeHtml(lang) || "text"}</span>
      <div class="code-actions">
        <button data-act="copy" type="button">复制</button>
        <button data-act="toggle" type="button">${collapsed ? "展开" : "折叠"}</button>
      </div>
    </div>
    <pre><code>${highlighted}</code></pre>
    <div class="code-expand-tip">▾ 点击展开全部（${lineCount} 行）</div>
  </div>`;
}

function inlineFormat(html) {
  const codes = [];
  html = html.replace(/`([^`\n]+)`/g, (_, code) => {
    codes.push(code);
    return `\u0001C${codes.length - 1}\u0001`;
  });
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => {
    if (/^(https?:\/\/|\/|#)/.test(url)) {
      return `<a href="${url}" target="_blank" rel="noopener noreferrer">${text}</a>`;
    }
    return m;
  });
  html = html.replace(/\u0001C(\d+)\u0001/g, (_, i) => `<code>${codes[i]}</code>`);
  return html;
}

function blockFormat(html) {
  const lines = html.split("\n");
  const out = [];
  let i = 0;
  const flushList = (items, ordered) => {
    const tag = ordered ? "ol" : "ul";
    out.push(`<${tag}>` + items.map((t) => `<li>${t}</li>`).join("") + `</${tag}>`);
  };
  while (i < lines.length) {
    const ln = lines[i];
    if (!ln.trim()) { i++; continue; }

    let m;
    if ((m = ln.match(/^(#{1,4})\s+(.*)$/))) {
      const lv = m[1].length;
      out.push(`<h${lv}>${m[2]}</h${lv}>`); i++; continue;
    }
    if (/^\s*([-*] )/.test(ln)) {
      const items = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) {
        items.push(inlineFormat(lines[i].replace(/^\s*[-*] /, ""))); i++;
      }
      flushList(items, false); continue;
    }
    if (/^\s*\d+\.\s+/.test(ln)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(inlineFormat(lines[i].replace(/^\s*\d+\.\s+/, ""))); i++;
      }
      flushList(items, true); continue;
    }
    if (/^&gt;\s?/.test(ln)) {
      const buf = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^&gt;\s?/, "")); i++;
      }
      out.push(`<blockquote>${buf.map(inlineFormat).join("<br>")}</blockquote>`);
      continue;
    }
    if (/^\s*-{3,}$/.test(ln)) { out.push("<hr>"); i++; continue; }

    const buf = [];
    while (i < lines.length && lines[i].trim() &&
      !/^(#{1,4}\s|\s*[-*] |\s*\d+\.\s|&gt;|\s*-{3,}$)/.test(lines[i])) {
      buf.push(lines[i]); i++;
    }
    out.push(`<p>${buf.map(inlineFormat).join("<br>")}</p>`);
  }
  return out.join("\n");
}

function renderMarkdown(text) {
  const blocks = [];
  let held = text.replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$(?![\s\S]))/g, (_, lang, code) => {
    blocks.push({ lang: lang.trim(), code: code.replace(/\n$/, "") });
    return `\u0001B${blocks.length - 1}\u0001`;
  });
  let html = blockFormat(escapeHtml(held));
  html = html.replace(/\u0001B(\d+)\u0001/g, (_, i) =>
    renderCodeBlockHtml(blocks[i].lang, blocks[i].code));
  return html;
}

/* ============================================================
   视图路由
   ============================================================ */
const composerWrap = $(".composer-wrap");

function showWorkspace() {
  $("#view-settings").classList.remove("active");
  $("#view-workspace").classList.add("active");
}
function showSettings(page) {
  closePopup();
  $("#view-workspace").classList.remove("active");
  $("#view-settings").classList.add("active");
  renderPage(page || state.currentPage);
}

function showChatView() {
  $("#welcome-pane").hidden = true;
  $("#chat-pane").hidden = false;
  $("#chat-composer-slot").appendChild(composerWrap);
  scrollMessages(true);
}
function showWelcomeView() {
  $("#chat-pane").hidden = true;
  $("#welcome-pane").hidden = false;
  const quick = $(".quick-tasks");
  $("#welcome-pane").insertBefore(composerWrap, quick);
}

/* ============================================================
   消息渲染
   ============================================================ */
function addUserMessage(text) {
  const row = document.createElement("div");
  row.className = "msg-row user";
  const bubble = document.createElement("div");
  bubble.className = "msg user";
  bubble.textContent = text;
  row.appendChild(bubble);
  $("#messages").appendChild(row);
  scrollMessages(true);
}

function addAssistantMessage(toolEvents = []) {
  const row = document.createElement("div");
  row.className = "msg-row assistant";
  row.innerHTML = `
    <span class="msg-avatar">C</span>
    <div class="assistant-inner">
      <div class="loop-timeline" hidden>
        <div class="loop-timeline-head">
          <svg class="ic sm"><use href="#i-zap"/></svg>
          Agent 循环链路
        </div>
        <div class="loop-steps"></div>
      </div>
      <div class="thought-box" hidden>
        <div class="thought-head">💭 思考过程 <span class="tool-chevron">⌄</span></div>
        <div class="thought-body"></div>
      </div>
      <div class="tool-panels"></div>
      <div class="md-body"></div>
    </div>`;
  $("#messages").appendChild(row);

  const panels = row.querySelector(".tool-panels");
  const body = row.querySelector(".md-body");
  const thoughtBox = row.querySelector(".thought-box");
  const thoughtBody = row.querySelector(".thought-body");
  const loopBox = row.querySelector(".loop-timeline");
  const loopSteps = row.querySelector(".loop-steps");
  const panelMap = {};
  const loopMap = {};
  let rawText = "";
  let renderQueued = false;

  function ensureLoopStep(id, title, meta, status) {
    loopBox.hidden = false;
    let step = loopMap[id];
    if (!step) {
      step = document.createElement("div");
      step.className = "loop-step";
      step.dataset.id = id;
      step.innerHTML = `<span class="loop-step-dot"></span><div><div class="loop-step-title"></div><div class="loop-step-meta"></div></div>`;
      loopSteps.appendChild(step);
      loopMap[id] = step;
    }
    step.querySelector(".loop-step-title").textContent = title;
    step.querySelector(".loop-step-meta").textContent = meta || "";
    step.className = "loop-step " + (status || "running");
    scrollMessages();
    return step;
  }

  const ui = {
    addThought(text) {
      thoughtBox.hidden = false;
      thoughtBox.classList.add("open");
      thoughtBody.textContent += text;
      scrollMessages();
    },
    addLoopPlan(summary, tasks) {
      loopBox.hidden = false;
      ensureLoopStep("plan", "任务规划", summary || "拆解执行步骤", "done");
      (tasks || []).forEach((task) => {
        ensureLoopStep(`task-${task.id}`, task.title || task.id, task.scope || "", task.status || "pending");
      });
    },
    updateLoopTask(id, status, attempts) {
      const step = loopMap[`task-${id}`];
      if (!step) {
        ensureLoopStep(`task-${id}`, id, `attempts=${attempts || 0}`, status === "done" ? "done" : "running");
        return;
      }
      step.className = "loop-step " + (status === "done" || status === "completed" ? "done" : status === "failed" ? "fail" : "running");
      const meta = step.querySelector(".loop-step-meta");
      if (meta) meta.textContent = `状态: ${status}${attempts ? ` · 第 ${attempts} 次` : ""}`;
    },
    addLoopVerify(data) {
      ensureLoopStep(`verify-${data.task || Date.now()}`, `验收 · ${data.command || ""}`,
        data.ok ? `通过 (exit ${data.exit_code})` : `未通过 (exit ${data.exit_code})`,
        data.ok ? "done" : "fail");
    },
    addLoopRollback(data) {
      ensureLoopStep(`rollback-${data.task || Date.now()}`, `回滚 · ${data.task || ""}`,
        (data.paths || []).join(", "), "fail");
    },
    addLoopDelegate(data) {
      ensureLoopStep(`delegate-${Date.now()}`, "子代理委派",
        (data.children || []).join(", "), "done");
    },
    addStatus(text) {
      ensureLoopStep(`status-${Date.now()}`, text, "", "running");
    },
    addToolCall(id, name, args) {
      const kind = toolChainKind(name);
      const panel = document.createElement("div");
      panel.className = `tool-panel chain-${kind} open`;
      panel.dataset.id = id;
      let argsText;
      try { argsText = typeof args === "string" ? args : JSON.stringify(args ?? {}, null, 2); }
      catch (_) { argsText = String(args); }
      const icons = { fs: "📁", rag: "🔍", sys: "⚙️", agent: "🔧" };
      panel.innerHTML = `
        <div class="tool-head">
          <div class="tool-icon">${icons[kind] || "🔧"}</div>
          <span class="tool-name"></span>
          <span class="tool-chain-tag">${toolChainLabel(kind)}</span>
          <span class="tool-status running"><span class="tool-spinner"></span> 运行中…</span>
          <span class="tool-chevron">⌄</span>
        </div>
        <div class="tool-body">
          <div class="tool-section">
            <div class="tool-section-title">调用参数</div>
            <pre class="tool-args"></pre>
          </div>
          <div class="tool-section result-section" hidden>
            <div class="tool-section-title">返回结果</div>
            <pre class="tool-output"></pre>
          </div>
        </div>`;
      panel.querySelector(".tool-name").textContent = name;
      panel.querySelector(".tool-args").textContent = argsText || "(无参数)";
      panels.appendChild(panel);
      panelMap[id] = panel;
      scrollMessages();
      return panel;
    },
    finishTool(id, output, isError) {
      const panel = panelMap[id];
      if (!panel) return;
      const status = panel.querySelector(".tool-status");
      status.className = "tool-status " + (isError ? "err" : "ok");
      status.textContent = isError ? "✗ 执行失败" : "✓ 执行完成";
      const section = panel.querySelector(".result-section");
      section.hidden = false;
      panel.querySelector(".tool-output").textContent = output || "(无返回)";
      if (isError) panel.style.borderColor = "rgba(220,59,59,.45)";
      panel.classList.remove("open");
      scrollMessages();
    },
    appendToken(piece) {
      rawText += piece;
      if (!renderQueued) {
        renderQueued = true;
        requestAnimationFrame(() => {
          renderQueued = false;
          body.innerHTML = renderMarkdown(rawText);
          scrollMessages();
        });
      }
    },
    setText(text) {
      rawText = text || "";
      body.innerHTML = renderMarkdown(rawText);
    },
    flush() {
      renderQueued = false;
      body.innerHTML = renderMarkdown(rawText);
      scrollMessages();
    },
    showError(message) {
      const banner = document.createElement("div");
      banner.className = "error-banner";
      banner.textContent = "⚠ " + message;
      row.querySelector(".assistant-inner").appendChild(banner);
      scrollMessages(true);
    },
    addPermissionRequest(data) {
      const card = document.createElement("div");
      card.className = "perm-card";
      card.dataset.requestId = data.request_id || "";
      const calls = (data.calls || []).map(escapeHtml).join("</div><div class=\"perm-call\">");
      card.innerHTML = `
        <div class="perm-head"><span class="perm-icon">🔐</span>需要你的确认（60 秒内未答复将自动拒绝）</div>
        <div class="perm-calls">
          <div class="perm-call">${calls || escapeHtml(data.kind || "工具调用")}</div>
        </div>
        <div class="perm-actions">
          <button type="button" class="perm-allow" data-perm="allow">允许</button>
          <button type="button" class="perm-deny" data-perm="deny">拒绝</button>
        </div>`;
      row.querySelector(".assistant-inner").appendChild(card);
      scrollMessages(true);
    },
  };

  toolEvents.forEach((ev, idx) => {
    const id = ev.tool_call_id || `hist_${idx}`;
    ui.addToolCall(id, ev.name, ev.arguments);
    ui.finishTool(id, ev.output || "", !!ev.is_error);
  });
  return ui;
}

function scrollMessages(force = false) {
  const box = $("#messages");
  if (!box) return;
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 140;
  if (force || near) box.scrollTop = box.scrollHeight;
}

/* 对话区事件委托：代码块 / 工具面板 / 思考折叠 */
$("#chat-pane").addEventListener("click", (e) => {
  const copyBtn = e.target.closest('button[data-act="copy"]');
  if (copyBtn) {
    const block = copyBtn.closest(".code-block");
    const code = block.querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(
      () => { copyBtn.textContent = "已复制"; setTimeout(() => (copyBtn.textContent = "复制"), 1200); },
      () => toast("复制失败，请手动选择", "error"));
    return;
  }
  const toggleBtn = e.target.closest('button[data-act="toggle"]');
  const expandTip = e.target.closest(".code-expand-tip");
  const block = toggleBtn ? toggleBtn.closest(".code-block")
    : expandTip ? expandTip.closest(".code-block") : null;
  if (block) {
    const collapsed = block.classList.toggle("collapsed");
    const btn = block.querySelector('button[data-act="toggle"]');
    if (btn) btn.textContent = collapsed ? "展开" : "折叠";
    return;
  }
  const toolHead = e.target.closest(".tool-head");
  if (toolHead) toolHead.closest(".tool-panel").classList.toggle("open");

  const thoughtHead = e.target.closest(".thought-head");
  if (thoughtHead) thoughtHead.closest(".thought-box").classList.toggle("open");

  // 权限确认卡片：允许 / 拒绝 → POST /api/permissions/{request_id}
  const permBtn = e.target.closest("button[data-perm]");
  if (permBtn) {
    const card = permBtn.closest(".perm-card");
    if (!card) return;
    const requestId = card.dataset.requestId;
    const decision = permBtn.dataset.perm;   // "allow" | "deny"
    $$("button[data-perm]", card).forEach((btn) => { btn.disabled = true; });
    card.classList.add(decision === "allow" ? "perm-decided-allow" : "perm-decided-deny");
    card.querySelector(".perm-head").textContent =
      decision === "allow" ? "✓ 已允许执行" : "✗ 已拒绝执行";
    fetch(`/api/permissions/${encodeURIComponent(requestId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    }).then((resp) => {
      if (!resp.ok) toast("审批答复失败，操作将被视为拒绝", "error");
    }).catch(() => toast("审批答复失败，操作将被视为拒绝", "error"));
    return;
  }
});

/* ============================================================
   本机项目 / 任务渲染（Zcode 式：选目录即热重载 fs_* / sys_*）
   ============================================================ */
async function loadRuntime(silent = false) {
  try {
    state.runtime = await api("/api/runtime");
    state.projects = state.runtime.projects || [];
    state.currentProjectId = state.runtime.active_project_id;
    syncExecModeFromRuntime();
    renderProjects();
    updateComposerHint();
    await refreshChainStatus();
    if (state.sidebarPanel === "groups") renderSidebarFiles(state.fileTreePath);
  } catch (err) {
    if (!silent) toast("运行时状态加载失败: " + err.message, "error");
  }
}

function activeProject() {
  return state.projects.find((p) => p.id === state.currentProjectId) || state.projects[0];
}

function updateComposerHint() {
  const hint = $("#composer-hint");
  if (!hint) return;
  const rt = state.runtime;
  const proj = activeProject();
  if (!proj) {
    hint.textContent = "点击左侧「添加本机项目」选择本地目录";
    return;
  }
  const fs = (rt?.tools?.fs || []).length;
  const sys = (rt?.tools?.sys || []).length;
  const rag = state.health?.rag_enabled ? (state.health.rag?.chunks ?? 0) : 0;
  const fw = state.health?.framework || "agent";
  const modeMeta = execModeMeta(state.execMode);
  const needsConfirm = state.execMode !== "full_access";
  hint.textContent =
    `链路协同: 文件 ${fs} 工具 → 知识库 ${rag} 块 → ${fw} 循环` +
    (sys ? `｜sys_* ${sys}` : "") +
    `｜${modeMeta.label}` +
    (needsConfirm ? "（写入/系统动作需确认）" : "（已授权范围内少确认）");
}

function toolChainKind(name) {
  const n = String(name || "");
  if (n.startsWith("fs_") || n === "read_file" || n === "write_file" || n === "list_dir") return "fs";
  if (n.startsWith("rag_") || n.includes("search") && n.includes("code")) return "rag";
  if (n.startsWith("sys_")) return "sys";
  return "agent";
}

function toolChainLabel(kind) {
  return { fs: "文件", rag: "检索", sys: "系统", agent: "工具" }[kind] || "工具";
}

async function refreshChainStatus() {
  try {
    state.health = await api("/api/health");
  } catch (_) {
    state.health = null;
  }
  try {
    const rag = await api("/api/rag/status");
    state.ragOk = !!rag.enabled;
    if (state.health) state.health.rag = rag;
  } catch (_) {
    state.ragOk = false;
  }
  renderChainStrip();
  renderChainFooter();
  syncExecModeFromRuntime();
  if (window.ChainModules) window.ChainModules.refresh();
}

function renderChainStrip() {
  const rt = state.runtime;
  const h = state.health;
  const fsCount = (rt?.tools?.fs || []).length;
  const fsNode = $("#chain-fs");
  const ragNode = $("#chain-rag");
  const agentNode = $("#chain-agent");
  if (!fsNode) return;

  const fsReady = fsCount > 0 && rt?.local_access?.enabled;
  fsNode.className = "chain-node " + (fsReady ? "ready" : fsCount ? "warn" : "off");
  const fsVal = $("#chain-fs-val");
  if (fsVal) fsVal.textContent = fsReady ? `${fsCount} 工具` : fsCount ? "待授权" : "未挂载";

  const ragCount = h?.rag?.chunks ?? h?.rag?.count ?? 0;
  const ragReady = state.ragOk && ragCount > 0;
  ragNode.className = "chain-node " + (ragReady ? "ready" : state.ragOk ? "warn" : "off");
  const ragVal = $("#chain-rag-val");
  if (ragVal) ragVal.textContent = ragReady ? `${ragCount} 块` : state.ragOk ? "待索引" : "未启用";

  const fw = h?.framework || "—";
  const loopReady = fw.includes("state_loop") || fw.includes("react");
  agentNode.className = "chain-node " + (loopReady ? "ready" : h ? "warn" : "off");
  const agentVal = $("#chain-agent-val");
  if (agentVal) agentVal.textContent = fw.replace("state_loop", "循环").replace("native_react", "ReAct");
}

function renderChainFooter() {
  const ragEl = $("#cf-rag-status");
  const ragText = $("#cf-rag-text");
  const accessEl = $("#cf-access-status");
  const accessText = $("#cf-access-text");
  const ragCount = state.health?.rag?.chunks ?? state.health?.rag?.count ?? 0;

  if (ragText) {
    ragText.textContent = state.ragOk && ragCount > 0
      ? `${t("chain.ragReady")} · ${ragCount}`
      : t("chain.ragIdle");
  }
  if (ragEl) ragEl.className = "cf-item cf-status cf-clickable " + (state.ragOk && ragCount > 0 ? "ready" : "warn");

  const localOn = state.runtime?.local_access?.enabled;
  if (accessText) {
    accessText.textContent = localOn
      ? t("composer.localGranted")
      : t("composer.localRestricted");
  }
  if (accessEl) accessEl.className = "cf-item cf-status cf-clickable " + (localOn ? "ready" : "warn");
}

function setSidebarPanel(panel) {
  state.sidebarPanel = panel;
  const proj = $("#sb-panel-projects");
  const groups = $("#sb-panel-groups");
  const segP = $("#seg-projects");
  const segG = $("#seg-groups");
  if (proj) proj.hidden = panel !== "projects";
  if (groups) groups.hidden = panel !== "groups";
  if (segP) segP.classList.toggle("active", panel === "projects");
  if (segG) segG.classList.toggle("active", panel === "groups");
  if (panel === "groups") renderSidebarFiles(state.fileTreePath);
}

async function renderSidebarFiles(path = ".") {
  const box = $("#sidebar-file-tree");
  if (!box) return;
  if (!activeProject()) {
    box.innerHTML = `<div class="sb-hint-text">请先添加项目</div>`;
    return;
  }
  box.innerHTML = `<div class="sb-hint-text">加载中…</div>`;
  try {
    const data = await api(`/api/files/list?path=${encodeURIComponent(path)}`);
    state.fileTreePath = data.path || path;
    box.innerHTML = "";
    if (path !== ".") {
      const up = document.createElement("button");
      up.className = "file-tree-row dir";
      up.innerHTML = `<svg class="ic sm"><use href="#i-chevron-right" style="transform:rotate(-90deg)"/></svg><span class="ft-name">..</span>`;
      up.addEventListener("click", () => renderSidebarFiles(parentPath(path)));
      box.appendChild(up);
    }
    (data.items || []).slice(0, 80).forEach((item) => {
      const row = document.createElement("button");
      row.className = "file-tree-row" + (item.type === "dir" ? " dir" : "");
      row.innerHTML = `<svg class="ic sm"><use href="#i-${item.type === "dir" ? "folder" : "file"}"/></svg><span class="ft-name"></span>`;
      row.querySelector(".ft-name").textContent = item.name;
      row.title = item.relpath;
      row.addEventListener("click", () => {
        if (item.type === "dir") renderSidebarFiles(item.relpath);
        else {
          insertFileRef(item.relpath);
          openFileOverlay(item.relpath);
        }
      });
      box.appendChild(row);
    });
    if (!data.items?.length && path === ".") {
      box.innerHTML = `<div class="sb-hint-text">空目录</div>`;
    }
  } catch (err) {
    box.innerHTML = `<div class="sb-hint-text">${escapeHtml(err.message)}</div>`;
  }
}

function insertFileRef(relpath) {
  const input = $("#input-box");
  if (!input) return;
  const ref = `@${relpath} `;
  const pos = input.selectionStart ?? input.value.length;
  input.value = input.value.slice(0, pos) + ref + input.value.slice(pos);
  autoGrow(input);
  input.focus();
}

function setWorkMode(mode) {
  state.activeMode = mode;
  $$(".mode-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.mode === mode));
  $$(".quick-chip").forEach((chip) => {
    chip.hidden = chip.dataset.mode && chip.dataset.mode !== mode;
  });
}

function updateAgentRoleLabel() {
  const role = AGENT_ROLES[state.agentRole] || AGENT_ROLES.fullstack;
  const lang = state.prefs.language === "en" ? "en" : "zh";
  const label = $("#agent-role-label");
  const avatar = $(".role-avatar");
  if (label) label.textContent = role[lang];
  if (avatar) avatar.textContent = role.letter;
}

function openAgentRolePicker() {
  const btn = $("#btn-agent-role");
  const menu = document.createElement("div");
  menu.className = "model-pick-menu";
  const lang = state.prefs.language === "en" ? "en" : "zh";
  menu.innerHTML = Object.entries(AGENT_ROLES).map(([id, role]) => `
    <button class="model-pick-item ${id === state.agentRole ? "active" : ""}" data-role="${id}">
      <span class="model-pick-name">${escapeHtml(role[lang])}</span>
    </button>`).join("");
  const rect = btn.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.left = `${Math.max(8, rect.left)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  document.body.appendChild(menu);
  const close = () => menu.remove();
  setTimeout(() => {
    document.addEventListener("click", function handler(e) {
      if (!menu.contains(e.target) && !btn.contains(e.target)) {
        close();
        document.removeEventListener("click", handler);
      }
    });
  }, 0);
  menu.querySelectorAll("[data-role]").forEach((el) => {
    el.addEventListener("click", () => {
      state.agentRole = el.dataset.role;
      updateAgentRoleLabel();
      close();
    });
  });
}

function hideSuggest() {
  const box = $("#input-suggest");
  if (box) box.hidden = true;
}

function showSuggest(items, onPick) {
  const box = $("#input-suggest");
  if (!box || !items.length) { hideSuggest(); return; }
  box.hidden = false;
  box.innerHTML = items.map((item, i) => `
    <button type="button" class="suggest-item${i === 0 ? " active" : ""}" data-idx="${i}">
      ${item.tag ? `<span class="suggest-tag">${escapeHtml(item.tag)}</span>` : ""}
      <span>${escapeHtml(item.label)}</span>
      ${item.desc ? `<span class="suggest-desc">${escapeHtml(item.desc)}</span>` : ""}
    </button>`).join("");
  box.querySelectorAll(".suggest-item").forEach((el) => {
    el.addEventListener("click", () => onPick(items[Number(el.dataset.idx)]));
  });
}

function handleInputSuggest() {
  const input = $("#input-box");
  if (!input) return;
  const val = input.value;
  const at = val.lastIndexOf("@");
  const slash = val.lastIndexOf("/");
  if (slash >= 0 && slash >= at && slash === val.length - 1 || val.slice(slash).match(/^\/\S*$/)) {
    const q = val.slice(slash + 1).toLowerCase();
    const items = SLASH_COMMANDS
      .filter((c) => c.cmd.slice(1).toLowerCase().startsWith(q))
      .map((c) => ({ tag: "/", label: c.cmd, desc: c.desc, cmd: c }));
    showSuggest(items, (item) => {
      if (item.cmd.action === "rag-index") runRagIndex("incremental");
      else if (item.cmd.action === "clear-chat") {
        state.currentSessionId = null;
        showWelcomeView();
      } else if (item.cmd.prompt) {
        input.value = item.cmd.prompt;
        autoGrow(input);
      } else {
        input.value = val.slice(0, slash) + item.cmd.cmd + " ";
        autoGrow(input);
      }
      hideSuggest();
      input.focus();
    });
    return;
  }
  if (at >= 0 && (at === val.length - 1 || !val.slice(at).includes(" "))) {
    const q = val.slice(at + 1).toLowerCase();
    const items = (state.sessions || [])
      .filter((s) => !q || s.title.toLowerCase().includes(q))
      .slice(0, 5)
      .map((s) => ({ tag: "@", label: s.title, desc: "对话", ref: `@对话:${s.id} ` }));
    if (activeProject()) {
      items.unshift({ tag: "@", label: "当前项目", desc: activeProject().name, ref: `@项目 ` });
    }
    showSuggest(items, (item) => {
      input.value = val.slice(0, at) + (item.ref || `@${item.label} `);
      autoGrow(input);
      hideSuggest();
      input.focus();
    });
    return;
  }
  hideSuggest();
}

function getVisibleSessions() {
  const meta = loadTaskMeta();
  let sessions = [...state.sessions];
  sessions = sessions.filter((s) => {
    const m = meta[s.id] || {};
    if (state.showArchivedTasks) return !!m.archived;
    if (m.archived) return false;
    if (state.taskFilterUnread && !m.unread) return false;
    return true;
  });
  sessions.sort((a, b) => {
    const pa = (meta[a.id] || {}).pinned ? 1 : 0;
    const pb = (meta[b.id] || {}).pinned ? 1 : 0;
    if (pa !== pb) return pb - pa;
    return (b.updated_at || 0) - (a.updated_at || 0);
  });
  return sessions;
}

function renderTaskList() {
  const box = $("#task-list");
  if (!box) return;
  box.innerHTML = "";
  const sessions = getVisibleSessions();
  if (!sessions.length) {
    const hint = document.createElement("div");
    hint.className = "sb-hint-text";
    hint.textContent = state.showArchivedTasks ? "没有已归档任务" : t("nav.noTasks");
    box.appendChild(hint);
    return;
  }
  sessions.forEach((s) => {
    const m = getTaskMeta(s.id);
    const row = document.createElement("button");
    row.className = "task-row"
      + (s.id === state.currentSessionId ? " active" : "")
      + (m.pinned ? " pinned" : "")
      + (m.unread ? " unread" : "");
    row.dataset.sessionId = s.id;
    row.innerHTML = `<span class="t-title"></span><span class="t-time"></span>`;
    row.querySelector(".t-title").textContent = s.title;
    row.querySelector(".t-time").textContent = relTime(s.updated_at);
    row.addEventListener("click", () => {
      setTaskMeta(s.id, { unread: false });
      openSession(s.id);
    });
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      openTaskCtxMenu(e, s);
    });
    box.appendChild(row);
  });
}

function renderProjects() {
  const box = $("#project-list");
  if (!box) return;
  box.innerHTML = "";
  const active = activeProject();
  if ($("#current-project-name")) {
    $("#current-project-name").textContent = active ? active.name : "未选择项目";
  }

  if (!state.projects.length) {
    const hint = document.createElement("div");
    hint.className = "sb-hint-text";
    hint.style.paddingLeft = "10px";
    hint.textContent = "添加本机目录开始";
    box.appendChild(hint);
    renderTaskList();
    return;
  }

  state.projects.forEach((p) => {
    const block = document.createElement("div");
    block.className = "project-block";

    const head = document.createElement("button");
    head.className = "project-row" + (p.id === state.currentProjectId ? " active" : "");
    head.innerHTML = `<svg class="ic"><use href="#i-folder"/></svg><span class="p-name"></span>`;
    head.querySelector(".p-name").textContent = p.name;
    head.title = p.path;
    head.addEventListener("click", () => selectProject(p.id));
    block.appendChild(head);

    if (p.id === state.currentProjectId) {
      const latest = state.sessions.reduce((best, s) => (
        !best || (s.updated_at || 0) > (best.updated_at || 0) ? s : best
      ), null);
      const src = document.createElement("button");
      src.className = "project-source-row" + (!state.currentSessionId ? " active" : "");
      src.innerHTML = `<span class="ps-label"></span><span class="ps-time"></span>`;
      src.querySelector(".ps-label").textContent = projectSourceLabel(p.path);
      src.querySelector(".ps-time").textContent = relTime(latest?.updated_at || p.updated_at);
      src.title = p.path;
      src.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openProjectCtxMenu(e, p);
      });
      src.addEventListener("click", () => {
        setSidebarPanel("groups");
        renderSidebarFiles(".");
      });
      block.appendChild(src);
    }
    box.appendChild(block);
  });
  renderTaskList();
}

function openProjectCtxMenu(e, project) {
  const items = [
    { label: t("ctx.explorer"), action: "explorer" },
    { label: t("ctx.copyPath"), action: "copyPath" },
    { sep: true },
    { label: t("ctx.config"), action: "config" },
    { label: t("ctx.feedback"), action: "feedback" },
  ];
  showCtxMenu(e.clientX, e.clientY, items, (item) => {
    if (!item || item.sep) return;
    if (item.action === "explorer") copyText(project.path, "路径已复制，可在资源管理器中粘贴打开");
    else if (item.action === "copyPath") copyText(project.path);
    else if (item.action === "config") showSettings("general");
    else if (item.action === "feedback") toast("感谢反馈，请通过 GitHub Issues 提交问题", "ok");
  });
}

async function openTaskCtxMenu(e, session) {
  const meta = getTaskMeta(session.id);
  const items = [
    { label: t("ctx.pin"), action: "pin" },
    { label: t("ctx.rename"), action: "rename" },
    { label: t("ctx.archive"), action: "archive" },
    { label: t("ctx.unread"), action: "unread" },
    { label: t("ctx.split"), action: "split" },
    { sep: true },
    { label: t("ctx.explorer"), action: "explorer" },
    { label: t("ctx.copyPath"), action: "copyPath" },
    { label: t("ctx.copyTaskPath"), action: "copyTaskPath" },
    { label: t("ctx.copyLogPath"), action: "copyLogPath" },
    { label: t("ctx.copySession"), action: "copySession" },
    { label: t("ctx.config"), action: "config" },
    { sep: true },
    { label: t("ctx.viewTrace"), action: "trace" },
    { sep: true },
    { label: t("ctx.feedback"), action: "feedback" },
  ];
  showCtxMenu(e.clientX, e.clientY, items, async (item) => {
    if (!item || item.sep) return;
    const proj = activeProject();
    switch (item.action) {
      case "pin":
        setTaskMeta(session.id, { pinned: !meta.pinned });
        renderProjects();
        toast(meta.pinned ? "已取消置顶" : "已置顶", "ok");
        break;
      case "rename": {
        const title = prompt("重命名任务", session.title);
        if (!title || title.trim() === session.title) return;
        try {
          await api(`/api/sessions/${session.id}`, {
            method: "PATCH",
            body: JSON.stringify({ title: title.trim() }),
          });
          await loadSessions(true);
          toast("已重命名", "ok");
        } catch (err) { toast(err.message, "error"); }
        break;
      }
      case "archive":
        setTaskMeta(session.id, { archived: true, pinned: false });
        if (state.currentSessionId === session.id) {
          state.currentSessionId = null;
          showWelcomeView();
        }
        renderProjects();
        toast("已归档", "ok");
        break;
      case "unread":
        setTaskMeta(session.id, { unread: true });
        renderProjects();
        toast("已标记为未读", "ok");
        break;
      case "split":
        setSidebarPanel("groups");
        await openSession(session.id);
        break;
      case "explorer":
        if (proj?.path) copyText(proj.path, "路径已复制，可在资源管理器中粘贴打开");
        else toast("请先添加项目", "error");
        break;
      case "copyPath":
        if (proj?.path) copyText(proj.path);
        else toast("请先添加项目", "error");
        break;
      case "copyTaskPath":
        copyText(taskDataPath(session.id));
        break;
      case "copyLogPath":
        copyText(logDataPath());
        break;
      case "copySession":
        copyText(session.id);
        break;
      case "config":
        showSettings("general");
        break;
      case "trace":
        await showCallTrace(session.id);
        break;
      case "feedback":
        toast("感谢反馈，请通过 GitHub Issues 提交问题", "ok");
        break;
      default:
        break;
    }
  });
}

async function showCallTrace(sessionId) {
  const modal = $("#trace-modal");
  const body = $("#trace-modal-body");
  if (!modal || !body) return;
  body.textContent = "加载中…";
  modal.hidden = false;
  try {
    const session = await api(`/api/sessions/${sessionId}`);
    const events = [];
    (session.messages || []).forEach((msg, i) => {
      (msg.tool_events || []).forEach((ev) => {
        events.push({
          turn: i + 1,
          role: msg.role,
          name: ev.name || ev.tool || "?",
          args: ev.arguments || ev.input,
          output: ev.output,
          is_error: ev.is_error,
        });
      });
    });
    body.textContent = events.length
      ? JSON.stringify(events, null, 2)
      : "该任务暂无工具调用记录";
  } catch (err) {
    body.textContent = `加载失败：${err.message}`;
  }
}

function closeTraceModal() {
  const modal = $("#trace-modal");
  if (modal) modal.hidden = true;
}

function toggleSidebarExpanded() {
  const sb = $(".sidebar");
  if (!sb) return;
  sb.classList.toggle("expanded");
  toast(sb.classList.contains("expanded") ? "侧边栏已展开" : "侧边栏已收起", "ok");
}

function toggleTaskFilter() {
  state.taskFilterUnread = !state.taskFilterUnread;
  $(".sidebar")?.classList.toggle("filter-active", state.taskFilterUnread);
  renderTaskList();
  toast(state.taskFilterUnread ? "仅显示未读任务" : "显示全部任务", "ok");
}

function toggleArchivedView() {
  state.showArchivedTasks = !state.showArchivedTasks;
  $(".sidebar")?.classList.toggle("archive-view", state.showArchivedTasks);
  renderTaskList();
  toast(state.showArchivedTasks ? "查看已归档任务" : "返回任务列表", "ok");
}

async function selectProject(projectId) {
  if (projectId === state.currentProjectId) return;
  try {
    await api(`/api/runtime/projects/${encodeURIComponent(projectId)}/activate`, { method: "POST" });
    await loadRuntime(true);
    if (!$("#chat-pane").hidden) {
      state.currentSessionId = null;
      showWelcomeView();
    }
    toast("已切换项目", "ok");
  } catch (err) {
    toast(err.message, "error");
  }
}

function openProjectModal() {
  $("#project-modal").hidden = false;
  browseProjectDirs("");
}

function closeProjectModal() {
  $("#project-modal").hidden = true;
}

async function browseProjectDirs(path) {
  const list = $("#project-browse-list");
  const pathEl = $("#project-browse-path");
  list.innerHTML = '<div class="sb-hint-text">加载中…</div>';
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await api(`/api/runtime/browse${q}`);
    state.browsePath = data.path || "";
    pathEl.textContent = data.path || "快捷位置";
    list.innerHTML = "";
    if (data.parent) {
      const up = document.createElement("button");
      up.className = "project-browse-item";
      up.innerHTML = `<svg class="ic sm"><use href="#i-folder"/></svg><span>.. 上级</span>`;
      up.addEventListener("click", () => browseProjectDirs(data.parent));
      list.appendChild(up);
    }
    (data.entries || []).forEach((entry) => {
      const row = document.createElement("button");
      row.className = "project-browse-item";
      const label = entry.label || entry.name || entry.path;
      row.innerHTML = `<svg class="ic sm"><use href="#i-folder"/></svg><span></span>`;
      row.querySelector("span").textContent = label;
      row.addEventListener("click", () => {
        if (entry.path) browseProjectDirs(entry.path);
      });
      list.appendChild(row);
    });
    if (!data.entries?.length) {
      const empty = document.createElement("div");
      empty.className = "sb-hint-text";
      empty.textContent = state.browsePath ? "无子目录，可直接添加当前路径" : "没有可用快捷位置";
      list.appendChild(empty);
    }
    if (state.browsePath) {
      const nameInput = $("#project-name-input");
      if (nameInput && !nameInput.value) {
        nameInput.value = state.browsePath.split(/[/\\]/).filter(Boolean).pop() || "";
      }
    }
  } catch (err) {
    list.innerHTML = `<div class="sb-hint-text">${escapeHtml(err.message)}</div>`;
  }
}

async function confirmAddProject() {
  const path = state.browsePath;
  if (!path) {
    toast("请先浏览并进入一个目录", "error");
    return;
  }
  const name = ($("#project-name-input").value || "").trim();
  const write = $("#project-write-check").checked;
  try {
    await api("/api/runtime/projects", {
      method: "POST",
      body: JSON.stringify({ path, name: name || undefined, write }),
    });
    closeProjectModal();
    $("#project-name-input").value = "";
    await loadRuntime(true);
    toast("项目已添加，本地访问工具已热重载", "ok");
  } catch (err) {
    toast(err.message, "error");
  }
}

/* ---------------- 会话管理 ---------------- */
async function loadSessions(silent = false) {
  try {
    const data = await api("/api/sessions");
    state.sessions = data.sessions || [];
    renderProjects();
  } catch (err) {
    if (!silent) toast("任务列表加载失败: " + err.message, "error");
  }
}

async function openSession(id) {
  try {
    const session = await api(`/api/sessions/${id}`);
    state.currentSessionId = id;
    renderHistory(session.messages || []);
    $("#chat-title").textContent = session.title || "新任务";
    renderProjects();
    showChatView();
  } catch (err) { toast(err.message, "error"); }
}

function renderHistory(messages) {
  const box = $("#messages");
  box.innerHTML = "";
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "text-align:center;color:var(--text-3);font-size:13.5px;padding:40px 0;";
    empty.textContent = "这是一个空任务，开始描述你的需求吧～";
    box.appendChild(empty);
    return;
  }
  for (const msg of messages) {
    if (msg.role === "user") {
      addUserMessage(msg.content);
    } else if (msg.role === "assistant") {
      const ui = addAssistantMessage(msg.tool_events || []);
      ui.setText(msg.content || "");
    }
  }
  scrollMessages(true);
}

/* ============================================================
   SSE 流式对话
   ============================================================ */
async function resolveOutgoingPrompt(text) {
  try {
    if (window.__promptOptimize) return await window.__promptOptimize(text);
  } catch (err) {
    console.warn("[prompt-optimizer] 优化失败，回退原始输入：", err);
  }
  return text;
}

async function sendMessage(text) {
  if (state.sending) return;
  text = (text || "").trim();
  if (!text) return;
  showChatView();
  addUserMessage(text);
  const ui = addAssistantMessage();
  setSending(true);

  state.abortCtrl = new AbortController();
  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: state.currentSessionId,
        message: text,
        reasoning_level: state.reasoningLevel,
      }),
      signal: state.abortCtrl.signal,
    });
    if (!resp.ok || !resp.body) {
      let detail = `HTTP ${resp.status}`;
      try { detail = (await resp.json()).detail || detail; } catch (_) { /* ignore */ }
      throw new Error(detail);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        let event;
        try { event = JSON.parse(payload); } catch (_) { continue; }
        handleStreamEvent(event, ui);
      }
    }
    ui.flush();
  } catch (err) {
    if (err.name === "AbortError") {
      ui.showError("已停止生成。");
    } else {
      ui.showError(err.message || "请求失败，请确认后端与模型服务是否正常。");
    }
  } finally {
    setSending(false);
    state.abortCtrl = null;
    await loadSessions(true);
  }
}

function handleStreamEvent(event, ui) {
  const { type, data } = event;
  switch (type) {
    case "session":
      state.currentSessionId = data.session_id;
      break;
    case "token":
      ui.appendToken(data.content || "");
      break;
    case "thought":
      ui.addThought(data.content || "");
      break;
    case "status":
      ui.addStatus(data.message || data.content || "处理中…");
      break;
    case "plan":
      ui.addLoopPlan(data.summary, data.tasks);
      break;
    case "task":
      ui.updateLoopTask(data.id, data.status, data.attempts);
      break;
    case "verify":
      ui.addLoopVerify(data);
      break;
    case "rollback":
      ui.addLoopRollback(data);
      break;
    case "delegate":
      ui.addLoopDelegate(data);
      break;
    case "tool_call":
      ui.addToolCall(data.id, data.name, data.arguments);
      break;
    case "tool_result":
      ui.finishTool(data.id, data.output || "", !!data.is_error);
      if (toolChainKind(data.name) === "rag" && !data.is_error) refreshChainStatus();
      if (window.POContextClient && data.output) {
        String(data.output).split(/\r?\n/).slice(-3).forEach(function (line) {
          POContextClient.appendTerminalLine(line);
        });
      }
      break;
    case "error":
      ui.showError(data.message || "未知错误");
      break;
    case "permission_request":
      ui.addPermissionRequest(data);
      break;
    case "done":
      ui.flush();
      break;
    case "saved":
      if (data.title) $("#chat-title").textContent = data.title;
      break;
  }
}

function setSending(sending) {
  state.sending = sending;
  $("#btn-send").hidden = sending;
  $("#btn-stop").hidden = !sending;
}

/* ============================================================
   账户快捷菜单弹层（右下角账户区触发）
   ============================================================ */
const popup = $("#settings-popup");
const flyout = $("#popup-flyout");

function openPopup() {
  const anchor = $("#user-card").getBoundingClientRect();
  popup.hidden = false;
  flyout.hidden = true;
  const pw = popup.offsetWidth;
  let left = anchor.right - pw;
  if (left < 12) left = 12;
  popup.style.left = left + "px";
  popup.style.bottom = window.innerHeight - anchor.top + 10 + "px";
  popup.style.top = "auto";
}
function closePopup() {
  popup.hidden = true;
  flyout.hidden = true;
  activeFlyout = null;
  $$("[data-fly]", popup).forEach((btn) => btn.classList.remove("fly-active"));
}

const FLYOUTS = {
  language: {
    titleKey: "popup.language",
    pref: "language",
    items: [
      { v: "zh", labelKey: "fly.lang.zh" },
      { v: "en", labelKey: "fly.lang.en" },
    ],
    onChoose: (v) => {
      state.prefs.language = v;
      savePrefs();
      applyI18n();
      if (!popup.hidden && activeFlyout) openFlyout(activeFlyout);
      toast(t("toast.lang"), "ok");
    },
  },
  theme: {
    titleKey: "popup.theme",
    pref: "theme",
    items: [
      { v: "system", labelKey: "fly.theme.system" },
      { v: "dark", labelKey: "fly.theme.dark" },
      { v: "light", labelKey: "fly.theme.light" },
    ],
    onChoose: (v) => {
      state.prefs.theme = v;
      savePrefs();
      applyTheme(v);
      syncAppearancePage();
      toast(t("toast.theme"), "ok");
    },
  },
  mode: {
    titleKey: "popup.mode",
    pref: "mode",
    items: [
      { v: "standard", labelKey: "fly.mode.standard" },
      { v: "focus", labelKey: "fly.mode.focus" },
      { v: "mini", labelKey: "fly.mode.mini" },
    ],
    onChoose: (v) => {
      state.prefs.mode = v;
      savePrefs();
      applyMode(v);
      toast(t("toast.mode"), "ok");
    },
  },
  zoom: {
    titleKey: "popup.zoom",
    pref: "zoom",
    items: [
      { v: "0.9", label: "90%" },
      { v: "1", label: "100%" },
      { v: "1.1", label: "110%" },
      { v: "1.25", label: "125%" },
    ],
    onChoose: (v) => {
      state.prefs.zoom = v;
      savePrefs();
      applyZoom(v);
      syncAppearancePage();
      toast(t("toast.zoom", { n: Math.round(parseFloat(v) * 100) }), "ok");
    },
  },
};

let activeFlyout = null;

function openFlyout(name) {
  const cfg = FLYOUTS[name];
  if (!cfg) return;
  activeFlyout = name;
  const current = state.prefs[cfg.pref];
  const title = t(cfg.titleKey);
  flyout.innerHTML = `<div class="fly-title">${title}</div>` +
    cfg.items.map((it) => {
      const label = it.labelKey ? t(it.labelKey) : it.label;
      return `
      <button class="fly-item ${current === it.v ? "active" : ""}" data-v="${it.v}">
        <span>${label}</span>
        <svg viewBox="0 0 24 24" class="ic check"><path d="m5 12 5 5L20 7"/></svg>
      </button>`;
    }).join("");
  flyout.hidden = false;
  $$("[data-fly]", popup).forEach((btn) =>
    btn.classList.toggle("fly-active", btn.dataset.fly === name));
  flyout.querySelectorAll(".fly-item").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      cfg.onChoose(el.dataset.v);
      openFlyout(name);
    });
  });
}

popup.addEventListener("click", (e) => {
  const flyBtn = e.target.closest("[data-fly]");
  if (flyBtn) { openFlyout(flyBtn.dataset.fly); return; }

  const goPage = e.target.closest("[data-go-page]");
  if (goPage) {
    closePopup();
    showSettings(goPage.dataset.goPage);
    return;
  }

  const actBtn = e.target.closest("[data-action]");
  if (actBtn) {
    const act = actBtn.dataset.action;
    closePopup();
    if (act === "upgrade") toast(t("toast.upgrade"), "ok");
    if (act === "invite") copyInviteLink();
    if (act === "disconnect") signOut();
  }
});

function copyInviteLink() {
  const link = "https://zcode.app/invite/traveler6172";
  const done = () => toast(t("toast.invite"), "ok");
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(link).then(done).catch(done);
  else done();
}

function signOut() {
  state.prefs.loggedIn = false;
  savePrefs();
  updateAccountDock();
  toast(t("toast.disconnect"));
}

popup.querySelectorAll("[data-fly]").forEach((btn) => {
  btn.addEventListener("mouseenter", () => openFlyout(btn.dataset.fly));
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    openFlyout(btn.dataset.fly);
  });
});

/* ---------------- 主题 / 缩放 / 强调色 ---------------- */
const mqDark = window.matchMedia("(prefers-color-scheme: dark)");
function applyTheme(mode) {
  const dark = mode === "dark" || (mode === "system" && mqDark.matches);
  document.documentElement.dataset.theme = dark ? "dark" : "";
}
mqDark.addEventListener("change", () => {
  if (state.prefs.theme === "system") applyTheme("system");
});
function applyZoom(v) {
  document.documentElement.style.setProperty("--ui-scale", v);
}
function applyMode(mode) {
  document.documentElement.dataset.uiMode = mode || "standard";
}
function applyAllPrefs() {
  applyTheme(state.prefs.theme);
  applyZoom(state.prefs.zoom);
  applyAccent(state.prefs.accent);
  applyMode(state.prefs.mode);
  applyI18n();
}
const ACCENTS = {
  orange: ["#e0812a", "#cf6a10", "#fdf0e1"],
  blue: ["#2f7de1", "#1d5fbb", "#e8f1fd"],
  green: ["#1a9e54", "#127a40", "#e7f5ec"],
  purple: ["#8b5cf6", "#6d3de0", "#f0ebfe"],
};
function applyAccent(name) {
  const a = ACCENTS[name];
  if (!a) return;
  const root = document.documentElement.style;
  root.setProperty("--accent", a[0]);
  root.setProperty("--accent-strong", a[1]);
  root.setProperty("--accent-soft", a[2]);
}

/* ============================================================
   设置中心：模型配置（见 model-settings.js）
   ============================================================ */
function updateModelPickerLabel() {
  const btn = $("#btn-model-pick");
  if (!btn || typeof ModelSettings === "undefined") return;
  const label = ModelSettings.activeLabel();
  btn.innerHTML = `${escapeHtml(label)}<svg class="ic xs"><use href="#i-chevron-down"/></svg>`;
}

function updateThinkingUI() {
  const meta = typeof ThinkingSlider !== "undefined"
    ? ThinkingSlider.meta(state.reasoningLevel)
    : { label: state.reasoningLevel, color: "#34d399", glow: "rgba(52,211,153,.5)" };
  const label = $("#think-label");
  const dot = $("#think-dot");
  const btn = $("#btn-thinking-pick");
  if (label) label.textContent = meta.label;
  if (dot) {
    dot.style.background = meta.color;
    dot.style.boxShadow = `0 0 8px ${meta.glow}`;
  }
  if (btn) {
    btn.style.setProperty("--think-accent", meta.color);
    btn.style.setProperty("--think-glow", meta.glow);
  }
}

async function syncReasoningFromCatalog() {
  if (typeof ModelSettings === "undefined") return;
  try {
    const cat = await ModelSettings.loadCatalog();
    const level = cat.active?.reasoning_level || "medium";
    state.reasoningLevel = level;
    updateThinkingUI();
  } catch (_) { /* ignore */ }
}

let thinkPopover = null;
function closeThinkingPopover() {
  if (thinkPopover) {
    thinkPopover.remove();
    thinkPopover = null;
  }
}

async function openThinkingPopover() {
  closeThinkingPopover();
  if (typeof ModelSettings !== "undefined") {
    try { await ModelSettings.loadCatalog(); } catch (_) { /* ignore */ }
  }
  const cat = typeof ModelSettings !== "undefined" ? ModelSettings.getCatalog() : {};
  const integ = cat.integration?.agent || {};
  const allowed = integ.reasoning_levels || ThinkingSlider.LEVELS.map((l) => l.id);

  const btn = $("#btn-thinking-pick");
  const rect = btn.getBoundingClientRect();
  const pop = document.createElement("div");
  pop.className = "think-popover";
  pop.style.left = `${Math.max(12, rect.left + rect.width / 2 - 150)}px`;
  pop.style.top = `${rect.top - 12}px`;
  pop.style.transform = "translateY(-100%)";
  document.body.appendChild(pop);
  thinkPopover = pop;

  const holder = document.createElement("div");
  pop.appendChild(holder);

  ThinkingSlider.mount(holder, {
    value: state.reasoningLevel,
    title: "思考强度",
    allowed,
    onChange: async (level) => {
      state.reasoningLevel = level;
      updateThinkingUI();
      try {
        await api("/api/models/reasoning", {
          method: "PUT",
          body: JSON.stringify({ level }),
        });
        if (typeof ModelSettings !== "undefined") await ModelSettings.refresh();
      } catch (err) {
        toast(err.message, "error");
      }
    },
  });

  setTimeout(() => {
    document.addEventListener("click", function handler(e) {
      if (!pop.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        closeThinkingPopover();
        document.removeEventListener("click", handler);
      }
    });
  }, 0);
}

/* ---------------- 执行档（计划 / 变更前确认 / 自动编辑 / 完全访问） ---------------- */
const EXEC_MODE_FALLBACK = [
  { id: "plan", labelKey: "exec.plan", hintKey: "exec.planHint", icon: "bulb" },
  { id: "confirm_writes", labelKey: "exec.confirm", hintKey: "exec.confirmHint", icon: "hand" },
  { id: "auto_workspace", labelKey: "exec.auto", hintKey: "exec.autoHint", icon: "shield" },
  { id: "full_access", labelKey: "exec.full", hintKey: "exec.fullHint", icon: "unlock" },
];

function execModeCatalog() {
  const remote = state.execModes || state.runtime?.exec_modes || state.health?.exec_modes;
  if (Array.isArray(remote) && remote.length) {
    return remote.map((item) => ({
      id: item.id,
      label: item.label || item.id,
      hint: item.hint || "",
      icon: item.icon || "shield",
    }));
  }
  return EXEC_MODE_FALLBACK.map((item) => ({
    id: item.id,
    label: t(item.labelKey),
    hint: t(item.hintKey),
    icon: item.icon,
  }));
}

function execModeMeta(id) {
  const catalog = execModeCatalog();
  return catalog.find((m) => m.id === id) || catalog.find((m) => m.id === "auto_workspace") || catalog[0];
}

function syncExecModeFromRuntime() {
  const mode = state.runtime?.exec_mode
    || state.runtime?.capabilities?.exec_mode
    || state.health?.exec_mode
    || state.execMode
    || "auto_workspace";
  state.execMode = mode;
  if (state.runtime?.exec_modes) state.execModes = state.runtime.exec_modes;
  else if (state.health?.exec_modes) state.execModes = state.health.exec_modes;
  updateExecModeUI();
}

function updateExecModeUI() {
  const meta = execModeMeta(state.execMode);
  const label = $("#exec-mode-label");
  const icon = $("#exec-mode-icon");
  if (label) label.textContent = meta.label;
  if (icon) icon.setAttribute("href", `#i-${meta.icon || "shield"}`);
  const btn = $("#btn-exec-mode");
  if (btn) {
    btn.title = meta.hint || meta.label;
    btn.dataset.mode = meta.id || "auto_workspace";
  }
}

let execModeMenu = null;
function closeExecModeMenu() {
  if (execModeMenu) {
    execModeMenu.remove();
    execModeMenu = null;
  }
}

async function setExecMode(modeId) {
  const meta = execModeMeta(modeId);
  if (!meta || meta.id === state.execMode) {
    closeExecModeMenu();
    return;
  }
  try {
    const body = await api("/api/runtime/capabilities", {
      method: "PUT",
      body: JSON.stringify({ exec_mode: meta.id }),
    });
    state.runtime = body;
    state.projects = body.projects || state.projects;
    state.execMode = body.exec_mode || meta.id;
    if (body.exec_modes) state.execModes = body.exec_modes;
    updateExecModeUI();
    updateComposerHint();
    toast(t("toast.execMode").replace("{n}", execModeMeta(state.execMode).label), "ok");
  } catch (err) {
    toast(err.message, "error");
  }
  closeExecModeMenu();
}

function openExecModeMenu() {
  closeExecModeMenu();
  closeThinkingPopover();
  const btn = $("#btn-exec-mode");
  if (!btn) return;
  const rect = btn.getBoundingClientRect();
  const menu = document.createElement("div");
  menu.className = "exec-mode-menu";
  menu.innerHTML = execModeCatalog().map((item) => {
    const active = item.id === state.execMode;
    return `
      <button type="button" class="exec-mode-item ${active ? "active" : ""}" data-mode="${escapeHtml(item.id)}">
        <span class="exec-ico-well" aria-hidden="true">
          <svg class="exec-ico"><use href="#i-${escapeHtml(item.icon || "shield")}"/></svg>
        </span>
        <span class="exec-copy">
          <span class="exec-title">${escapeHtml(item.label)}</span>
          <span class="exec-hint">${escapeHtml(item.hint || "")}</span>
        </span>
        <span class="exec-check-well" aria-hidden="true">
          <svg class="exec-check"><use href="#i-check"/></svg>
        </span>
      </button>`;
  }).join("");
  document.body.appendChild(menu);
  const mw = menu.offsetWidth || 260;
  menu.style.left = `${Math.max(8, Math.min(rect.right - mw, window.innerWidth - mw - 8))}px`;
  menu.style.top = `${Math.max(8, rect.top - 8)}px`;
  menu.style.transform = "translateY(-100%)";
  execModeMenu = menu;
  menu.querySelectorAll("[data-mode]").forEach((el) => {
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      setExecMode(el.dataset.mode);
    });
  });
  setTimeout(() => {
    document.addEventListener("click", function handler(e) {
      if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        closeExecModeMenu();
        document.removeEventListener("click", handler);
      }
    });
  }, 0);
}

async function openModelPicker() {
  try {
    await ModelSettings.loadCatalog();
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  const cat = ModelSettings.getCatalog();
  const items = [];
  (cat.providers || []).forEach((p) => {
    if (!p.enabled) return;
    (p.models || []).forEach((m) => {
      if (!m.enabled || m.role !== "chat" || !m.agent_compatible) return;
      const active = cat.active.provider_id === p.id && cat.active.model_id === m.id;
      items.push({ provider: p, model: m, active });
    });
  });
  if (!items.length) {
    toast("请先在设置中心配置并启用对话模型", "error");
    showSettings("model");
    return;
  }
  const menu = document.createElement("div");
  menu.className = "model-pick-menu";
  menu.innerHTML = items.map(({ provider, model, active }) => `
    <button class="model-pick-item ${active ? "active" : ""}" data-pid="${provider.id}" data-mid="${model.id}">
      <span class="model-pick-name">${escapeHtml(provider.name)} / ${escapeHtml(model.name)}</span>
      ${active ? '<span class="tag">当前</span>' : ""}
    </button>`).join("");
  const btn = $("#btn-model-pick");
  const rect = btn.getBoundingClientRect();
  menu.style.position = "fixed";
  menu.style.left = `${Math.max(8, rect.left)}px`;
  menu.style.top = `${rect.top - 8}px`;
  menu.style.transform = "translateY(-100%)";
  document.body.appendChild(menu);
  const close = () => menu.remove();
  setTimeout(() => {
    document.addEventListener("click", function handler(e) {
      if (!menu.contains(e.target) && e.target !== btn) {
        close();
        document.removeEventListener("click", handler);
      }
    });
  }, 0);
  menu.querySelectorAll(".model-pick-item").forEach((el) => {
    el.addEventListener("click", async () => {
      close();
      try {
        await api("/api/models/activate", {
          method: "POST",
          body: JSON.stringify({ provider_id: el.dataset.pid, model_id: el.dataset.mid }),
        });
        await ModelSettings.refresh();
        updateModelPickerLabel();
        await syncReasoningFromCatalog();
        toast("已切换模型", "ok");
      } catch (err) { toast(err.message, "error"); }
    });
  });
}

const modelPickStyle = document.createElement("style");
modelPickStyle.textContent = `
  .model-pick-menu{min-width:240px;max-width:360px;max-height:320px;overflow-y:auto;
    background:var(--card-bg);border:1px solid var(--border);border-radius:var(--radius-sm);
    box-shadow:0 12px 32px rgba(0,0,0,.12);z-index:150;padding:6px}
  .model-pick-item{display:flex;align-items:center;justify-content:space-between;gap:8px;
    width:100%;padding:9px 10px;border:none;background:transparent;text-align:left;
    font-size:13px;border-radius:8px;cursor:pointer;color:var(--text)}
  .model-pick-item:hover,.model-pick-item.active{background:var(--soft-bg)}
  .model-pick-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}`;
document.head.appendChild(modelPickStyle);

/* ============================================================
   设置中心：其他页面
   ============================================================ */
const pageHead = (title, desc) => `
  <div class="set-page-head">
    <div><h1 class="set-page-title">${title}</h1>
    <p class="set-page-desc">${desc}</p></div>
  </div>`;

const toggleHtml = (checked) => `
  <label class="toggle"><input type="checkbox" ${checked ? "checked" : ""}><span class="track"></span></label>`;

const card = (title, sub, rows) => `
  <div class="set-card">
    <div class="set-card-title">${title}</div>
    ${sub ? `<div class="set-card-sub">${sub}</div>` : ""}
    ${rows}
  </div>`;

const toggleRow = (title, desc, checked) => `
  <div class="set-row">
    <div class="set-row-main">
      <div class="set-row-title">${title}</div>
      ${desc ? `<div class="set-row-desc">${desc}</div>` : ""}
    </div>
    ${toggleHtml(checked)}
  </div>`;

const PAGES = {
  general: () => `
    ${pageHead("常规", "配置启动行为、工作空间与知识库等基础选项。")}
    ${card("启动与行为", "", `
      ${toggleRow("启动时恢复上次任务", "应用启动后自动打开退出前的任务", true)}
      ${toggleRow("自动检查更新", "发现新版本时在侧边栏提示", true)}
      ${toggleRow("发送匿名使用统计", "仅包含功能使用次数，不含任何对话内容", false)}
    `)}
    ${card("工作空间文件", "文件操作被安全限制在该目录内。点击文件可直接编辑。", `
      <div class="set-row" style="flex-wrap:wrap">
        <div class="set-row-main" style="flex-basis:100%">
          <div class="set-row-desc" id="ws-cwd-text">/ 加载中…</div>
        </div>
        <div id="ws-file-list" style="flex-basis:100%"></div>
      </div>
    `)}
    ${card("源码知识库（RAG）", "智能体检索项目源码时使用的向量知识库。", `
      <div class="set-row">
        <div class="set-row-main">
          <div class="set-row-title" id="rag-status-text">RAG 状态检测中…</div>
        </div>
        <button class="gray-btn" id="btn-rag-index">增量索引</button>
        <button class="gray-btn" id="btn-rag-reindex">全量重建</button>
      </div>
    `)}
  `,

  appearance: () => `
    ${pageHead(t("page.appearance.title"), t("page.appearance.desc"))}
    ${card(t("page.appearance.theme"), null, `
      <div class="set-row">
        <div class="set-row-main"><div class="set-row-title">${t("page.appearance.themeMode")}</div></div>
        <span class="segmented" data-pref="theme">
          <button data-v="light" class="${state.prefs.theme === "light" ? "active" : ""}">${t("theme.light")}</button>
          <button data-v="dark" class="${state.prefs.theme === "dark" ? "active" : ""}">${t("theme.dark")}</button>
          <button data-v="system" class="${state.prefs.theme === "system" ? "active" : ""}">${t("theme.system")}</button>
        </span>
      </div>
    `)}
    ${card(t("page.appearance.zoom"), null, `
      <div class="set-row">
        <div class="set-row-main"><div class="set-row-title">${t("page.appearance.zoomRatio")}</div></div>
        <span class="segmented" data-pref="zoom">
          ${[["0.9", "90%"], ["1", "100%"], ["1.1", "110%"], ["1.25", "125%"]].map(
            ([v, l]) => `<button data-v="${v}" class="${state.prefs.zoom === v ? "active" : ""}">${l}</button>`).join("")}
        </span>
      </div>
    `)}
    ${card("字体大小", null, `
      <div class="set-row">
        <div class="set-row-main"><div class="set-row-title">正文字号</div></div>
        <span class="segmented">
          <button>小</button><button class="active">标准</button><button>大</button>
        </span>
      </div>
    `)}
    ${card("强调色", "用于开关、高亮与重点操作按钮。", `
      <div class="set-row">
        <div class="set-row-main"></div>
        <span class="accent-swatches" style="display:flex;gap:10px">
          ${Object.keys(ACCENTS).map((name) => `
            <button data-accent="${name}" title="${name}"
              style="width:26px;height:26px;border-radius:50%;background:${ACCENTS[name][0]};
              box-shadow:${state.prefs.accent === name ? `0 0 0 3px var(--card-bg), 0 0 0 5px ${ACCENTS[name][0]}` : "none"}"></button>
          `).join("")}
        </span>
      </div>
    `)}
  `,

  browser: () => `
    ${pageHead("浏览器控制", "授权智能体在浏览器中打开网页、填写表单与提取页面内容。")}
    ${card("浏览器能力", "所有操作均在本机浏览器中执行，不会上传浏览数据。", `
      ${toggleRow("启用浏览器控制", "允许智能体打开和操作网页", true)}
      ${toggleRow("操作前确认", "执行点击、提交等动作前先征求我的同意", true)}
      ${toggleRow("保留登录状态", "复用本机浏览器的登录会话", false)}
    `)}
    ${card("允许访问的网站", "不在列表内的网站需要单独授权。", `
      <div class="set-row">
        <div class="set-row-main">
          <span class="tag">localhost</span><span class="tag">github.com</span><span class="tag">*.feishu.cn</span>
        </div>
        <button class="gray-btn">添加</button>
      </div>
    `)}
  `,

  computer: () => `
    ${pageHead("电脑控制", "授权智能体操作本机应用、终端与文件系统。")}
    ${card("电脑操作权限", "权限范围越广，智能体可自主完成的任务越多，请谨慎授权。", `
      ${toggleRow("启用电脑控制", "允许智能体操作本机应用", true)}
      ${toggleRow("文件读写", "在工作空间目录内读取与修改文件", true)}
      ${toggleRow("终端命令", "在沙箱中执行命令行指令", true)}
      ${toggleRow("跨应用操作", "控制其他桌面应用窗口", false)}
    `)}
  `,

  shortcuts: () => `
    ${pageHead("键盘快捷键", "查看并熟悉工作台的键盘操作。")}
    ${card("快捷键列表", null, `
      ${[
        ["新建任务", ["Ctrl", "N"]],
        ["快速搜索", ["Ctrl", "K"]],
        ["发送消息", ["Enter"]],
        ["换行输入", ["Shift", "Enter"]],
        ["停止生成", ["Esc"]],
        ["全屏模式", ["F11"]],
      ].map(([name, keys]) => `
        <div class="set-row">
          <div class="set-row-main"><div class="set-row-title">${name}</div></div>
          <kbd class="kbd-group">${keys.map((k) => `<span class="kbd-key">${k}</span>`).join("")}</kbd>
        </div>`).join("")}
    `)}
  `,

  memory: () => `<p class="set-page-desc">加载记忆状态…</p>`,

  subagents: () => `
    ${pageHead("子智能体", "为不同角色配置专用的子智能体，协同完成复杂任务。")}
    ${card("子智能体列表", null, `
      ${toggleRow("架构师", "负责方案设计与任务拆解", true)}
      ${toggleRow("工程师", "负责编写与修改代码", true)}
      ${toggleRow("评审员", "负责审查代码正确性与安全性", true)}
      <div class="set-row">
        <div class="set-row-main"></div>
        <button class="gray-btn"><svg class="ic sm"><use href="#i-plus"/></svg>添加子智能体</button>
      </div>
    `)}
  `,

  plugins: () => `
    ${pageHead("插件", "管理已安装的功能插件，扩展智能体能力。")}
    ${card("已安装插件", null, `
      ${toggleRow("提示词优化器", "三层架构将口语需求转换为工程化指令", true)}
      ${toggleRow("Git 助手", "提交信息生成与变更分析", true)}
      ${toggleRow("文档翻译", "多语言 Markdown 互译", false)}
      <div class="set-row">
        <div class="set-row-main"></div>
        <button class="gray-btn">获取更多插件</button>
      </div>
    `)}
  `,

  mcp: () => `<p class="set-page-desc">加载 MCP 状态…</p>`,

  skills: () => `
    ${pageHead("技能", "开关智能体可调用的专项技能。")}
    ${card("技能列表", null, `
      ${toggleRow("周报总结", "按项目维度梳理一周进展", true)}
      ${toggleRow("报错修复", "分析堆栈并定位根因", true)}
      ${toggleRow("PPT 制作", "根据需求生成演示文稿", true)}
      ${toggleRow("代码审查", "质量、性能与安全检查", true)}
    `)}
  `,

  commands: () => `
    ${pageHead("命令", "管理斜杠命令，在输入框中以 / 快速唤起。")}
    ${card("命令列表", null, `
      ${commandRow("/周报", "生成周报总结")}
      ${commandRow("/修 Bug", "排查并修复错误")}
      ${commandRow("/PPT", "制作演示文稿")}
      ${commandRow("/清空", "清空当前对话上下文")}
    `)}
  `,

  hooks: () => `
    ${pageHead("钩子", "在智能体生命周期事件发生时触发自定义请求。")}
    ${card("钩子列表", null, `
      ${commandRow("任务完成", "POST http://127.0.0.1:9000/hooks/done")}
      ${commandRow("工具调用前", "POST http://127.0.0.1:9000/hooks/tool")}
      <div class="set-row">
        <div class="set-row-main"></div>
        <button class="gray-btn"><svg class="ic sm"><use href="#i-plus"/></svg>添加钩子</button>
      </div>
    `)}
  `,

  stats: () => `<p class="set-page-desc">${t("page.stats.loading")}</p>`,

  guide: () => `
    ${pageHead("引导", "四步上手，快速发挥智能体的全部能力。")}
    ${card("上手指引", null, `
      ${guideStep("1", "选择并配置模型供应商", "在「模型设置」中添加你的 API Key", true)}
      ${guideStep("2", "描述你的第一个任务", "用自然语言告诉智能体你的需求", true)}
      ${guideStep("3", "授权工具与能力", "按需开启浏览器、电脑控制等权限", false)}
      ${guideStep("4", "尝试快捷任务", "点击周报总结、报错修复等快捷入口", false)}
    `)}
  `,
};

function mcpRow(name, desc, ok) {
  return `
    <div class="set-row">
      <div class="set-row-main">
        <div class="set-row-title">${escapeHtml(name)}</div>
        <div class="set-row-desc">${escapeHtml(desc)}</div>
      </div>
      <span class="dot ${ok ? "green" : ""}" style="width:8px;height:8px;border-radius:50%;background:${ok ? "var(--green)" : "var(--text-3)"}"></span>
    </div>`;
}
function commandRow(name, desc) {
  return `
    <div class="set-row">
      <div class="set-row-main">
        <div class="set-row-title">${name}</div>
        <div class="set-row-desc">${desc}</div>
      </div>
    </div>`;
}
function guideStep(no, title, desc, done) {
  return `
    <div class="set-row">
      <span style="width:28px;height:28px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;
        font-weight:700;font-size:13px;background:${done ? "var(--green)" : "var(--soft-bg)"};color:${done ? "#fff" : "var(--text-2)"}">${no}</span>
      <div class="set-row-main">
        <div class="set-row-title">${title}</div>
        <div class="set-row-desc">${desc}</div>
      </div>
    </div>`;
}

/* ---------------- 页面渲染与事件绑定 ---------------- */
function switchPage(page) {
  state.currentPage = page;
  $$(".set-nav-item").forEach((item) =>
    item.classList.toggle("active", item.dataset.page === page));
}

function fmtNum(n) {
  const v = Number(n) || 0;
  return v.toLocaleString();
}

function fmtBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) {
    const kb = v / 1024;
    return `${kb < 10 ? kb.toFixed(1) : Math.round(kb)} KB`;
  }
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

function fmtIndexTime(ts) {
  if (!ts) return "尚未索引";
  const d = new Date(Number(ts) * 1000);
  if (Number.isNaN(d.getTime())) return "尚未索引";
  return d.toLocaleString();
}

async function loadStatsPage(body) {
  body.innerHTML = PAGES.stats();
  try {
    const data = await api("/api/stats");
    const totals = data.totals || {};
    const trend = data.trend || [];
    const byLevel = data.by_reasoning_level || [];
    const maxTurns = Math.max(1, ...trend.map((d) => d.chat_turns || 0));
    const levelMeta = (typeof ThinkingSlider !== "undefined")
      ? ThinkingSlider.LEVELS : [];
    const levelLabel = (id) => levelMeta.find((l) => l.id === id)?.label || id;
    body.innerHTML = `
    ${pageHead(t("page.stats.title"), t("page.stats.desc"))}
    <div class="stat-grid">
      <div class="stat-tile"><div class="stat-num">${fmtNum(totals.tasks)}</div><div class="stat-label">${t("page.stats.tasks")}</div></div>
      <div class="stat-tile"><div class="stat-num">${fmtNum(totals.messages)}</div><div class="stat-label">${t("page.stats.messages")}</div></div>
      <div class="stat-tile"><div class="stat-num">${fmtNum(totals.tool_calls)}</div><div class="stat-label">${t("page.stats.tools")}</div></div>
      <div class="stat-tile"><div class="stat-num">${totals.hours || 0}h</div><div class="stat-label">${t("page.stats.hours")}</div></div>
      <div class="stat-tile"><div class="stat-num">${fmtNum(totals.total_tokens)}</div><div class="stat-label">${t("page.stats.tokens")}</div></div>
      <div class="stat-tile"><div class="stat-num">${fmtNum(totals.reasoning_tokens)}</div><div class="stat-label">${t("page.stats.reasoning")}</div></div>
    </div>
    ${card(t("page.stats.trend"), null, `
      <div class="set-row">
        <div class="bar-chart" style="flex:1">
          ${trend.map((d) => {
            const h = Math.round(((d.chat_turns || 0) / maxTurns) * 100);
            const label = (d.date || "").slice(5);
            return `<div class="bar-col">
              <div class="bar" style="height:${h}%"></div>
              <div class="bar-label">${escapeHtml(label)}</div>
            </div>`;
          }).join("")}
        </div>
      </div>
    `)}
    ${byLevel.length ? card(t("page.stats.byLevel"), null, `
      <div class="set-row" style="flex-direction:column;align-items:stretch;gap:8px">
        ${byLevel.map((row) => `
          <div class="set-row" style="padding:8px 0;border-bottom:1px solid var(--border)">
            <div class="set-row-main">
              <div class="set-row-title">${escapeHtml(levelLabel(row.level))}</div>
              <div class="set-row-desc">${row.turns} 轮 · ${fmtNum(row.total_tokens)} tokens · 均 ${row.avg_latency_ms}ms</div>
            </div>
          </div>`).join("")}
      </div>
    `) : ""}`;
  } catch (err) {
    body.innerHTML = `<p class="set-page-desc">${t("page.stats.loadFail")}：${escapeHtml(err.message || String(err))}</p>`;
  }
}

function renderPage(page) {
  switchPage(page);
  const body = $("#settings-body");
  if (page === "model") {
    ModelSettings.renderInto(body);
  } else if (page === "stats") {
    loadStatsPage(body);
  } else if (page === "memory") {
    loadMemoryPage(body);
  } else if (page === "mcp") {
    loadMcpPage(body);
  } else {
    body.innerHTML = PAGES[page]();
    if (page === "general") bindGeneralPage();
  }
}

function syncAppearancePage() {
  if (state.currentPage === "appearance" && $("#view-settings").classList.contains("active")) {
    $("#settings-body").innerHTML = PAGES.appearance();
  }
}

function bindRagButtons({ refreshStatus = true } = {}) {
  // 记忆页已用 /api/memory/status 渲染，勿再覆盖成「常规」页文案
  if (refreshStatus) refreshRagStatus();
  const ragIdx = $("#btn-rag-index");
  const ragFull = $("#btn-rag-reindex");
  if (ragIdx) ragIdx.addEventListener("click", () => runRagIndex("incremental"));
  if (ragFull) ragFull.addEventListener("click", () => {
    if (confirm("全量重建会先清空当前知识库，确定继续吗？")) runRagIndex("full");
  });
}

function bindGeneralPage() {
  loadWorkspaceFiles(state.cwd);
  bindRagButtons();
}

async function loadMemoryPage(body) {
  body.innerHTML = PAGES.memory();
  try {
    const data = await api("/api/memory/status");
    const sess = data.sessions || {};
    const rag = data.rag || {};
    const prefStats = data.preferences || {};
    const prefData = await api("/api/memory/preferences");
    const prefs = Array.isArray(prefData.items) ? prefData.items : [];
    const ragLine = rag.enabled
      ? `${fmtNum(rag.chunks)} 个代码块 · ${escapeHtml(rag.embedder || "")}`
      : (rag.message || "RAG 未启用");
    const ragTime = rag.enabled ? `最近索引：${fmtIndexTime(rag.indexed_at)}` : "";
    const prefCap = `${fmtNum(prefStats.count || prefs.length)} / ${fmtNum(prefStats.max_items || 500)} 条 · ${fmtBytes(prefStats.bytes || 0)} / ${fmtBytes(prefStats.max_bytes || 10485760)}`;
    const prefRows = prefs.length
      ? prefs.map((p) => {
          const on = !!p.enabled;
          const src = p.source === "conversation_summary" ? "对话摘要" : "手动";
          return `<div class="set-row" data-pref-id="${escapeHtml(p.id)}" style="flex-wrap:wrap;gap:8px;align-items:flex-start">
            <div class="set-row-main" style="flex:1;min-width:180px">
              <div class="set-row-title">${escapeHtml(p.content || "")}</div>
              <div class="set-row-desc">${src} · ${on ? "已启用" : "已停用"}</div>
            </div>
            <button class="gray-btn btn-pref-toggle" data-id="${escapeHtml(p.id)}" data-enabled="${on ? "1" : "0"}">${on ? "停用" : "启用"}</button>
            <button class="gray-btn btn-pref-del" data-id="${escapeHtml(p.id)}">删除</button>
          </div>`;
        }).join("")
      : `<div class="set-row"><div class="set-row-main">
          <div class="set-row-title">暂无偏好</div>
          <div class="set-row-desc">例如「回复时先给结论再给细节」「项目用 React」。不写入 RAG / history.db。</div>
        </div>`;
    body.innerHTML = `
    ${pageHead("记忆", "会话历史 · 源码知识库 · 用户偏好（三者分离）")}
    ${card("用户偏好", "跨会话约定，注入系统提示词；上限与存储一致。", `
      <div class="set-row" style="margin-bottom:8px">
        <div class="set-row-main">
          <div class="set-row-title">已用：${prefCap}</div>
          <div class="set-row-desc">启用 ${fmtNum(prefStats.enabled_count || 0)} 条会进入系统提示词</div>
        </div>
        <button class="gray-btn" id="btn-clear-prefs">清空偏好</button>
      </div>
      <div class="set-row" style="flex-wrap:wrap;gap:8px;margin-bottom:10px">
        <input id="pref-input" type="text" maxlength="2000" placeholder="输入一条偏好，例如：回复时先给结论再给细节"
          style="flex:1;min-width:200px;padding:8px 10px;border:1px solid var(--border, #ddd);border-radius:6px;background:var(--bg-input, #fff);color:inherit" />
        <button class="gray-btn" id="btn-add-pref">添加</button>
      </div>
      <div id="pref-list">${prefRows}</div>
    `)}
    ${card("会话记忆", "SQLite 会话历史（history.db）。", `
      <div class="set-row" style="flex-wrap:wrap;gap:8px">
        <div class="set-row-main">
          <div class="set-row-title">已用：${fmtBytes(sess.db_bytes)} / ${escapeHtml(sess.db_name || "history.db")}</div>
          <div class="set-row-desc">${fmtNum(sess.session_count)} 个会话，${fmtNum(sess.message_count)} 条消息</div>
        </div>
        <button class="gray-btn" id="btn-clear-sessions">清除全部会话</button>
      </div>
    `)}
    ${card("源码知识库（RAG）", "工作区源码切片，供 rag_search 检索。", `
      <div class="set-row" style="flex-wrap:wrap;gap:8px">
        <div class="set-row-main">
          <div class="set-row-title" id="rag-status-text">${ragLine}</div>
          <div class="set-row-desc">${ragTime}</div>
        </div>
        <button class="gray-btn" id="btn-rag-index" ${rag.enabled ? "" : "disabled"}>增量索引</button>
        <button class="gray-btn" id="btn-rag-reindex" ${rag.enabled ? "" : "disabled"}>全量重建</button>
      </div>
    `)}`;
    state.ragOk = !!rag.enabled;
    $("#btn-clear-sessions")?.addEventListener("click", clearAllSessions);
    $("#btn-clear-prefs")?.addEventListener("click", clearAllPreferences);
    $("#btn-add-pref")?.addEventListener("click", addPreferenceFromInput);
    $("#pref-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") addPreferenceFromInput();
    });
    $$(".btn-pref-toggle").forEach((btn) => {
      btn.addEventListener("click", () => togglePreference(btn.dataset.id, btn.dataset.enabled !== "1"));
    });
    $$(".btn-pref-del").forEach((btn) => {
      btn.addEventListener("click", () => deletePreference(btn.dataset.id));
    });
    bindRagButtons({ refreshStatus: false });
  } catch (err) {
    body.innerHTML = `<p class="set-page-desc">记忆状态加载失败：${escapeHtml(err.message || String(err))}</p>`;
  }
}

async function addPreferenceFromInput() {
  const input = $("#pref-input");
  const content = (input?.value || "").trim();
  if (!content) {
    toast("请输入偏好内容", "error");
    return;
  }
  try {
    await api("/api/memory/preferences", {
      method: "POST",
      body: JSON.stringify({ content, source: "user", enabled: true }),
    });
    if (input) input.value = "";
    toast("已添加偏好", "ok");
    if (state.currentPage === "memory") loadMemoryPage($("#settings-body"));
  } catch (err) {
    toast(err.message || String(err), "error");
  }
}

async function togglePreference(id, enabled) {
  try {
    await api(`/api/memory/preferences/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !!enabled }),
    });
    if (state.currentPage === "memory") loadMemoryPage($("#settings-body"));
  } catch (err) {
    toast(err.message || String(err), "error");
  }
}

async function deletePreference(id) {
  if (!confirm("确定删除这条偏好？")) return;
  try {
    await api(`/api/memory/preferences/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast("已删除", "ok");
    if (state.currentPage === "memory") loadMemoryPage($("#settings-body"));
  } catch (err) {
    toast(err.message || String(err), "error");
  }
}

async function clearAllPreferences() {
  if (!confirm("将清空全部用户偏好，不影响会话历史与 RAG。确定继续？")) return;
  try {
    const data = await api("/api/memory/clear", { method: "POST" });
    toast(`已清空 ${data.deleted || 0} 条偏好`, "ok");
    if (state.currentPage === "memory") loadMemoryPage($("#settings-body"));
  } catch (err) {
    toast(err.message || String(err), "error");
  }
}

async function clearAllSessions() {
  if (!confirm("将删除全部会话历史，不可恢复。确定继续？")) return;
  try {
    const data = await api("/api/sessions", { method: "DELETE" });
    state.currentSessionId = null;
    await loadSessions(true);
    showWelcomeView();
    toast(`已清除 ${data.deleted || 0} 个会话`, "ok");
    if (state.currentPage === "memory") loadMemoryPage($("#settings-body"));
  } catch (err) {
    toast(err.message || String(err), "error");
  }
}

async function loadMcpPage(body) {
  body.innerHTML = PAGES.mcp();
  try {
    const health = await api("/api/health");
    const servers = Array.isArray(health.mcp) ? health.mcp : [];
    const rows = servers.length
      ? servers.map((s) => {
          const ok = s.status === "ready";
          const desc = `${s.transport || ""} · ${ok ? "已连接" : "未连接"} · ${fmtNum(s.tools || 0)} 个工具`;
          return mcpRow(s.name, desc, ok);
        }).join("")
      : `<div class="set-row"><div class="set-row-main">
          <div class="set-row-title">未配置 MCP 服务器</div>
          <div class="set-row-desc">当前 config.yaml → mcp.servers 为空，不会默认启用 memory 或其他服务器。</div>
        </div>`;
    body.innerHTML = `
    ${pageHead("MCP 服务器", "状态来自 config.yaml → mcp.servers，与 /api/health 一致。")}
    ${card("服务器列表", null, `
      ${rows}
      <div class="set-row">
        <div class="set-row-main"></div>
        <button class="gray-btn" id="btn-mcp-add"><svg class="ic sm"><use href="#i-plus"/></svg>添加服务器</button>
      </div>
    `)}`;
    $("#btn-mcp-add")?.addEventListener("click", () => {
      toast("请在 config.yaml 的 mcp.servers 中添加，然后重启服务", "ok");
    });
  } catch (err) {
    body.innerHTML = `<p class="set-page-desc">MCP 状态加载失败：${escapeHtml(err.message || String(err))}</p>`;
  }
}

/* 设置页内通用交互（分段控件 / 强调色 / 静态开关） */
$("#settings-body").addEventListener("click", (e) => {
  const seg = e.target.closest(".segmented[data-pref]");
  if (seg) {
    const btn = e.target.closest("button");
    if (!btn) return;
    const pref = seg.dataset.pref;
    state.prefs[pref] = btn.dataset.v;
    savePrefs();
    seg.querySelectorAll("button").forEach((b) =>
      b.classList.toggle("active", b === btn));
    if (pref === "theme") applyTheme(btn.dataset.v);
    if (pref === "zoom") applyZoom(btn.dataset.v);
    return;
  }
  const swatch = e.target.closest("[data-accent]");
  if (swatch) {
    state.prefs.accent = swatch.dataset.accent;
    savePrefs();
    applyAccent(swatch.dataset.accent);
    syncAppearancePage();
    toast(t("toast.accent"), "ok");
  }
});

/* ============================================================
   工作空间文件浏览（设置·常规页内）
   ============================================================ */
async function loadWorkspaceFiles(path) {
  const listEl = $("#ws-file-list");
  if (!listEl) return;
  try {
    const data = await api(`/api/files/list?path=${encodeURIComponent(path)}`);
    state.cwd = path;
    $("#ws-cwd-text").textContent = "/ " + (data.path || "");
    listEl.innerHTML = "";

    if (path !== ".") {
      const up = document.createElement("button");
      up.className = "ws-file-row";
      up.innerHTML = `<svg class="ic sm"><use href="#i-folder"/></svg><span>..</span>`;
      up.addEventListener("click", () => loadWorkspaceFiles(parentPath(path)));
      listEl.appendChild(up);
    }
    data.items.forEach((item) => {
      const entry = document.createElement("button");
      entry.className = "ws-file-row";
      entry.innerHTML = `<svg class="ic sm"><use href="#i-${item.type === "dir" ? "folder" : "file"}"/></svg><span class="ws-f-name"></span>`;
      entry.querySelector(".ws-f-name").textContent = item.name;
      entry.addEventListener("click", () => {
        if (item.type === "dir") loadWorkspaceFiles(item.relpath);
        else openFileOverlay(item.relpath);
      });
      listEl.appendChild(entry);
    });
    if (!data.items.length) {
      listEl.innerHTML = `<div class="set-row-desc" style="padding:4px 0">(空目录)</div>`;
    }
  } catch (err) {
    listEl.innerHTML = `<div class="set-row-desc">文件列表加载失败：${escapeHtml(err.message)}</div>`;
  }
}

function parentPath(path) {
  if (!path || path === ".") return ".";
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  parts.pop();
  return parts.length ? parts.join("/") : ".";
}

/* ws 文件行样式（注入为页内类） */
const wsRowStyle = document.createElement("style");
wsRowStyle.textContent = `
  .ws-file-row{display:flex;align-items:center;gap:8px;width:100%;padding:6px 8px;border-radius:8px;font-size:13px;text-align:left;color:var(--text)}
  .ws-file-row:hover{background:var(--soft-bg)}
  .ws-file-row .ic{color:var(--text-2)}
  .ws-f-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}`;
document.head.appendChild(wsRowStyle);

/* ============================================================
   文件编辑遮罩
   ============================================================ */
async function openFileOverlay(relpath) {
  try {
    const data = await api(`/api/files/read?path=${encodeURIComponent(relpath)}`);
    state.currentFile = data.path;
    $("#file-overlay-title").textContent = data.path;
    $("#file-editor").value = data.content;
    $("#file-overlay").hidden = false;
    if (window.POContextClient) {
      POContextClient.onFileOpened(data.path);
    }
  } catch (err) { toast(err.message, "error"); }
}
async function saveOverlayFile() {
  if (!state.currentFile) return;
  try {
    await api("/api/files/write", {
      method: "POST",
      body: JSON.stringify({
        path: state.currentFile,
        content: $("#file-editor").value,
        create: false,
        overwrite: true,
      }),
    });
    toast("已保存 ✓", "ok");
    if (window.POContextClient) POContextClient.onFileSaved(state.currentFile);
  } catch (err) { toast("保存失败: " + err.message, "error"); }
}

/* ============================================================
   RAG 状态与索引
   ============================================================ */
async function refreshRagStatus() {
  const el = $("#rag-status-text");
  if (!el) return;
  try {
    const data = await api("/api/rag/status");
    state.ragOk = true;
    el.innerHTML = `知识库：<span style="color:var(--green)">就绪</span>
      <span class="set-row-desc">${data.chunks} 个切片 · ${escapeHtml(data.embedder || "")}</span>`;
  } catch (_) {
    state.ragOk = false;
    el.innerHTML = `知识库：<span style="color:var(--accent)">未就绪</span>
      <span class="set-row-desc">检查 embedding 配置后重启</span>`;
  }
}
async function runRagIndex(mode) {
  if (!state.ragOk) {
    await refreshChainStatus();
    if (!state.ragOk) { toast("RAG 当前未就绪", "error"); return; }
  }
  try {
    const data = await api(mode === "full" ? "/api/rag/reindex" : "/api/rag/index",
      { method: "POST" });
    toast(`索引完成：扫描 ${data.scanned}，新增 ${data.added}，共 ${data.chunks_total} 切片`, "ok");
    if (state.currentPage === "memory") loadMemoryPage($("#settings-body"));
    else refreshRagStatus();
  } catch (err) { toast("索引失败: " + err.message, "error"); }
}

/* ============================================================
   问候语 & 事件绑定 & 初始化
   ============================================================ */
function setGreeting() {
  const el = $("#greeting");
  if (!el) return;
  const h = new Date().getHours();
  let key = "greeting.evening";
  if (h >= 5 && h < 11) key = "greeting.morning";
  else if (h >= 11 && h < 13) key = "greeting.noon";
  else if (h >= 13 && h < 18) key = "greeting.afternoon";
  el.textContent = t(key);
}

function bindEvents() {
  /* 侧边栏：新建任务 / 搜索 / 知识库 / 设置 */
  $("#btn-new-task").addEventListener("click", () => {
    if (state.sending) return;
    state.currentSessionId = null;
    showWelcomeView();
    renderProjects();
    $$(".sb-menu-item").forEach((el) => el.classList.remove("active"));
    $("#btn-new-task").classList.add("active");
    $("#input-box").focus();
  });
  $("#btn-quick-search").addEventListener("click", () => $("#input-box").focus());
  $("#btn-rag-panel")?.addEventListener("click", () => {
    setSidebarPanel("groups");
    runRagIndex("incremental");
  });
  $("#btn-open-settings")?.addEventListener("click", () => showSettings("skills"));
  $("#btn-sidebar-expand")?.addEventListener("click", toggleSidebarExpanded);
  $("#btn-sidebar-filter")?.addEventListener("click", toggleTaskFilter);
  $("#btn-sidebar-trash")?.addEventListener("click", toggleArchivedView);
  /* chain-fs / chain-rag / chain-agent / cf-rag / cf-access 由 chain-modules.js 绑定优化入口 */
  $("#seg-projects")?.addEventListener("click", () => setSidebarPanel("projects"));
  $("#seg-groups")?.addEventListener("click", () => setSidebarPanel("groups"));
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#task-ctx-menu")) closeCtxMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeCtxMenu();
      closeTraceModal();
      closeExecModeMenu();
      closeThinkingPopover();
    }
  });
  $("#btn-trace-close")?.addEventListener("click", closeTraceModal);
  $("#trace-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "trace-modal") closeTraceModal();
  });
  $$(".mode-tab").forEach((tab) => {
    tab.addEventListener("click", () => setWorkMode(tab.dataset.mode));
  });

  /* 对话头部 */
  $("#btn-chat-back").addEventListener("click", () => {
    showWelcomeView();
    renderProjects();
  });
  $("#btn-chat-new").addEventListener("click", () => {
    state.currentSessionId = null;
    showWelcomeView();
    renderProjects();
    $("#input-box").focus();
  });

  /* 输入框 */
  const input = $("#input-box");
  input.addEventListener("input", () => {
    autoGrow(input);
    handleInputSuggest();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const text = input.value;
      input.value = "";
      autoGrow(input);
      resolveOutgoingPrompt(text).then(sendMessage);
    }
    if (e.key === "Escape" && state.abortCtrl) state.abortCtrl.abort();
  });
  $("#btn-send").addEventListener("click", () => {
    const text = input.value;
    input.value = "";
    autoGrow(input);
    resolveOutgoingPrompt(text).then(sendMessage);
  });
  $("#btn-stop").addEventListener("click", () => {
    if (state.abortCtrl) state.abortCtrl.abort();
  });
  $("#btn-model-pick").addEventListener("click", (e) => {
    e.stopPropagation();
    openModelPicker();
  });
  $("#btn-thinking-pick")?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (thinkPopover) closeThinkingPopover();
    else openThinkingPopover();
  });
  $("#btn-exec-mode")?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (execModeMenu) closeExecModeMenu();
    else openExecModeMenu();
  });
  $("#btn-agent-role")?.addEventListener("click", (e) => {
    e.stopPropagation();
    openAgentRolePicker();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#input-suggest") && e.target !== input) hideSuggest();
  });

  /* 快捷任务 */
  $$(".quick-chip").forEach((chip2) => {
    chip2.addEventListener("click", () => {
      input.value = chip2.dataset.prompt;
      autoGrow(input);
      input.focus();
    });
  });

  /* 右下角账户区 */
  $("#user-card").addEventListener("click", (e) => {
    e.stopPropagation();
    if (state.prefs.loggedIn === false) {
      toast(t("toast.reconnect"));
      return;
    }
    popup.hidden ? openPopup() : closePopup();
  });
  $("#btn-gear").addEventListener("click", (e) => {
    e.stopPropagation();
    showSettings("model");
  });
  document.addEventListener("click", (e) => {
    if (!popup.hidden
      && !popup.contains(e.target)
      && e.target.closest("#user-card") === null) {
      closePopup();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePopup();
    if ((e.ctrlKey || e.metaKey) && e.key === "n") {
      e.preventDefault(); $("#btn-new-task").click();
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault(); $("#input-box").focus();
    }
  });

  /* 设置视图 */
  $$(".set-nav-item").forEach((item) => {
    item.addEventListener("click", () => renderPage(item.dataset.page));
  });
  $("#btn-back-workspace").addEventListener("click", showWorkspace);

  /* 窗口控制（浏览器内仅视觉/部分可用） */
  $("#btn-fullscreen").addEventListener("click", () => {
    if (!document.fullscreenElement) document.documentElement.requestFullscreen?.();
    else document.exitFullscreen?.();
  });
  $("#btn-maximize").addEventListener("click", () => $("#btn-fullscreen").click());
  $$(".win-ic.close").forEach((btn) => btn.addEventListener("click", () => {
    toast("这是浏览器中的演示界面，无法真正关闭窗口哦～");
  }));

  /* 本机项目 */
  $("#btn-add-project").addEventListener("click", openProjectModal);
  $("#project-switch").addEventListener("click", openProjectModal);
  $("#btn-project-modal-close").addEventListener("click", closeProjectModal);
  $("#btn-project-confirm").addEventListener("click", confirmAddProject);
  $("#project-modal").addEventListener("click", (e) => {
    if (e.target.id === "project-modal") closeProjectModal();
  });

  /* 文件遮罩 */
  $("#btn-file-overlay-back").addEventListener("click", () => {
    $("#file-overlay").hidden = true;
    state.currentFile = null;
  });
  $("#btn-file-overlay-save").addEventListener("click", saveOverlayFile);
  $("#file-editor").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      saveOverlayFile();
    }
  });
}

/* ---------------- 启动 ---------------- */
ModelSettings.init({
  api,
  toast,
  escapeHtml,
  onActiveChange: () => updateModelPickerLabel(),
});
loadPrefs();
applyAllPrefs();
bindEvents();
setWorkMode(state.activeMode);
updateAgentRoleLabel();
if (window.ChainModules) {
  ChainModules.init({
    api,
    toast,
    t,
    refreshChainStatus,
    setSidebarPanel,
    showSettings,
    pollMs: 30000,
  });
}
loadRuntime(true);
loadSessions(true);
refreshChainStatus();
ModelSettings.loadCatalog().then(() => {
  updateModelPickerLabel();
  return syncReasoningFromCatalog();
}).catch(() => {});
