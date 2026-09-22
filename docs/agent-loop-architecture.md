# 自研 Agent 执行框架：状态驱动主循环

状态日期：2026-09-21。本文是实现规格，不是口号。本轮只交付设计，不改运行时代码，不改 `agents/agent.yaml` 的默认框架。

> **2026-09-21 修订（后续）**：LangGraph 已按最终决定从本仓库移除，默认框架改为 `native_react`，
> 代码层已完成（`requirements.txt` / `agents/registry.py` / `agents/agent.py` / `app/config.py` / `README.md`），
> `agents/langgraph_agent.py` 已删除。因此本文 A1、第 2 节的调用链路图中 LangGraph 分支、
> 以及第 21 节中「保留 LangGraph」的表述**均已作废**；其余状态机、转移表、权限分级、
> 失败分类、数据结构、上下文预算等内容仍然有效。架构层面的权威文档是
> [`agent-runtime-architecture.md`](./agent-runtime-architecture.md)，本文作为其模块级实现规格附录保留。


## 0. 结论

在现有 `BaseAgent.astream_run` 之外新增框架名 `state_loop`。控制面是纯 Python 显式状态机，不引入 LangGraph，也不把 `prompts/task.md` 的六步清单再包一层 ReAct。模型只在 `decompose`、`decide`、`repair` 三个状态产出类型化 `Decision`。任务图、权限、写前快照、验收命令、子代理合并由机器执行。MCP 工具在进程启动时注册进现有 `ToolRegistry`，调用时仍走同一条 `authorize → act` 路径。

一句话：一次用户消息对应一张可持久化的 `LoopState`；主循环按转移表推进「拆解 → 规划任务图 → 强制读码 → 授权后执行 → 机器验收 → 回滚或增量修复」，直到全部任务完成或命中停止条件。

## 1. 假设（未再向用户确认）

这些假设若与后续产品决定冲突，先改本文再写代码。

| ID | 假设 |
| --- | --- |
| A1 | ~~新框架名为 `state_loop`，经 `agents/registry.py` 注册。在切片有测试之前，`agents/agent.yaml` 的 `framework` 保持 `langgraph`，不静默切换默认。~~ **已作废**：最终决定 LangGraph 直接移除，默认框架先落到 `native_react`；`state_loop` 切片通过测试后再成为默认。 |
| A2 | 运行时语言是本仓库的 Python 3.10–3.12，不是 TypeScript。接口写成与 `AgentDependencies`、`ToolRegistry`、`WorkspaceSecurity` 兼容的 dataclass。 |
| A3 | 部署形态不变：只监听 `127.0.0.1`，文件路径继续由 `WorkspaceSecurity` 锁定在 `config.yaml` 的 `server.workspace_root`。 |
| A4 | 不依赖工作区是 git 仓库。回滚用写前字节快照 `FileJournal`，不调用 `git checkout`。 |
| A5 | 「终端」的原语是工作区内的 argv 子进程，不是 `shell=True` 拼字符串。Windows 默认不走 bash；只有决策显式请求 `shell` 形态时才用 `powershell -NoProfile -Command`，且仍受 L3 允许列表约束。POSIX 上对应 `bash -lc` 同样受允许列表约束。 |
| A6 | 现有 `RestrictedPython` 沙箱保留，语义不变：禁止 import，看不见工作区文件。它是 L2，不能冒充编译器或测试运行器。跑 pytest、ruff、pyright 走 L3 `WorkspaceCommandRunner`。 |
| A7 | 本地模型（默认 `qwen2.5-coder:7b`）没有可靠 tokenizer。上下文预算用字符估算：`tokens ≈ chars / 2`（中英混合的保守上界，宁早压缩）。不引入 tiktoken 作为硬依赖。 |
| A8 | 默认执行档是 `auto_workspace`：L0–L3 在工作区内自动执行，L4（会改外部状态的 MCP）暂停等人确认，L5 直接拒绝。另有 `plan` 与 `confirm_writes` 两档，见第 8 节。 |
| A9 | 子代理最大深度为 1（禁止孙子代理），默认最大并发 3，只适用于写路径租约不相交的任务。子代理不写会话历史，只把 `TaskResult` 交回父循环。 |
| A10 | 第一期不改前端协议的必填字段。`app/web/app.js` 的 `handleStreamEvent` 只认识 `session/token/thought/tool_call/tool_result/error/done/saved`。计划、验证、回滚摘要同时发 `thought`，新事件类型作为增量，旧前端忽略即可。 |
| A11 | LSP 不是进程内语言服务器。验收阶段若 `ruff` 或 `pyright` 在 PATH 上，则对本次触碰的文件跑一次；不在 PATH 上就跳过并在验证报告里写明 `skipped`，不假装通过。 |
| A12 | MCP 只做客户端。支持 stdio、SSE、Streamable HTTP。配置放 `config.yaml` 的 `mcp.servers`，不实现把本进程暴露成 MCP Server。 |
| A13 | 对比 Trae / Cursor / Zcode 只使用公开文档与公开产品说明。未公开的循环内部实现一律标「公开信息推断」，不写成事实。 |

## 2. 当前链路与缺口

现有调用链：

```text
POST /api/chat/stream          app/api/routes_chat.py
  → HistoryStore.get_llm_messages
  → BaseAgent.astream_run       agents/base.py
       LangGraphReActAgent      agents/langgraph_agent.py
       或 NativeReActAgent      agents/native_react.py
  → ToolRegistry.execute        tools/base.py
       file_tools / search / rag / calculator / run_python_code
  → WorkspaceSecurity           tools/workspace.py
  → SSE 事件                    agents/events.py
```text

