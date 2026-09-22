# 本地编码智能体（Local Coding Agent）

一个完全运行在**本地**的 AI 编码助手：ChatGPT 风格网页界面 + SSE 流式输出 + 可插拔 Agent 框架（默认 `native_react`，控制面为自研实现）+ 项目源码 RAG 知识库 + RestrictedPython 安全代码执行。

> 前端使用**原生 HTML/CSS/JavaScript**，零框架依赖、零打包步骤，打开即用。

---

## ⚠️ 安全警告（务必阅读）

| 警告项 | 说明 |
|--------|------|
| 🚫 **禁止公网部署** | 本应用具备本地文件读写与代码执行能力，**仅限 `127.0.0.1` 本机使用**，严禁部署到公网或多用户环境。 |
| 🔒 工作空间锁定 | 所有文件操作被强制限制在 `config.yaml -> server.workspace_root` 目录内，路径穿越（`../`、绝对路径、符号链接）一律拒绝。 |
| 🧪 执行器隔离 | 代码通过 RestrictedPython 在独立子进程中执行：禁止 `import`、禁止文件/网络/系统 API，带超时强杀。**这是“降低风险”而非“绝对安全”，请勿执行不可信来源代码。** |
| 🔑 API Key | 使用云端模型（GLM/OpenAI）时，代码内容会发送至对应服务商；敏感代码请改用 Ollama 本地模型。 |

---

## 功能一览

- **对话界面**：左侧会话列表（新建/删除/切换，持久化）、右侧气泡对话、SSE 逐字流式输出
- **消息类型区分**：用户消息（右）、AI 消息（左）、工具调用（可展开的特殊面板，含参数与返回结果）
- **代码块**：语言标识、语法着色、一键复制、超长代码折叠/展开
- **本地文件系统操控**：工作区内目录浏览、读取/创建/修改、关键词/正则搜索、行级精确编辑；工作区外经 `local_access.roots` 逐根授权，走 `fs_*` 工具，写入需审批卡片确认
- **本机系统数据**：`sys_*` 只读查询（进程/磁盘/内存/环境变量等，敏感字段脱敏）；受控动作（通知/打开路径/启动白名单应用）默认关闭
- **代码执行**：RestrictedPython 隔离执行，完整捕获 stdout/stderr/异常，超时强杀
- **源码 RAG**：tree-sitter 智能切片（不可用时自动降级）+ Chroma 本地向量库 + 文件增量更新
- **可插拔 Agent**：`state_loop`（自研状态驱动主循环，需求拆解→规划→读码→执行→验收→迭代全链路）/ `native_react`（默认，零额外依赖的手写 ReAct）/ `autogen` / `llamaindex` / `crewai`，改一行配置即可切换，工具集完全复用。**LangGraph 已移除**，控制面为自研实现（架构见 `docs/agent-runtime-architecture.md`）
- **多模型兼容**：Ollama、智谱 GLM、任意 OpenAI 兼容接口；优先原生 Function Call

## 快速开始

### 1. 环境要求