| 已有 | 实际行为 | 对目标链路的缺口 |
| --- | --- | --- |
| `prompts/task.md` | 把「弄清目标 → 定位 → 阅读 → 修改 → 验证 → 报告」写进系统提示词 | 模型可以跳步。没有任务对象，循环无法判断哪一步没做 |
| `LangGraphReActAgent` | `ReActState` 只有 `messages` 与 `pending_calls`；`agent` 有工具调用就去 `tools`，否则结束 | 这是 ReAct 图，不是任务状态机。`tools_node` 用 `asyncio.gather` 并发往同一 `messages` 列表 append，结果顺序不确定。新循环不要复制这个写法 |
| `NativeReActAgent` | 原生 function call，失败则从正文里抠 JSON 动作，没有动作就当最终答复 | 停止条件是「模型不再调用工具」，不是「验收通过」 |
| `ToolRegistry.execute` | 异常吞成字符串，以 `[工具错误]` 前缀表示失败 | 机器无法区分参数错误、越界、超时、测试失败。新循环在执行层外再包一层 `ToolOutcome` |
| `WorkspaceSecurity` | 解析真实路径，拒绝工作区外与符号链接逃逸 | 保留，作为 L0/L1/L3 的路径门闩。它不表示命令权限 |
| `RestrictedPythonExecutor` | 子进程 + 超时强杀，禁止 import | 不能编译项目、不能跑 pytest、不能读刚改的文件 |
| `skills/*.py` | 技能 = 工具名元组 + 一段提示词 | 保留为「模型可见工具白名单」。技能不成为状态 |
| `agents/events.py` + `routes_chat.py` | 统一 SSE；工具调用记入 SQLite 消息 | 没有计划、验证、回滚、权限请求的持久字段。第一期把这些摘要放进 `thought` 文本，避免改表 |
| `config.yaml` 的 `agent: {}` | 框架实际以 `agents/agent.yaml` 和 `AGENT_FRAMEWORK` 为准 | 循环限额、权限档、MCP、预算新增配置，不塞进提示词 |

仓库内不存在：规划器、任务图、命令运行器、诊断器、写前日志、子代理、MCP 客户端、验收状态。

## 3. 方案取舍

| 方案 | 做法 | 否决或采纳原因 |
| --- | --- | --- |
| A. 加厚提示词 | 把六步写得更严，仍用 `native_react` / LangGraph | 用户明确拒绝。本地小模型不会稳定遵守顺序，失败也无法分类 |
| B. 给 LangGraph 加节点 | 在现有图上增加 plan/verify 节点 | 用户明确不要 LangGraph。节点仍易退化成「模型说了算的边」 |
| C. 显式状态机（采纳） | `StateLoopAgent` 持有 `LoopState`，转移表是代码，模型只填 `Decision` | 能单测转移，不增加依赖，能接上现有 `astream_run` 与 `ToolRegistry` |
| D. 事件溯源全程落盘 | 每步写 WAL，崩溃后续跑 | 正确但超出第一刀。本设计只把 `FileJournal` 放内存，并预留 `to_dict()`，不在第一期写 WAL |

## 4. 模块落点

新增，不替换现有五个框架：

```text
agents/state_loop/
  agent.py         StateLoopAgent(BaseAgent)，唯一对外入口
  machine.py       转移表、停止条件、主循环
  state.py         LoopState / TaskNode / Decision / ToolOutcome
  planner.py       decompose：用户消息 → TaskGraph
  scheduler.py     选可运行任务、写路径租约
  context.py       预算、图钉、观察环、压缩
  permissions.py   分级与执行档
  journal.py       写前快照与 restore
  verify.py        验收命令、诊断、差异范围
  repair.py        失败签名、回滚或增量、停滞检测
  delegate.py      子循环，深度 1
tools/shell.py     WorkspaceCommandRunner，注册 run_command
tools/mcp/
  config.py        读 mcp.servers
  client.py        连接、tools/list、tools/call、关闭
  bridge.py        MCP 工具适配成 tools.base.Tool
```

衔接点（实现时只改这些调用处）：

| 现有符号 | 改动 |
| --- | --- |
| `agents/registry.py` 的 `_CORE` | 增加 `"state_loop": StateLoopAgent` |
| `agents/agent.py` 的 `build_runtime` | 装配 journal、shell、mcp client，放进扩展后的依赖。`Runtime` 增加可选字段，旧框架忽略 |
| `agents/base.py` 的 `AgentDependencies` | 增加可选字段，默认 `None`，避免破坏 LangGraph / native_react 构造 |
| `skills/code_interpreter.py` | 工具元组追加 `run_command`。不把 MCP 工具写死在技能里，由 bridge 按配置动态并入已选注册表 |
| `agents/events.py` | 增加事件常量。旧前端不识别时不影响现有分支 |
| `prompts/task.md` | 状态机启用后，该文件不再承担流程控制。模型在 `decide` 只看到当前任务卡和允许的 `Decision.kind`。不要删文件，LangGraph 路径仍在用 |

`routes_chat.py` 继续只消费 `astream_run`。它不感知状态名。

## 5. 主循环状态

状态是机器的，不是模型自报的步骤名。

| 状态 | 谁执行 | 职责 |
| --- | --- | --- |
| `intake` | 机器 | 校验本轮用户消息、装入历史、初始化预算与执行档 |
| `decompose` | 模型一次，机器校验 | 需求拆解 + 项目规划，产出 `TaskGraph` |
| `schedule` | 机器 | 选下一个可运行任务，或决定委派、结束、阻塞 |
| `perceive` | 机器驱动只读工具 | 仓库感知。任务第一次执行前强制读码，模型不能跳过 |
| `decide` | 模型一次，机器校验 | 针对当前任务给出下一步 `Decision` |
| `authorize` | 机器，必要时等人 | 按权限级放行、拒绝或暂停 |
| `act` | 机器 | 执行已授权的工具批次，写前记账 |
| `verify` | 机器 | 跑验收命令与诊断，对照任务范围 |
| `repair` | 机器分类，模型只在增量修复时发言 | 回滚或增量，更新失败签名 |
| `delegate` | 机器 | 并行子循环，合并 `TaskResult` |
| `compress` | 机器，必要时一次摘要模型调用 | 上下文超过阈值时压缩观察，不改变任务图 |
| `halt` | 机器 | 终态。生成给用户的收尾文本 |

`plan` 不是独立状态。计划就是 `decompose` 产出的 `TaskGraph`。执行档为 `plan` 时，合法任务图先进入 `authorize` 做计划确认，允许后才 `schedule`。确认前不会 `perceive`、不会写文件、不会起进程。

### 5.1 转移表

`Ev` 是机器事件，不是模型自由文本。守卫失败时不跳到「看起来合理」的邻居状态，而进入表中的失败行。