- Python 3.10 ~ 3.12（Windows / macOS / Linux）
- 如需本地模型：安装 [Ollama](https://ollama.com/) 并拉取模型，例如
  ```bash
  ollama pull qwen2.5-coder:7b
  ollama pull nomic-embed-text      # RAG 向量嵌入（可选）
  ```

### 2. 安装依赖

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

> 安装 ChromaDB / RestrictedPython 如遇编译问题，请升级 pip：`python -m pip install -U pip`。

### 3. 修改配置

三处分开，避免把密钥和角色写在同一个文件里：

| 文件 | 改什么 |
|------|--------|
| `settings.json` | 项目清单、监听地址、工作空间、`history.db` 与向量库路径 |
| `config.yaml` | 模型、RAG、执行器超时 |
| `agents/agent.yaml` | 框架、技能、提示词路径。工具由技能声明，不另列一份 |
| `.env` | API Key 与模型地址（从 `.env.example` 复制） |

- **Ollama**：默认配置即可（`http://127.0.0.1:11434/v1`，key 随意）
- **智谱 GLM**：在 `.env` 中设置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`，或改 `config.yaml` 的 `llm` 段。环境变量优先。

### 4. 启动

```bash
# Windows
start.bat                # 或 python run.py
start.bat dev            # 开发模式（热重载，并记录工具调用）
python run.py --debug    # 只记工具日志，不热重载。日志在 data/logs/agent.log

# macOS / Linux
./start.sh
./start.sh dev
```

浏览器打开 **http://127.0.0.1:8000** 即可使用。

## 使用说明

1. 左上角 **+ 新建会话**，在输入框中描述编码需求（例如“帮我写一个快速排序并运行测试”）。
2. AI 会自动 ReAct 循环：思考 → 调用工具（浏览/读取/编辑/检索/执行）→ 观察结果 → 继续，直至完成，全过程实时展示。
3. 左侧切到 **“文件”页签** 可直接浏览工作空间；对话中提到的工具调用可点击展开查看参数与返回。
4. **RAG 知识库**：在左下角点击「重建索引」，或调用 API：
   ```bash
   curl -X POST http://127.0.0.1:8000/api/rag/index    # 增量索引（自动跳过未变更文件）
   curl -X POST http://127.0.0.1:8000/api/rag/reindex  # 全量重建
   ```
5. 提问涉及项目代码时，Agent 会自动使用 `rag_search` 做语义检索。
6. **本机文件 / 系统数据**（默认关闭）：在 `config.yaml` 打开 `local_access.enabled` 并填写 `roots`，对话里先 `fs_roots` 再读；系统状态打开 `system.enabled`（需 `pip install psutil`）。写入会弹出确认卡片，60 秒不点等于拒绝。

## 切换 Agent 框架

控制面不依赖任何编排库（**LangGraph 已移除**）。编辑 `agents/agent.yaml` 的 `framework`：

```yaml
framework: "native_react"   # state_loop | native_react | autogen | llamaindex | crewai
```

也可以不改文件，启动前设置 `AGENT_FRAMEWORK`（例如 `set AGENT_FRAMEWORK=state_loop`）。

- `state_loop`：**自研状态驱动主循环**。主循环由代码的转移表推进，模型只在「拆解」与「决策」两处产出结构化决策；写盘/起进程一律先过权限分级；任务完成由验收命令的退出码判定，而不是模型宣布。相比 ReAct 系框架，它多了任务图、写前快照回滚、失败分类与停滞检测、子代理委派。
  需要的模型能力是**原生 Function Calling**（会强制调用 `submit_decision`）。相关配置在 `agents/agent.yaml`：`exec_mode`（执行档）、`max_replan`、`max_attempts`、`context_char_budget`、`delegate_parallel`。
- `native_react`：默认，零额外依赖的手写精简 ReAct，资源占用最低；模型不支持强制函数调用时它有文本动作兜底，因此**默认框架刻意保守地留给了它**。
- `autogen` / `llamaindex` / `crewai`：可选框架，首次切换时按 `requirements.txt` 末尾注释安装对应依赖即可；**统一工具集无需任何改动**
- 若配置残留 `langgraph`，启动时会明确报错并提示替代方案，不会静默回退
- 执行档三选一：`auto_workspace`（默认，工作区内自动执行，改外部系统需确认）/ `confirm_writes`（写盘与命令都要确认）/ `plan`（先出计划待确认）。**不提供「完全访问」档**
- 本机文件与系统数据在 `config.yaml` 的 `local_access` / `system`：默认关闭；开启后只读可直接用，写入与系统动作走同一条审批通道（`native_react` 与 `state_loop` 都会弹出确认卡片，60 秒未答复 = 拒绝）
- MCP 工具在 `config.yaml` 的 `mcp.servers` 里配置；它不依赖具体框架，与内置工具共用同一套权限判定与事件流

技能开关在同一文件的 `skills` 列表。关掉某个技能后，它声明的工具不会再交给模型。例如去掉 `code_interpreter` 后，就不能再 `edit_file`、`write_file` 或 `run_python_code`。提示词在 `prompts/system.md`、`prompts/task.md`、`prompts/examples.md`。`role` 和 `goals` 会一并写入系统提示词。

## 项目结构

对齐主流 Agent 分层：**核心运行时**（agents / tools / memory / skills / prompts）→ **服务层**（app）→ **沙箱与数据**（workspaces / data）→ **开发与验证**（tests / scripts）。

```
222222/
├── agents/                        # Agent 运行时（编排、状态机、框架适配）
│   ├── agent.yaml                 #   角色、目标、技能、框架、执行档
│   ├── agent.py                   #   装配入口（提示词 + 工具 + 记忆 + MCP + 框架）
│   ├── base.py / registry.py      #   统一协议与框架工厂
│   ├── state_loop/                #   自研状态驱动主循环（见 docs/agent-runtime-architecture.md）
│   └── *_agent.py / native_react.py
├── tools/                         # 原子工具（Tool 契约，所有框架共用）
│   ├── base.py / shell.py / mcp/
│   ├── file_tools.py / fs_tools.py / system_tools.py
│   └── api_client.py / executor.py
├── skills/                        # 技能 = 工具组合 + 任务说明
├── memory/                        # 会话历史 + 向量记忆
├── prompts/                       # 提示词模板（system / task / examples）
├── app/                           # HTTP API + 原生前端外壳
│   ├── main.py / api/ / web/
├── workspaces/                    # Agent 沙箱（文件操作锁定在此）
│   ├── demo/                      #   默认工作空间（config.yaml workspace_root）
│   └── e2e/                       #   端到端验证用例项目
├── data/                          # 运行时数据（gitignore，自动创建）
│   ├── history.db / chroma/ / sessions/ / memory/preferences.json
│   ├── .workbuddy/memory/         #   人工笔记（不参与 Agent 记忆，设置页不展示）
│   └── logs/                      #   agent.log、审计、诊断产物
├── scripts/                       # 开发/诊断脚本（不进主运行时）
│   ├── e2e/                       #   真实模型端到端驱动
│   ├── probes/                    #   LLM API 链路探针
│   └── diagnostics/               #   一次性排障脚本
├── tests/                         # 单元 + 集成测试
├── docs/                          # 架构与方案文档
├── config.yaml                    # 模型、RAG、执行器、MCP
├── settings.json                  # 监听地址、路径、项目清单
├── .env.example
├── requirements.txt
└── run.py                         # 启动入口
```

## 架构说明

```
浏览器(原生JS, fetch+SSE)
        │  HTTP / SSE
FastAPI (app/api/*)
        │
agents/agent.py  ← agents/agent.yaml + prompts/*.md
        │
Native ReAct / state_loop（自研状态驱动主循环）/ AutoGen / LlamaIndex / CrewAI
        │
skills（启用哪些技能，就暴露哪些工具）
        │
ToolRegistry ── file_tools / search / calculator
              ── shell（工作区命令，L3）
              ── tools/mcp（MCP 桥接工具）
              ── memory.vector_store（rag_search）
              ── executor（RestrictedPython）
        │
memory/history.py（data/history.db，旧 JSON 会导入一次）
        │
tools/api_client.py ── Ollama / GLM / OpenAI 兼容
```

设计要点：

1. **工具与框架解耦**：工具只依赖 `Tool` 抽象，注册一次，五个框架共用。技能只声明工具和提示词，不再包一层执行器。
2. **提示词是文件**：`prompts/*.md` 由 `agents/agent.yaml` 引用，和代码一起版本管理。
3. **记忆分成三段**：会话在 SQLite `history.db`；源码在 Chroma；跨会话用户偏好在 `data/memory/preferences.json`（不进 RAG）。`data/.workbuddy/memory/` 是人工笔记，不参与 Agent 记忆，设置页不展示。旧的 `data/sessions/*.json` 会在启动时导入，已存在的会话 id 不会覆盖。
4. **流式事件统一**：无论哪个框架，均产出 `token / tool_call / tool_result / thought / done / error` 事件。
5. **失败可降级**：tree-sitter、embedding 服务、可选框架缺失都不会导致主程序崩溃。

## API 摘要

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat/stream` | SSE 流式对话（body: session_id, message） |
| GET | `/api/sessions` | 会话列表 |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions/{id}` | 会话详情（消息记录） |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| GET | `/api/files/list` | 列目录（参数 path） |
| GET | `/api/files/read` | 读文件（参数 path） |
| POST | `/api/files/write` | 写文件 |
| POST | `/api/files/edit` | 行级/块级编辑 |
| GET | `/api/files/search` | 关键词/正则搜索 |
| POST | `/api/rag/index` | 增量索引 |
| POST | `/api/rag/reindex` | 全量重建 |
| GET | `/api/rag/status` | 索引状态 |
| GET | `/api/memory/status` | 会话 / RAG / 偏好聚合状态 |
| GET/POST | `/api/memory/preferences` | 列出 / 新增用户偏好 |
| PATCH/DELETE | `/api/memory/preferences/{id}` | 更新 / 删除偏好 |
| POST | `/api/memory/clear` | 清空用户偏好 |

## 运行测试

```bash
python -m unittest discover -s tests -p "test_*.py"
```

- **不要用 pytest**：测试是 `unittest` 写的，项目也没把 pytest 列进依赖。
- **不要加 `-t .`**：`tests/` 没有 `__init__.py`，加了会报 `Start directory is not importable`。
- 测试**不依赖 Ollama 或任何真实模型**：需要模型的地方用 `tests/fake_llm_server.py`
  （假的 OpenAI 兼容服务），需要 MCP 的地方用 `tests/fake_mcp_server.py`（假 stdio server）。
- 覆盖范围：转移表与停止条件、权限分级、写前快照与回滚、任务图校验、沙箱终端（真实起子进程）、
  失败分类与停滞检测、MCP 协议与桥接，以及**从 `/api/chat/stream` 打进去的 HTTP 端到端链路**。
- 另有 `test_source_hygiene.py` 做源码卫生检查（空字节 / 零宽字符 / 语法 / 关键模块可导入）。

## 常见问题

- **Q: Ollama 明明开着，程序却说连不上（或报 502）？** A: 多半是本机地址被系统代理拦了。
  若设了 `HTTP_PROXY` / `HTTPS_PROXY`，httpx 默认会把 `127.0.0.1:11434` 的请求也送进代理。
  本项目对**本机地址自动绕过代理**（`tools/net.py`）；若仍不通，检查代理环境变量或改用
  `NO_PROXY=127.0.0.1,localhost`。
- **Q: 模型不支持 Function Call 怎么办？** A: 请换用具备工具调用能力的模型（如 qwen2.5-coder、glm-4 系列）；`native_react` 框架额外内置 JSON 文本协议兜底。
- **Q: RAG 报 embedding 错误？** A: 确认已 `ollama pull nomic-embed-text`；或在配置中关闭 `llm.embedding.enabled`，系统将使用哈希向量降级。
- **Q: 为什么文件操作提示“路径越界”？** A: 这是安全机制，目标路径必须位于 `workspace_root` 之内。

## 免责声明

本软件按“现状”提供，仅用于**本机学习与开发辅助**。RestrictedPython 沙盒不能替代操作系统级容器隔离，请勿在生产环境、公网环境或处理不可信代码时使用。