| 当前 | 事件 | 守卫 | 下一状态 |
| --- | --- | --- | --- |
| `intake` | `TurnAccepted` | 用户消息非空，且历史可装入 | `decompose` |
| `intake` | `TurnRejected` | 消息空，或会话历史损坏 | `halt(blocked)` |
| `decompose` | `PlanAccepted` | 任务图通过第 6.2 节校验，且执行档不是 `plan` | `schedule` |
| `decompose` | `PlanNeedsConfirm` | 任务图合法，且执行档是 `plan` | `authorize`（请求种类为 `plan`，不是工具调用） |
| `decompose` | `PlanInvalid` | 协议不合法且 `protocol_retries < 1` | `decompose` |
| `decompose` | `PlanInvalid` | 已用完 1 次重试 | `halt(blocked)` |
| `schedule` | `NeedPerceive` | 存在 `status=ready` 且 `perceived=false` 的任务 | `perceive` |
| `schedule` | `NeedDecide` | 当前任务已感知、未完成、本步不委派 | `decide` |
| `schedule` | `CanDelegate` | 至少 2 个 `ready` 且 `perceived=false` 的任务，写租约不相交，深度为 0 | `delegate` |
| `schedule` | `AllDone` | 全部任务 `done` 或 `skipped` | `halt(completed)` |
| `schedule` | `GraphBlocked` | 没有 ready 任务，且存在 `blocked` 或未满足依赖 | `halt(blocked)` |
| `perceive` | `PerceiveOk` | 只读批次全部 `ok` 或 `empty` | `decide` |
| `perceive` | `PerceiveFailed` | 有 `denied` / `error`，且该任务 `attempts < max_attempts` | `repair` |
| `decide` | `DecisionAccepted` | kind 属于允许集合，参数通过 schema | `authorize` |
| `decide` | `DecisionIsVerify` | kind=`mark_done`，或本任务本轮已经写过盘 / 跑过命令 | `verify` |
| `decide` | `DecisionReplan` | kind=`replan` 且 `replan_count < max_replan`（默认 2） | `decompose` |
| `decide` | `DecisionAsk` | kind=`ask_user` | `halt(blocked)` |
| `decide` | `DecisionInvalid` | 协议不合法且本状态重试未用完（1 次） | `decide` |
| `decide` | `DecisionInvalid` | 重试用完 | `halt(blocked)` |
| `authorize` | `Granted` | 批次中每个调用 `effect ≤ 档位上限`，且路径在工作区内 | `act` |
| `authorize` | `NeedsConfirm` | 请求种类为 `plan`，或调用含 L4，或 `confirm_writes` 下含 L1/L3 | 停在 `authorize`，发出 `permission_request` |
| `authorize` | `ConfirmGranted` | 同一 `request_id` 被允许，且请求种类是 `plan` | `schedule` |
| `authorize` | `ConfirmGranted` | 同一 `request_id` 被允许，且请求是工具批次 | `act` |
| `authorize` | `ConfirmRejected` | 用户拒绝 | `repair` |
| `authorize` | `Denied` | L5，或命令不在允许列表 | `repair` |
| `act` | `ActOk` | 批次无 `error`；若含写或命令 | `verify` |
| `act` | `ActOkReadOnly` | 批次全部是 L0，且本任务连续只读批次数 `< 3` | `decide` |
| `act` | `ReadStreakCap` | 本任务连续 3 批纯 L0 仍不写、不验收 | `repair`（签名 `read_streak`；再出现一次则 `Stalled`） |
| `act` | `ActFailed` | 任一调用 `error` | `repair` |
| `verify` | `VerifyPassed` | 验收命令退出码 0，诊断无新增 error，改动文件 ⊆ 任务 `scope` | `schedule`（当前任务标 `done`） |
| `verify` | `VerifyFailed` | 失败且 `attempts < max_attempts`（默认 3） | `repair` |
| `verify` | `VerifyFailed` | 失败且尝试耗尽，`replan_count` 仍有余量 | `decompose` |
| `verify` | `VerifyFailed` | 尝试与重规划都耗尽 | `halt(blocked)` |
| `repair` | `RepairEdit` | 分类为可增量修复，且失败签名未重复 | `decide` |
| `repair` | `RepairRollback` | 分类为补丁冲突或连续同类错误，且存在检查点 | `perceive` |
| `repair` | `RepairReplan` | 分类为范围错误或停滞前最后一次升级 | `decompose` |
| `repair` | `Stalled` | 同一失败签名出现 2 次 | `halt(stalled)` |
| `delegate` | `ChildrenJoined` | 每个子循环返回 `TaskResult`，父任务图已合并 | `schedule` |
| `delegate` | `ChildCrashed` | 子循环未捕获异常 | 该任务标 `blocked`，其余结果照常合并，然后 `schedule` |
| 任意非 `halt` | `BudgetSoft` | 估算上下文 ≥ 预算 70% | `compress` |
| `compress` | `Compressed` | 压缩后 < 92%，记住 `resume_state` | 回到进入压缩前的状态 |
| `compress` | `BudgetHard` | 压缩后仍 ≥ 92% | `halt(budget)` |
| 任意非 `halt` | `Cancel` | 客户端断开或显式取消 | `halt(cancelled)` |
| 任意非 `halt` | `StepCap` | `step_count ≥ max_steps` | `halt(budget)` |

`max_steps` 默认取 `max_iterations * 4`。现有 `max_iterations`（`agent.yaml` 为 12）表示模型回合上限的旧语义；状态机一步不等于一次模型调用。`step_count` 每次离开一个状态加 1。模型调用另外计数 `model_calls`，上限 `max_iterations`，超出走 `StepCap` 同一终态。

压缩是插桩，不是业务步。进入 `compress` 时记下 `resume_state`，完成后回到该状态，且不增加任务的 `attempts`。

### 5.2 与目标链路的对应

| 用户链路 | 状态 | 不可跳过的机器条件 |
| --- | --- | --- |
| 需求拆解 / 项目规划 | `decompose` | 没有合法 `TaskGraph` 不能 `schedule` |
| 读代码文件 | `perceive` | `perceived=false` 不能 `decide` 出写操作 |
| 执行终端命令 | `authorize` + `act` | 只有 L3 且 argv 过允许列表才启动进程 |
| 运行检验自测 | `verify` | 本任务发生过写入或命令后，不能直接 `done` |
| 迭代 | `repair` → `decide` / `perceive` / `decompose` | 失败签名重复两次则停止，不无限循环 |

报告文本发生在 `halt(completed)`：机器把每个任务的验收摘要交给一次最终模型调用，只允许散文，不允许再发工具调用。这次调用失败时，机器用任务图自己拼一段摘要，不回退到工具循环。

## 6. 数据结构

语言为 Python。字段都要可 `dataclasses.asdict`，便于以后落盘，但第一期不落盘。

```python
class Phase(str, Enum):
    intake = "intake"
    decompose = "decompose"
    schedule = "schedule"
    perceive = "perceive"
    decide = "decide"
    authorize = "authorize"
    act = "act"
    verify = "verify"
    repair = "repair"
    delegate = "delegate"
    compress = "compress"
    halt = "halt"

class HaltReason(str, Enum):
    completed = "completed"
    blocked = "blocked"
    stalled = "stalled"
    budget = "budget"
    cancelled = "cancelled"

class ExecMode(str, Enum):
    auto_workspace = "auto_workspace"   # 默认，A8
    confirm_writes = "confirm_writes"   # L1/L3/L4 都暂停
    plan = "plan"                       # 任务图先暂停，确认后按 auto_workspace

class Effect(IntEnum):
    L0_READ = 0
    L1_WRITE = 1
    L2_SANDBOX = 2
    L3_COMMAND = 3
    L4_MCP_MUTATE = 4
    L5_ESCAPE = 5

class TaskStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    running = "running"
    done = "done"
    blocked = "blocked"
    skipped = "skipped"

@dataclass
class Acceptance:
    commands: list[list[str]]          # argv，不是 shell 字符串
    diagnostics: tuple[str, ...] = ("ruff", "pyright")
    must_read: tuple[str, ...] = ()    # 相对路径，perceive 至少读到这些

@dataclass
class TaskNode:
    id: str                            # "t1" 这种短 id，由机器生成，不信模型
    title: str
    detail: str
    depends_on: tuple[str, ...]
    scope: tuple[str, ...]             # 允许写入的相对路径或目录前缀
    acceptance: Acceptance
    status: TaskStatus = TaskStatus.pending
    perceived: bool = False
    attempts: int = 0
    max_attempts: int = 3
    checkpoint_id: str | None = None
    last_failure: "Failure | None" = None

@dataclass
class Failure:
    kind: str                          # 见第 7 节
    signature: str                     # 稳定哈希材料：kind + tool + 归一化消息前 200 字
    tool: str | None
    message: str
    retryable: bool

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    effect: Effect                     # 由 permissions 计算，不由模型填写

@dataclass
class ToolOutcome:
    call_id: str
    name: str
    ok: bool
    failure: Failure | None
    text: str                          # 给模型看的正文，已经按工具 output_limit 截断
    artifacts: dict                    # 机器字段：exit_code、changed_paths、diagnostic_count

@dataclass
class Decision:
    kind: str                          # tool_batch | mark_done | replan | ask_user
    calls: list[ToolCall]
    note: str                          # 给用户看的一句思考，进 thought 事件
    replan_reason: str = ""
    question: str = ""

@dataclass
class LoopState:
    phase: Phase
    halt_reason: HaltReason | None
    mode: ExecMode
    user_text: str
    tasks: dict[str, TaskNode]
    order: list[str]                   # 拓扑序，稳定
    current_task_id: str | None
    observations: list[ToolOutcome]    # 环，压缩时折叠旧项
    journal_seq: int
    step_count: int
    model_calls: int
    replan_count: int
    protocol_retries: int
    failure_counts: dict[str, int]     # signature → 次数
    pending_calls: list[ToolCall]
    resume_state: Phase | None
    permission_request_id: str | None
    char_budget: int
    chars_used: int
```text

`Decision.calls` 里的 `effect` 在解析后由 `permissions.classify(tool_name, arguments)` 覆盖。模型若自己写 `effect`，丢弃。

### 6.1 模型协议

`decompose` 与 `decide` 不走自由 ReAct。请求仍用现有 `LLMClient.chat`，但 `tool_choice` 固定为名为 `submit_decision` 的内部函数，schema 即 `Decision` 去掉 `effect`。模型如果只返回散文：

1. 记 `ModelProtocolError`；
2. 把「只调用 submit_decision」作为下一条 user 消息重试一次；
3. 仍失败则按转移表进入 `halt(blocked)`。

不从散文里用正则抠 JSON。`native_react.py` 的文本动作兜底只属于旧框架。`state_loop` 不调用 `_extract_text_action`。

`decompose` 的 arguments 形状：

```json
{
  "summary": "一句话目标",
  "tasks": [
    {
      "title": "让健康检查返回版本号",
      "detail": "改 app 的健康响应，并补一个断言",
      "depends_on": [],
      "scope": ["app/main.py", "tests/test_health.py"],
      "commands": [["python", "-m", "pytest", "tests/test_health.py", "-q"]],
      "must_read": ["app/main.py"]
    }
  ]
}
```text

机器把 `tasks[]` 转成 `TaskNode`：id 按序 `t1..tn`，忽略模型给的 id。

### 6.2 任务图校验

`planner.accept(raw) -> TaskGraph | Failure`，全部为确定性规则：

1. `tasks` 长度 1–8。超过 8 不截断执行，直接 `PlanInvalid`，把「请合并到 8 个以内」写进重试提示。
2. 每个 task 的 `title`、`scope`、`commands` 非空。纯问答由调用方在 `intake` 判为 `trivial`（见 6.3），不会进到这里。
3. `scope` 每项经 `WorkspaceSecurity.resolve`。越界则整张图无效。
4. `depends_on` 只能引用本次列表中的下标或标题，机器改写成 id。有环则无效。
5. `commands` 每条必须是非空 argv 数组，且 `commands[0]` 属于 L3 允许列表。不允许 `rm`、`del`、`format`、`shutdown`、`git`（git 不在第一期允许列表，避免回滚语义分叉）。
6. 无依赖的任务标 `ready`，其余 `pending`。

校验失败不执行任何工具。

### 6.3 小任务短路

`intake` 若判定为闲聊或单步只读问答（启发式：无「改、修、实现、测试、运行」等动词，且历史里没有未完成任务图），不建任务图，直接一次无工具的最终回复并 `halt(completed)`。启发式写在 `planner.is_trivial`，误判的代价是多走一轮 `decompose`，因此宁可不短路。默认词表放在 `state_loop/planner.py` 常量里，不进提示词。

已有未完成任务图的会话（第二期若持久化）不允许短路。第一期任务图只活在单次 `astream_run` 内，所以短路只看本条消息。

## 7. 失败分类与策略

`act` 之后禁止再只看字符串前缀。`ToolRegistry.execute` 仍返回字符串以兼容旧框架；`state_loop` 用适配器 `run_tool` 包一层，优先读 handler 抛出的异常类型，其次解析已知前缀。

| kind | 判定 | retryable | repair 动作 |
| --- | --- | --- | --- |
| `ModelProtocolError` | 没有合法 `submit_decision` | 是，仅 1 次 | 同状态重试，不进 `repair` |
| `ArgError` | 缺参、JSON 损坏、schema 不符 | 是 | `RepairEdit`，把错误原文放进观察 |
| `NotFound` | 文件不存在 | 是 | `RepairEdit`。若发生在 `perceive` 的 `must_read`，改 `RepairReplan` |
| `PathDenied` | `PathTraversalError` | 否 | `Denied`，任务 `blocked` |
| `PermissionDenied` | 用户拒绝或执行档不允许 | 否 | 任务 `blocked`，不自动改道重试同一调用 |
| `PatchConflict` | `edit_file` 的 old_string 0 次或多次 | 是 | `RepairRollback` 到本任务检查点，再 `perceive` |
| `CommandFailed` | 进程退出码非 0 | 是 | `RepairEdit`，观察里只留尾部 80 行加退出码 |
| `Timeout` | 命令或沙箱超时 | 是，同一命令最多 1 次 | 第二次同一 signature 即 `Stalled` |
| `SandboxViolation` | RestrictedPython 编译拒绝或运行期拦截 | 是 | 提示改走 `run_command`，若模型再次调用 `run_python_code` 做文件 IO，算同一 signature |
| `TestFailed` | 验收命令失败 | 是 | 与 `CommandFailed` 相同，但 `attempts += 1` 发生在 `verify` 而不是 `act` |
| `DiagnosticError` | ruff/pyright 在触碰文件上报新增 error | 是 | `RepairEdit` |
| `ScopeViolation` | 实际写入路径不在 `task.scope` | 否 | `RepairRollback`，然后 `RepairReplan` |
| `McpError` | 传输断开、tools/call 报错 | 是，仅 1 次 | 第二次 `blocked` |
| `LoopStall` | `failure_counts[signature] >= 2` | 否 | `halt(stalled)` |
| `BudgetExhausted` | 压缩后仍超硬阈值，或步数耗尽 | 否 | `halt(budget)` |
| `Cancelled` | 生成器被关闭 | 否 | `halt(cancelled)`。已写文件不自动回滚 |

`signature` 算法：`sha1(f"{kind}|{tool}|{normalized}")` 的前 12 位十六进制。`normalized` 去掉行号、临时路径、耗时数字，避免「同一错误换行号」被当成新错误。

`attempts` 只在 `verify` 失败或 `RepairRollback` 时加 1。只读工具失败不加，否则搜索一次就消耗修复预算。

用户取消时保留磁盘改动，并在收尾 `thought` 里列出 `journal` 中本轮检查点 id。不悄悄回滚，避免和「用户已经看到一半文件」冲突。自动回滚只发生在 `ScopeViolation` 与 `PatchConflict`。

## 8. 权限分级

分级看副作用，不看工具名字的字符串是否像只读。`permissions.classify` 是唯一判定点。

| 级 | 含义 | 现有或新增工具 | `auto_workspace` | `confirm_writes` | `plan` |
| --- | --- | --- | --- | --- | --- |
| L0 | 不改磁盘、不启进程 | `list_dir` `read_file` `file_search` `rag_search` `calculator`；MCP 标注 `readOnlyHint=true` 的工具 | 自动 | 自动 | 规划确认前自动，确认前禁止 L1+ |
| L1 | 改工作区文件 | `write_file` `edit_file` | 自动 | 暂停 | 确认计划后自动 |
| L2 | 无文件副作用的计算沙箱 | `run_python_code` | 自动 | 自动 | 确认计划前禁止 |
| L3 | 工作区子进程 | `run_command` | 自动，且 argv0 在允许列表 | 暂停 | 确认计划后自动 |
| L4 | 可能改外部系统 | 未标注只读的 MCP 工具 | 暂停 | 暂停 | 暂停 |
| L5 | 工作区外、换盘符、解释器 `-c` 逃逸 | 任何解析后越界的路径或命令 | 拒绝 | 拒绝 | 拒绝 |

L3 默认允许的 argv0（小写比较，Windows 去掉 `.exe`）：`python`、`pytest`、`ruff`、`pyright`、`pip`。`pip` 只允许子命令 `show` 与 `list`。其他一律 `PermissionDenied`，不通过「用户话里说过可以」放行。

`run_command` 参数：

```python
def run_command(argv: list[str], cwd: str = ".", timeout_seconds: int = 60) -> ToolOutcome:
    ...
```text

实现约束：

- `shell=False`。不接受单字符串 `command`。
- `cwd` 经 `WorkspaceSecurity.resolve`，必须是目录。
- 环境变量使用当前进程环境的副本，删除 `PYTHONPATH` 以外的自定义注入入口；不提供 `env` 参数给模型。
- 超时：`proc.kill()` 后等待 2 秒，与 `RestrictedPythonExecutor` 的强杀语义对齐。默认 60 秒，上限 120 秒，由配置夹紧。
- stdout/stderr 合并截断到 `executor.max_result_chars`（现有配置，默认 20000），但 `ToolOutcome.artifacts["tail"]` 另保留最后 80 行给修复态，避免头尾对半截断把错误栈切掉。这一条是相对 `truncate_output` 的特化，只用于命令结果。

执行档来源：`agents/agent.yaml` 新增可选键 `exec_mode`，缺省 `auto_workspace`。单次请求若以后扩展 body 字段，覆盖 yaml；第一期 HTTP body 不改，避免动前端。

暂停协议：`authorize` 发出事件后，循环在内存中等待一个 `asyncio.Future`。第一期没有审批 UI，因此 `confirm_writes` 与 L4 在 0 秒时视为拒绝并 `blocked`，同时 `thought` 说明「当前界面不能确认高权限操作」。这是故意的：没有人点允许就不执行。`plan` 档同理，在有审批 API 之前等于「只出计划然后停」。不把「等不到人」实现成自动放行。

## 9. 仓库感知

`perceive` 不让模型自己决定第一批读什么。`scheduler` 为当前任务生成固定只读批次：

1. 对 `acceptance.must_read` 每个路径调用 `read_file`。路径不存在 → `NotFound`。
2. 若任务 `detail` 里没有具体符号，再调用一次已有 `file_search` 或 `rag_search`（RAG 不可用时只用 `file_search`）。查询串用任务标题，top 结果最多 5 个路径。
3. 搜索命中的文件若落在 `scope` 内且还没读，最多再读 3 个，每个文件默认前 200 行。模型随后在 `decide` 里可以再要行区间。

读完把 `task.perceived = True`。之后 `decide` 仍可发 L0 调用，那是补充阅读，走 `ActOkReadOnly → decide`，不再强制回 `perceive`。

写入前的附加守卫，放在 `authorize` 而不是提示词里：`edit_file` / `write_file` 的目标若不在本任务 `scope`，直接 `ScopeViolation`，工具不执行。`scope` 是目录前缀时，用 `Path.is_relative_to` 判断。

## 10. 自测验证

`verify` 的输入是当前 `TaskNode`、本任务检查点之后的 `journal` 差异、`Acceptance`。输出是 `VerifyPassed` 或带 `Failure` 的 `VerifyFailed`。此状态不调用通用决策模型。

步骤：

1. 收集本任务检查点以来 `journal` 中的写入路径。任一路径不在 `scope` → `ScopeViolation`，不再跑命令。
2. 顺序执行 `acceptance.commands`。工作目录是 workspace root。任一非 0 退出 → `TestFailed`，后续命令不跑。
3. 对触碰文件跑诊断：若 `ruff` 在 PATH，`argv = ["ruff", "check", *touched]`；若 `pyright` 在 PATH，`argv = ["pyright", *touched]`。只把新增的 error 当失败。无法取得「新增」基线时（第一期没有语言服务器快照），把退出码非 0 视为 `DiagnosticError`。工具不存在则 `artifacts["diagnostics"] = "skipped"`，不失败。
4. 三者都过，任务 `status=done`，依赖它的任务若其全部前驱已 `done` 则改为 `ready`。

验收命令来自任务图，不来自模型在 `verify` 里的临场发挥。模型不能在 `decide` 里用一次成功的 `run_command` 把任务标完成来绕过 `verify`：只要本任务发生过 L1 或 L3，`mark_done` 会被机器改写成 `DecisionIsVerify`。

## 11. 迭代、快照与回滚

`FileJournal` 只记录本轮 `astream_run`：

```python
class FileJournal:
    def checkpoint(self, task_id: str, paths: list[Path]) -> str: ...
    def remember_write(self, call_id: str, path: Path, before: bytes | None): ...
    def restore(self, checkpoint_id: str) -> list[str]: ...
    def changed_paths(self, checkpoint_id: str) -> list[str]: ...
```

- 进入任务的第一次 L1 之前，对 `scope` 内已存在文件做 `checkpoint`。目录 scope 不递归整棵树，只在具体写入路径上 `remember_write`。
- `before is None` 表示文件原先不存在，`restore` 时删除该文件。
- `restore` 用当初的字节写回，再经 `WorkspaceSecurity.resolve` 复查，防止检查点被换路径。
- 日志存在 `Runtime` 内存。进程退出即丢。这是 A4 的边界：崩溃恢复不在本设计内。
- 回滚后清空该任务在 `observations` 里的写入结果正文，改为一句「已回滚到 checkpoint cx，原因 …」，避免模型对着旧文件内容打补丁。

`repair` 不自己改文件。它只决定下一状态，并把 `Failure` 放进下一次 `decide` 的任务卡。增量修复的补丁仍由模型在 `decide` 里提出，再走 `authorize → act → verify`。

## 12. 上下文预算与压缩

预算默认 24000 字符（约 A7 的 12000 token），配置键 `agent.context_char_budget`。组成：

| 区块 | 策略 |
| --- | --- |
| 系统提示词 | 图钉。用现有 `compose_system_prompt`，但 `state_loop` 删掉「任务流程」那一段，改为一句「流程由运行时执行，你只提交 Decision」 |
| 任务图 | 图钉。每任务一行：id、状态、scope、验收 argv、attempts、最后一次失败 kind |
| 当前任务正文 | 图钉。`detail` 全文，上限 2000 字 |
| 本任务最近观察 | 环，默认保留 6 条。更旧的收成一行摘要：工具名、ok、kind、首行 |
| 会话历史 | 只保留用户原话与上一轮助手最终答复，不回放旧框架的整段 tool 消息。`get_llm_messages` 的全量历史不直接塞进 `decide` |

估算在每次进入 `decide` / `decompose` 之前做。≥ 70% 触发 `compress`：

1. 先做确定性折叠（不调用模型）：观察环超出 6 条的部分换成摘要行；单条命令输出换成 `artifacts["tail"]`。
2. 若仍 ≥ 70%，再调用一次模型，只允许输出 800 字以内的「已做完的事实」列表。输入是被折叠的观察，不是整段历史。摘要替换那些观察。失败则保留折叠结果，不重试摘要。
3. 折叠后仍 ≥ 92% → `halt(budget)`。

压缩不改 `TaskGraph`，不改 `FileJournal`。

工具结果校验发生在两处：`ToolOutcome.ok` 由适配器根据异常与退出码设置；进入模型上下文之前，`context.render_observation` 丢弃超过 `output_limit` 的中段，但命令类保留尾部。禁止把未校验的原始字符串直接标成成功。

## 13. 子代理

子代理是一次嵌套的 `run_loop`，不是提示词里的角色扮演，也不是现有 `frameworks.autogen.roles`。

隔离规则：

| 项 | 父循环 | 子循环 |
| --- | --- | --- |
| 消息 | 父自己的观察环 | 新建空观察环，只含任务卡。子循环从 `perceive` 起，自己读码，不继承父观察 |
| 工具 | 全部已授权工具 | 同一 `ToolRegistry`，但 `authorize` 使用子任务的 `scope` |
| 写租约 | `scheduler.lease(paths)` | 租约路径相交则不允许并行，改串行 `NeedDecide` |
| 深度 | 0 | 1。子循环的 `CanDelegate` 守卫恒假 |
| 预算 | 父预算 | 子预算 = 父剩余的 40%，硬顶 8000 字 |
| 返回 | — | 只返回 `TaskResult`：status、changed_paths、verify 摘要、failure。不返回子观察全文 |
| 会话库 | 父回合结束由 `routes_chat` 落盘 | 子循环不调用 `HistoryStore` |
| 并发 | — | `asyncio.gather`，上限 3。合并时按 task id 排序写入父任务图，禁止并发 append 共享 list |

子循环命中 `halt(blocked/stalled/budget)` 时，父任务标 `blocked`，父循环继续 `schedule` 其他已完成兄弟，不因此取消整个用户回合。全部兄弟都 blocked 才由父 `GraphBlocked` 结束。

子代理不允许持有独立 MCP 连接。它们共用父进程的 client，调用仍经父注册表，避免 stdio 子进程被重复拉起。

## 14. MCP 生命周期

MCP 是工具源，不是第二套循环。

配置（建议加在 `config.yaml`，缺省空列表，空则跳过）：

```yaml
mcp:
  servers:
    - name: docs
      transport: stdio          # stdio | sse | streamable_http
      command: ["uvx", "some-server"]
      read_only: false          # true 则该 server 全部工具视为 L0
```text

生命周期，绑在 `build_runtime` / 进程退出：

| 阶段 | 动作 | 失败 |
| --- | --- | --- |
| `load` | 解析配置。名称须匹配 `^[a-z][a-z0-9_]{0,31}$` | 非法配置：打印警告并跳过该条，不阻止对话。与现有 RAG 初始化失败策略一致 |
| `connect` | stdio 拉起子进程；SSE / Streamable HTTP 建会话。握手 `initialize` | 该 server 标记 `down`，工具不注册 |
| `discover` | `tools/list`。支持分页则拉全 | 失败则 `down` |
| `bridge` | 每个工具注册为 `mcp_<server>_<tool>`，handler 调 `tools/call`。描述用 server 提供的 description。schema 用 inputSchema | 同名冲突则跳过并警告 |
| `refresh` | 收到 `notifications/tools/list_changed` 时重新 list，增删注册表项 | 刷新失败保持旧表，记一条日志 |
| `call` | 仅当状态机处于 `act` 且该调用已 `Granted`。参数原样传递。结果取 text content；若有 structuredContent，放进 `artifacts` | 超时 30 秒，`McpError` |
| `close` | `Runtime` 丢弃或进程退出时 `shutdown`，stdion 子进程 terminate | 忽略关闭错误 |

工具数量 > 32 时，不把全部 schema 放进每次模型请求。`decide` 的可见工具 = 内置工具 + MCP 工具目录（name + 一行 description）。模型若调用目录中的工具，机器在执行前把该工具的完整 schema 用于参数校验；校验失败是 `ArgError`，不会发到服务器。这是预算措施，不是第二协议。

MCP 工具的 effect：server 级 `read_only: true`，或工具元数据含公开的 `readOnlyHint: true`（MCP annotations，若该 server 提供）→ L0。否则 L4。不根据描述文本猜测只读。

本进程不实现 MCP Server。资源（resources）与提示词（prompts）能力第一期不桥接，避免和 `prompts/*.md`、RAG 两套上下文打架。

## 15. 主循环接口与伪代码

```python
class StateLoopAgent(BaseAgent):
    framework_name = "state_loop"

    def __init__(self, deps: AgentDependencies):
        super().__init__(deps)

    async def astream_run(
        self, history: list[dict],
    ) -> AsyncIterator[dict]:
        state = build_initial_state(history, self.deps)
        while state.phase is not Phase.halt:
            if cancelled(self):
                state = transition(state, Cancel())
                continue
            # BudgetSoft 只在这里插桩，不由 step() 再发一次
            if over_soft_budget(state) and state.phase is not Phase.compress:
                state.resume_state = state.phase
                state.phase = Phase.compress
            event = await step(state, self.deps, emit=yield_event)
            state = transition(state, event)
            state.step_count += 1
        async for piece in final_answer(state, self.deps):
            yield ev.make_event(ev.TOKEN, {"content": piece})
        yield ev.make_event(ev.DONE, {"finish_reason": state.halt_reason.value})

async def step(state: LoopState, deps, emit) -> Event:
    match state.phase:
        case Phase.intake:
            return accept_turn(state)
        case Phase.decompose:
            raw = await model_decision(state, deps, schema="plan")
            return planner.accept(raw, deps.workspace)
        case Phase.schedule:
            return scheduler.pick(state)
        case Phase.perceive:
            batch = scheduler.perceive_batch(state)
            outcomes = await run_batch(batch, deps, emit)
            return PerceiveOk() if all_ok(outcomes) else PerceiveFailed(outcomes)
        case Phase.decide:
            raw = await model_decision(state, deps, schema="act")
            return decisions.parse(raw, tool_catalog(deps))
        case Phase.authorize:
            return permissions.judge(state, deps.mode)
        case Phase.act:
            outcomes = await run_batch(state.pending_calls, deps, emit)
            return ActOk(outcomes) if all_ok(outcomes) else ActFailed(outcomes)
        case Phase.verify:
            return await verify.run(state, deps)
        case Phase.repair:
            return repair.choose(state)
        case Phase.delegate:
            return await delegate.run(state, deps, emit)
        case Phase.compress:
            return context.compress(state, deps)
```text

`run_batch` 对 L0 允许并发（上限 4），对 L1 及以上强制按调用顺序执行。每个调用：

1. `emit(TOOL_CALL)`，参数用现有事件形状，前端不用改；
2. L1 调用前 `journal.remember_write`；
3. `await asyncio.to_thread(registry.execute, ...)`，命令与 MCP 同理；
4. 适配成 `ToolOutcome`；
5. `emit(TOOL_RESULT)`，`is_error = not outcome.ok`；
6. 追加到 `state.observations`。

`yield_event` 在伪代码里写成回调。实现时 `step` 应改成异步生成器，或把事件放进 `Event.emitted`，由 `astream_run` 转发出去。不要在子线程里直接 yield。

内部函数 `model_decision` 使用 `LLMClient.chat`（已有、非流式）。`note` 字段单独 `emit(THOUGHT)`。最终答复才用现有的分片 token。不在 `decide` 里流式拼接工具参数，避免半截 JSON 进入状态。

## 16. 停止条件

同时生效，先命中先停：

| 条件 | 终态 | 用户可见 |
| --- | --- | --- |
| 全部任务 `done` 或 `skipped` | `completed` | 验收摘要 |
| 任务图无可运行节点 | `blocked` | 卡住的任务 id 与最后 `Failure.kind` |
| 同一 signature 两次 | `stalled` | 签名与最后一次工具输出尾部 |
| 协议重试失败 | `blocked` | 说明模型没有提交 Decision |
| `model_calls` 或 `step_count` 到顶 | `budget` | 已完成任务列表，未完成任务保持磁盘现状 |
| 压缩后仍超硬预算 | `budget` | 同上 |
| 客户端断开 | `cancelled` | `routes_chat` 现有 finally 仍会把已产生的 token 落盘 |
| 用户拒绝权限 | `blocked` | 哪一个调用被拒绝 |

`halt` 之后禁止再转移。不允许「终态之后再补一次工具调用」。

## 17. 和 Trae、Cursor、Zcode 的差异

公开材料：Trae 文档中的 SOLO / 内置 Agent / MCP（docs.trae.ai）、Cursor 文档中的子代理、运行模式与 ACP、Zcode 文档中的执行模式、目标模式与 MCP（zcode.z.ai）。下面「公开信息推断」都不是对方源码事实。

### 17.1 Trae

公开机制：SOLO 从需求理解走到代码、测试和预览；内置 Agent 先给出可执行计划，用户确认后再逐步做。MCP 由 Agent 当客户端，传输含 stdio、SSE、Streamable HTTP，发现用 `tools/list`，调用用 `tools/call`。官方博客把 Agent 描述成提示词工程加工具。另有独立开源 CLI（trae-agent），不等同于 IDE 内部循环。

差异：

1. Trae 的计划确认是产品交互；本设计把确认实现成 `authorize` 的一个暂停点，且在没有审批 API 时失败关闭，不自动开做。
2. 公开信息推断：Trae IDE 的逐步执行仍由模型驱动工具选择。本设计用转移表强制「未感知不能写、写过必须 verify」。
3. 本设计不采用「提示词 + 工具 = Agent」作为控制面。提示词只描述当前任务卡。开源 trae-agent 的 Docker / 轨迹记录不在范围内。

### 17.2 Cursor

公开机制：子代理（Explore、Bash、Browser 及自定义）隔离上下文，可只读、可后台，继承父工具与本地 MCP。运行模式里 Auto-review 的顺序是允许列表、能沙箱则沙箱、其余交给分类器。检查点是本地、按提示恢复，文档说明与 git 分开。ACP 的权限答复是 allow-once、allow-always、reject-once。Plan 模式偏只读。

差异：

1. 不引入分类器模型来决定命令能不能跑。L0–L5 与 argv 允许列表是确定性的，本地 7B 模型不承担安全裁决。
2. 子代理第一期只做任务图上的并行编码，深度锁死为 1，且必须写租约不相交。不做 Cursor 文档里的后台子代理、云端虚拟机和 Team MCP。
3. 回滚是工作区文件的字节检查点，不实现 Cursor 文档中的跨消息检查点产品。公开信息推断：Cursor 主循环的状态划分未公开，不能把它说成与本文同一张表。

### 17.3 Zcode

公开机制：自研 ZCode Agent，执行档包含变更前确认、自动编辑、计划模式、完全访问。文档写明目标模式负责长任务的目标、完成校验与状态恢复，但没有公开状态转移表。MCP 按用户级与工作区级配置接入。工作区、终端、浏览器、Review 在同一产品里。辅助对话是并行问答，不是子任务执行器；文档写明它不继承主任务目标。

差异：

1. 执行档只保留三档，且默认 `auto_workspace` 对齐本仓库「本机、工作区锁定」的威胁模型，不提供「完全访问」去关掉 L5。
2. 完成校验是 `verify` 状态里的 argv 与诊断退出码，不依赖模型宣布完成。目标模式的状态恢复在对方文档中存在，本设计第一期不持久化 `LoopState`，进程退出不能续跑。这一点弱于 Zcode 的公开产品能力。
3. 子代理返回结构化 `TaskResult`，与 Zcode 辅助对话（共享主历史、不继承目标）不是同一机制。本设计第一期不做浏览器面板。

## 18. 事件

在 `agents/events.py` 增加常量，不改已有常量的字符串值：

| type | data | 第一期前端 |
| --- | --- | --- |
| `plan` | `{tasks:[{id,title,status,commands}]}` | 忽略。同时发一条 `thought` 摘要 |
| `task` | `{id,status,attempts}` | 同上 |
| `verify` | `{id,ok,command,exit_code}` | 同上 |
| `rollback` | `{checkpoint_id,paths}` | 同上 |
| `delegate` | `{parent,children:[id]}` | 同上 |
| `permission_request` | `{request_id,calls}` | 第一期立即按拒绝处理，不新增按钮 |

`tool_call` / `tool_result` 字段保持 `routes_chat.py` 已经读取的 `id/name/arguments/output/is_error`。

## 19. 配置与测试

`agents/agent.yaml` 预留（实现切片时再改，本轮不改文件）：

```yaml
framework: state_loop
exec_mode: auto_workspace
max_iterations: 12          # 模型调用上限
max_replan: 2
context_char_budget: 24000
```text

`config.yaml` 预留 `mcp.servers: []`。命令超时复用 `executor.timeout_seconds` 作为沙箱超时；命令超时单独键 `executor.command_timeout_seconds`，默认 60。

必须能单测、且不启动模型的部分：

| 测试 | 断言 |
| --- | --- |
| 转移表 | 第 5.1 节每一行至少一个用例。非法组合不能落到别的状态 |
| `planner.accept` | 空 scope、环、越界路径、argv0 不在允许列表 → `PlanInvalid` |
| `permissions.classify` | `edit_file` 为 L1；`run_command` 的 `python -c` 为 L5；工作区外 cwd 为 L5 |
| `FileJournal.restore` | 覆盖文件能还原；新建文件能删除 |
| `repair` 停滞 | 同一 signature 两次 → `Stalled` |
| `verify` 范围 | 写入 scope 外路径 → 不执行验收命令 |

模型相关测试用假 `LLMClient` 返回固定 `submit_decision`。不把真实 Ollama 放进单元测试。

## 20. 实现顺序

本轮不写这些模块。下一轮最小切片按此顺序，做完即可用一条假模型测试跑通「改文件 → 命令失败 → 回滚 → 再验证」：

1. `agents/state_loop/state.py` 与 `machine.py`：转移表和停止条件，纯函数。
2. `agents/state_loop/journal.py`：检查点与 restore。
3. `tools/shell.py`：`WorkspaceCommandRunner`，注册 `run_command`，允许列表与 `shell=False`。
4. `agents/state_loop/verify.py` 与 `repair.py`：失败分类、验收、停滞。
5. `agents/state_loop/agent.py`：`StateLoopAgent.astream_run` 接到 `BaseAgent`，并在 `registry._CORE` 注册。`decompose` / `decide` 先用可注入的假决策器，确认循环闭合后再接 `LLMClient`。

MCP（`tools/mcp/`）和子代理（`delegate.py`）不进这一刀。它们依赖上面的 `authorize` 与 `ToolOutcome`，但可以后加而不改转移表的状态名。

## 21. 明确不做

- ~~不替换、不删除 LangGraph、native_react、autogen、llamaindex、crewai。~~ **已作废**：LangGraph 已按最终决定移除；`native_react`、`autogen`、`llamaindex`、`crewai` 保留不动。
- 不把 `prompts/task.md` 当成新循环的控制器。
- 不实现 git 回滚、云端代理、浏览器自动化、审批按钮、LoopState 崩溃恢复。
- 不把本进程做成 MCP Server。
- 不在第一期修改 `app/web/app.js` 的事件 switch。新事件必须能被忽略。
