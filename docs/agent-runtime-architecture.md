# 自研 Agent 运行时架构设计（state_loop）

> 状态：**架构设计定稿，待分切片实现**。日期 2026-09-21。
> 适用范围：本仓库 `D:\idea\1\222222`（本地编码智能体，FastAPI + SSE + 统一工具集）。
> 关联文档：[`agent-loop-architecture.md`](./agent-loop-architecture.md)（本文的模块级实现规格附录，
> 含逐行可测的转移矩阵、字段级 dataclass 定义）；
> [`reasoning-levels.md`](./reasoning-levels.md)（思考强度档位、厂商能力矩阵与可观测性闭环）。
>
> 本文回答三件事：**为什么这么设计**、**每个模块的设计逻辑**、**与 Zcode 这类大厂产品的差异在哪**。

---

## 0. 摘要

一句话：**一次用户消息 = 一张可持久化的状态对象 `LoopState`；控制流由代码的转移表推进，模型只在三个状态里产出类型化 `Decision`。**

四个不变量（整个框架的承重墙，后面所有模块都在服务它们）：

| 不变量 | 含义 | 反例（我们要避免的） |
| --- | --- | --- |
| **I1 控制流归机器** | 状态转移是代码，不是模型的自由输出 | 「模型自己说下一步该读文件了」 |
| **I2 副作用归授权** | 任何写盘 / 起进程 / 改外部状态，先过 `authorize` | 「模型调了工具就直接执行」 |
| **I3 完成归验证** | 任务完成由验收命令退出码 + 诊断决定，不由模型宣布 | 「模型说改好了就算完成」 |
| **I4 失败归分类** | 每个失败都有 `Failure.kind`，决定它走增量修复/回滚/重规划/停止 | 「错误是一段字符串，模型自己看着办」 |

框架标识 `state_loop`，落在 `agents/state_loop/`，与现有 `native_react`、`autogen`、`llamaindex`、`crewai` 平级，通过 `agents/registry.py` 注册。

**当前已落地的前置改动**：LangGraph 已从本仓库移除（见第 13 节），默认框架暂为 `native_react`，等本设计切片通过测试后再把默认切到 `state_loop`。

---

## 1. 为什么不用 LangGraph，也不用「加强版 ReAct 提示词」

这一节不是吐槽，是把「被否决的方案为什么不可靠」写成可复核的失效模式。后面每个模块的设计，都是这些失效模式的补丁。

### 1.1 三个候选方案

| 方案 | 做法 | 失效模式（决定性否决理由） |
| --- | --- | --- |
| **A. 加厚提示词** | 把六步链路写进 `prompts/task.md`，模型自己按顺序走 | ① **不可验收**：停止条件是「模型不再调用工具」，不是「测试通过」；② **不可分类**：失败只是 `[工具错误] xxx` 文本，无法区分「参数写错」「路径越界」「测试挂了」；③ **不可复现**：同一输入两次跑出不同路径；④ **上下文线性膨胀**：每轮把全部历史塞回去，本地 7B 模型窗口直接爆 |
| **B. 保留 LangGraph，在图上加 plan/verify 节点** | 用 `StateGraph` 加节点编排 | ① **节点是代码，边是模型输出**——加了节点仍然退化成「模型决定要不要走到 verify」，本质没夺回控制权；② 图状态是 `messages` 容器，不是领域状态：没有「任务」这个对象，无法回答「哪一步没做」；③ 引入 `langchain-core` 依赖链与版本漂移；④ 实测缺陷：现有实现里 `tools_node` 用 `asyncio.gather` 并发往同一个 `messages` 列表 append，**结果顺序不确定**，而工具结果的顺序恰恰是可观测性的一部分 |
| **C. 显式状态机（采纳）** | 代码持有 `LoopState`，转移表是纯函数，模型只填 `Decision` | 代价：要自己写调度、预算、权限、快照、验收（本设计的全部工作量）。收益：**转移可单测、失败可分类、停止条件可枚举、完成可机器判定** |

### 1.2 核心命题：控制面与语义面分离

| 归机器（确定性、可单测） | 归模型（语义、不可枚举） |
| --- | --- |
| 流程合法性（这个状态能不能去那个状态） | 把「一句自然语言需求」变成结构化任务列表 |
| 权限分级与放行 | 在给定任务卡上下文里，选下一步动作 |
| 上下文预算与压缩 | 写补丁内容（`old_string → new_string`） |
| 写前快照与回滚 | 失败后提出新的修复补丁 |
| 验收命令执行与退出码判定 | 判断需求里有没有歧义、要不要问用户 |
| 子任务合并、停止条件 | 最终给用户的散文总结 |

**「工程化」的真正含义**：把可靠性做成**不依赖模型自觉**的性质。模型可以犯懒、可以跳步、可以幻觉「我已经测试通过了」——只要 I1–I4 成立，这些都会被机器拦住。这是本设计和「提示词工程」的分界线。

### 1.3 与用户给定链路的映射

用户要求的链路「需求拆解 → 项目规划 → 读代码文件 → 执行终端命令 → 运行检验自测 → 迭代」，在状态机里**每一环都有机器守卫，跳过不了**：

| 链路环节 | 状态 | 不可跳过的机器条件 |
| --- | --- | --- |
| 需求拆解 | `decompose` | 没有通过 7 条校验的 `TaskGraph`，不能进入 `schedule` |
| 项目规划 | `decompose` | 「规划」的产物就是任务图本身（依赖、scope、验收命令），不是一个独立状态 |
| 读代码文件 | `perceive` | `perceived=false` 的任务，不允许 `decide` 出任何写操作 |
| 执行终端命令 | `authorize` + `act` | 只有 L3 且 argv 通过允许列表，才真正 `spawn` 进程 |
| 运行检验自测 | `verify` | 本任务发生过写入或命令后，`mark_done` 被机器改写为进 `verify` |
| 迭代 | `repair` → `decide`/`perceive`/`decompose` | 同一失败签名出现 2 次即 `stalled` 停机，不无限循环 |

---

## 2. 总体架构：五层 + 两条主干

```text
┌─────────────────────────────────────────────────────────────────────┐
│ L1 接入层  Access                                                    │
│   app/api/routes_chat.py (SSE)   │  CLI/自动化调用   │  agents/events.py │
│   职责：会话落盘、事件下发、断线兜底。不感知状态名。                      │
├─────────────────────────────────────────────────────────────────────┤
│ L2 编排层  Orchestration  ← 控制面，本框架的心脏                        │
│   machine.py   转移表 / 停止条件 / step() 纯函数                       │
│   state.py     LoopState / TaskNode / Decision / ToolOutcome          │
│   scheduler.py 选任务、写路径租约、生成感知批次                          │
│   职责：决定「下一步做什么」，且这个决定不来自模型                        │
├─────────────────────────────────────────────────────────────────────┤
│ L3 能力层  Capability（七大模块）                                      │
│   planner.py  repo.py  shell.py  verify.py  repair.py  delegate.py     │
│   mcp/        （需求拆解｜仓库感知｜沙箱终端｜自测验证｜迭代修复｜委派｜MCP）│
│   职责：每层只解决一个专业问题，输入输出都是结构化数据                     │
├─────────────────────────────────────────────────────────────────────┤
│ L4 契约层  Contract                                                   │
│   tools/base.py (Tool / ToolRegistry)      agents/base.py (BaseAgent)  │
│   permissions.py (L0–L5)  journal.py (写前快照)  context.py (预算)      │
│   职责：所有模块共用的抽象与门闩。新模块不得绕过这一层                     │
├─────────────────────────────────────────────────────────────────────┤
│ L5 扩展层  Extension                                                  │
│   技能白名单 skills/*.py（模型可见工具集）  │  MCP 动态工具  │  子代理     │
│   职责：扩展必须「不改控制流」。新工具 = 新契约实现，不是新循环             │
└─────────────────────────────────────────────────────────────────────┘
```

### 主干 1 · 控制流（同步、确定性）

```text
intake → decompose → schedule ⇄ (perceive → decide → authorize → act → verify) → halt
                        ↓            ↑                                 ↓
                    delegate      compress                          repair ─┘
```

### 主干 2 · 数据流（每步都在收敛）

```text
用户消息 → TaskGraph(可执行/可验收/可并行) → ToolCall[] → ToolOutcome[](观察环)
        → VerifyReport(实据) → 最终答复(散文)
```

**六条设计原则**（新模块评审时按这六条打勾）：

1. **单一入口单一出口**：对外只有 `astream_run(history)`，产出统一 SSE 事件。接入层不需要知道状态机存在。
2. **一切副作用经 `authorize`，一切完成经 `verify`**：没有任何旁路。
3. **状态是机器的**：模型不能自报「我完成了第 3 步」。它只能提交 `Decision`，进度由机器根据 `TaskNode.status` 计算。
4. **失败必须可分类**：拿不到分类的失败，等于不可修复。
5. **上下文是预算资源**，不是日志。观察环是**环**（有容量、会折叠），不是无限追加。
6. **扩展不改变控制流**：MCP 工具、子代理、技能都在既有状态内运行，不引入新状态。

---

## 3. 主循环：状态驱动

### 3.1 状态清单（谁执行，干什么）

| 状态 | 执行方 | 职责 | 产出 |
| --- | --- | --- | --- |
| `intake` | 机器 | 校验用户消息、装入历史、初始化预算与执行档、判定是否小任务短路 | `TurnAccepted` / `TurnRejected` |
| `decompose` | **模型 1 次** + 机器校验 | 需求拆解 + 项目规划 | `TaskGraph` |
| `schedule` | 机器 | 选下一个可运行任务 / 决定委派 / 结束 / 阻塞 | `NeedPerceive` / `NeedDecide` / `CanDelegate` / `AllDone` / `GraphBlocked` |
| `perceive` | 机器驱动只读工具 | 仓库感知（强制读码） | `PerceiveOk` / `PerceiveFailed` |
| `decide` | **模型 1 次** + 机器校验 | 针对当前任务给出下一步动作 | `Decision` |
| `authorize` | 机器（必要时等人） | 按权限级放行 / 拒绝 / 暂停 | `Granted` / `NeedsConfirm` / `Denied` / `ConfirmRejected` |
| `act` | 机器 | 执行已授权批次，写前记账 | `ActOk` / `ActOkReadOnly` / `ActFailed` / `ReadStreakCap` |
| `verify` | 机器 | 跑验收命令与诊断，对照任务范围 | `VerifyPassed` / `VerifyFailed` |
| `repair` | 机器分类（模型只在增量修复时发言） | 回滚或增量，更新失败签名 | `RepairEdit` / `RepairRollback` / `RepairReplan` / `Stalled` |
| `delegate` | 机器 | 并行子循环，合并 `TaskResult` | `ChildrenJoined` / `ChildCrashed` |
| `compress` | 机器（必要时 1 次摘要调用） | 上下文超阈值时折叠观察 | `Compressed` / `BudgetHard` |
| `halt` | 机器 | 终态，生成收尾文本 | 5 种 `HaltReason` |

**注意两点「没有」**：

- **没有 `plan` 状态**。计划就是 `decompose` 的产物。执行档为 `plan` 时，合法的任务图先进 `authorize` 做**计划确认**（请求种类是 `plan` 而不是工具调用，`ConfirmGranted` 后回 `schedule`）。不发明第二个状态去做同一件事。
- **没有 `think` 状态**。模型的「思考」是 `Decision.note` 字段，作为 `thought` 事件流出去给用户看，**不参与控制**。把思考和控制分开，是因为前者不可枚举、后者必须可枚举。

### 3.2 转移表（核心行）

完整矩阵（含每一行的守卫与失败分支）见附录文档第 5.1 节。这里给主干与代表性失败行：

| 当前 | 事件 | 守卫 | 下一状态 |
| --- | --- | --- | --- |
| `intake` | `TurnAccepted` | 消息非空且历史可装入 | `decompose` |
| `decompose` | `PlanAccepted` | 任务图过校验，执行档非 `plan` | `schedule` |
| `decompose` | `PlanInvalid` | 协议不合法且重试 < 1 | `decompose` |
| `decompose` | `PlanInvalid` | 重试用完 | `halt(blocked)` |
| `schedule` | `NeedPerceive` | 存在 `ready` 且 `perceived=false` | `perceive` |
| `schedule` | `CanDelegate` | ≥2 个 ready、写租约不相交、深度为 0 | `delegate` |
| `schedule` | `AllDone` | 全部任务 `done`/`skipped` | `halt(completed)` |
| `schedule` | `GraphBlocked` | 无 ready 且存在 `blocked`/依赖未满足 | `halt(blocked)` |
| `perceive` | `PerceiveFailed` | 有 denied/error 且 `attempts < max_attempts` | `repair` |
| `decide` | `DecisionAccepted` | kind 合法、参数过 schema | `authorize` |
| `decide` | `DecisionIsVerify` | `mark_done`，或本任务已写过盘/跑过命令 | `verify` |
| `decide` | `DecisionReplan` | kind=`replan` 且 `replan_count < max_replan` | `decompose` |
| `authorize` | `Granted` | 每个调用 `effect ≤ 档位上限` 且在 workspace 内 | `act` |
| `authorize` | `NeedsConfirm` | 含 L4，或 `confirm_writes` 下含 L1/L3 | 停在 `authorize`，发 `permission_request` |
| `authorize` | `ConfirmRejected` / `Denied` | 用户拒绝 / L5 / 命令不在允许列表 | `repair` |
| `act` | `ActOk` | 批次无 error，且含写或命令 | `verify` |
| `act` | `ActOkReadOnly` | 全 L0 且本任务连续只读批次 `< 3` | `decide` |
| `act` | `ReadStreakCap` | 连续 3 批纯 L0 仍不写、不验收 | `repair`（签名 `read_streak`） |
| `verify` | `VerifyPassed` | 退出码 0 + 诊断无新增 error + 改动 ⊆ `scope` | `schedule`（任务标 `done`） |
| `verify` | `VerifyFailed` | 失败且 `attempts < max_attempts` | `repair` |
| `verify` | `VerifyFailed` | 尝试耗尽但 `replan_count` 有余量 | `decompose` |
| `verify` | `VerifyFailed` | 两者都耗尽 | `halt(blocked)` |
| `repair` | `Stalled` | 同一失败签名出现 2 次 | `halt(stalled)` |
| 任意非 `halt` | `StepCap` | `step_count ≥ max_steps` | `halt(budget)` |
| 任意非 `halt` | `Cancel` | 客户端断开 | `halt(cancelled)` |

两条设计约定：

- **守卫失败不进「看起来合理的邻居状态」，只进表中写明的失败行**。这是防止状态机退化成「模糊路由」的关键纪律。
- **`compress` 是插桩不是业务步**：进入时记 `resume_state`，完成后回到该状态，且**不给任务加 `attempts`**。压缩不该消耗修复预算。

### 3.3 为什么 `step()` 是纯函数

```python
async def step(state: LoopState, deps, emit) -> Event   # 只读 state，返回事件
state = transition(state, event)                        # 纯函数，转移表
```text

`step` 不修改传入的 `state`，`transition` 是确定的查表。带来三个直接收益：

1. **转移表可以离线单测**：每行至少一个用例，不需要模型、不需要网络、不需要真实文件系统。
2. **状态可以序列化**：`LoopState` 全部字段可 `dataclasses.asdict()`，为第二期落盘/崩溃续跑留好接口（第一期不做，见第 15 节）。
3. **可观测性天然成立**：每次转移产出一个事件，事件既发给前端、也写日志。**看回放就知道它在哪一步、为什么停**。

### 3.4 停止条件（7 种终态，先命中先停）

| 条件 | 终态 | 用户可见 |
| --- | --- | --- |
| 全部任务 `done`/`skipped` | `completed` | 验收摘要 |
| 任务图无可运行节点 | `blocked` | 卡住的任务 id + 最后 `Failure.kind` |
| 同一 signature 两次 | `stalled` | 签名 + 最后工具输出尾部 |
| 协议重试失败 | `blocked` | 「模型没有提交可用的 Decision」 |
| `model_calls` / `step_count` 到顶 | `budget` | 已完成列表；未完成任务**保持磁盘现状** |
| 压缩后仍超硬预算 | `budget` | 同上 |
| 客户端断开 | `cancelled` | 列出本轮检查点 id；**不自动回滚** |

`halt` 之后禁止再转移——不允许「终态之后再补一次工具调用」。

### 3.5 与 ReAct 的对比

| 维度 | ReAct（提示词驱动） | state_loop（本设计） |
| --- | --- | --- |
| 停止条件 | 模型不再发起工具调用 | 验收通过 / 枚举的 7 种终态 |
| 步骤概念 | 无（只有消息轮次） | 任务图 + 状态，进度可查询 |
| 失败语义 | 字符串前缀 | `Failure.kind` + 归一化签名 |
| 可测性 | 需真实模型 | 转移表纯函数可单测 |
| 上下文 | 线性追加历史 | 预算 + 环 + 折叠 |
| 并发 | 结果合并顺序不确定 | L0 并发上限 4；L1+ 强制顺序 |
| 权限 | 无（或笼统确认） | L0–L5 + 四档执行模式 |
| 进度可见性 | 「正在思考…」 | 任务卡状态 + verify 报告 |

---

## 4. 模块一｜需求拆解与项目规划（`planner.py`）

### 4.1 设计目标

把「一句自然语言需求」变成**可执行、可验收、可并行**的 `TaskGraph`。这三个形容词对应三个字段族：

- **可执行** → `depends_on`（拓扑序，决定顺序与并行度）
- **可验收** → `acceptance.commands`（完成定义）
- **可并行** → `scope`（写路径边界，决定两个任务能不能同时跑）

### 4.2 关键设计：强制结构化输出

`decompose` 不自由生成文本，而是把 `tool_choice` **固定**为内部函数 `submit_decision`，schema 就是 `Decision` 去掉 `effect` 字段。模型如果只返回散文：

1. 记 `ModelProtocolError`；
2. 把「请只调用 submit_decision」作为下一条 user 消息**重试一次**；
3. 仍失败 → `halt(blocked)`。

**为什么不从散文里正则抠 JSON**：那是 ReAct 的兜底手法（现有 `native_react._extract_text_action`），它把「模型没按协议输出」这个重要信号静默成了「解析成功」。在状态机里，协议违反必须是**一等事件**——否则第 1.1 节的「不可分类」问题原封不动地回来了。

### 4.3 数据结构与字段语义

```python
@dataclass
class Acceptance:
    commands: list[list[str]]        # argv 数组，不是 shell 字符串
    diagnostics: tuple[str, ...] = ("ruff", "pyright")
    must_read: tuple[str, ...] = ()  # perceive 至少读到这些相对路径

@dataclass
class TaskNode:
    id: str                          # "t1".."tn"，机器生成，不信任模型
    title: str
    detail: str
    depends_on: tuple[str, ...]
    scope: tuple[str, ...]           # 允许写入的相对路径或目录前缀
    acceptance: Acceptance
    status: TaskStatus = TaskStatus.pending
    perceived: bool = False
    attempts: int = 0
    max_attempts: int = 3
    checkpoint_id: str | None = None
    last_failure: Failure | None = None
```

每个字段都承担一个**具体的控制职责**，没有装饰性字段：

| 字段 | 为什么需要它 |
| --- | --- |
| `scope` | 安全边界。写入路径不在 scope 内 → `ScopeViolation`，工具**不执行**。同时是并行判据：两个任务 scope 前缀不相交才允许并行 |
| `acceptance.commands` | 完成定义。**来自任务图，不来自模型在 verify 里的临场发挥**——这是 I3 的落地方式 |
| `acceptance.must_read` | 感知下限。保证模型不是在没看代码的情况下写补丁 |
| `depends_on` | 并行度的来源：无依赖 → `ready`，可被 `CanDelegate` 收集 |
| `perceived` | 强制感知的门闩。一个布尔值拦住「不看代码直接改」 |
| `attempts` / `max_attempts` | 修复预算。只在 `verify` 失败或回滚时 +1（见 8.5） |
| `checkpoint_id` | 回滚锚点（见 8.6） |
| `last_failure` | 下一次 `decide` 的任务卡里要带上「上次为什么失败」 |

### 4.4 校验规则（7 条，全确定性）

`planner.accept(raw) -> TaskGraph | Failure`：

1. `tasks` 长度 **1–8**。超 8 不截断执行，直接 `PlanInvalid`，重试提示写「请合并到 8 个以内」。——**宁可让模型重规划，也不执行半张图**：截断会丢掉依赖边，导致「验收命令引用了没被改的文件」这类诡异失败。
2. 每个任务的 `title`、`scope`、`commands` 非空。（纯问答由 `intake` 判为 `trivial` 短路，不会走到这里。）
3. `scope` 每一项经 `WorkspaceSecurity.resolve` 校验。**任一项越界 → 整张图无效**。
4. `depends_on` 只能引用本次列表内的下标或标题，机器改写成 `id`；**有环 → 无效**。
5. `commands` 每条必须是非空 argv 数组，且 `commands[0]` 属于 L3 允许列表。不允许 `rm` / `del` / `format` / `shutdown`；**第一期也不允许 `git`**——回滚语义会和 `FileJournal` 分叉。
6. 无依赖任务标 `ready`，其余 `pending`。
7. 校验失败**不执行任何工具**（包括不预热 RAG、不建索引）。

### 4.5 小任务短路（`is_trivial`）

`intake` 判定为闲聊或单步只读问答时（启发式：消息里无「改/修/实现/测试/运行」等动词，且历史没有未完成任务图），不建任务图，直接一次无工具回复并 `halt(completed)`。

**误判成本分析**：把「该拆的」误判成 trivial → 用户要再说一次；把 trivial 误判成「该拆的」→ 多走一轮 `decompose`。后者代价更小，所以**启发式宁可不短路**。词表放在 `planner.py` 常量里，不进提示词（提示词里的是「建议」，代码里的是「规则」）。

---

## 5. 模块二｜仓库感知（`repo` / 现有 `tools/file_tools.py` + `tools/search.py`）

### 5.1 三层漏斗

```text
结构层  list_dir / 目录树          「仓库里有什么」        成本最低，先做
  ↓
检索层  file_search(关键词/正则)    「改动该落在哪个文件」   符号级、精确、无需嵌入
        + AST/tree-sitter 切片
        + rag_search（语义召回）    「名字对不上的地方兜底」
  ↓
精读层  read_file(path, 行区间)     「这段代码到底长什么样」  成本最高，按需
```

### 5.2 为什么「先符号、后语义」，而不是「先 RAG」

这是被约束倒逼的设计，值得写清：

- 项目**默认模型是本地 `qwen2.5-coder:7b`**，嵌入模型是 `nomic-embed-text`。向量检索的召回质量对中文注释、短标识符、跨语言混排都不稳定。
- 而**关键词/正则检索（ripgrep 语义）在这个规模下几乎总是更准**：找一个函数名、一个错误文案、一个 API 路径，精确匹配的命中率远高于余弦相似度。
- 向量检索真正的价值场景只有一个：**模型不知道目标叫什么**（「处理登录超时的逻辑在哪」）。所以 RAG 的定位是**兜底召回**，不是主路径。

结论：`perceive` 的默认批次以 `file_search` 为主，`rag_search` 只在「任务 `detail` 里没有可用的具体符号」时启用；且 RAG 不可用时（向量库初始化失败）**自动退化为只用 `file_search`**，不阻断感知。

### 5.3 强制感知：`perceived` 门闩

`perceive` 不让模型决定第一批读什么，`scheduler.perceive_batch()` 生成固定只读批次：

1. 对 `acceptance.must_read` 每个路径调 `read_file`。路径不存在 → `NotFound`。
2. 若任务 `detail` 里没有具体符号，再调一次 `file_search`（或 `rag_search`，按 5.2 的条件），查询串用**任务标题**，取 top 5 路径。
3. 搜索命中的文件若落在 `scope` 内且还没读，最多再读 3 个，每文件默认前 200 行。模型随后可在 `decide` 里要具体行区间。

读完 `task.perceived = True`。之后 `decide` 仍可发 L0 调用（补充阅读），走 `ActOkReadOnly → decide`，不必重走 `perceive`。

**为什么强制**：本地小模型在「先读后改」上有稳定的偷懒倾向（直接从文件名猜内容）。把「读」从「任务」里剥离出来、变成状态机的必经状态，比在提示词里写三遍「务必先读代码」有效得多。这也是第 1.2 节「不依赖模型自觉」的一个具体例子。

### 5.4 编辑原语与 diff

现有四个原语（`tools/file_tools.py`）已经够用，本设计不改语义，只加两个约束：

| 原语 | 语义 | 新增约束 |
| --- | --- | --- |
| `list_dir` | 列目录（可递归/后缀过滤） | 无 |
| `read_file` | 读文件，带行号，支持 `start_line/end_line` | 作为 `perceive` 的只读原语 |
| `write_file` | 整体写入/新建 | **目标路径必须在任务 `scope` 内**，否则 `ScopeViolation` 且不执行 |
| `edit_file` | ① 精确串替换（要求唯一匹配）② 行区间替换 | 同上；`old_string` 0 次或多次匹配 → `PatchConflict`（走回滚，见 8.3） |

**为什么优先 `edit_file` 而不是 `write_file`**：整文件重写有两个风险——模型重新「编」一遍未改动部分（幻觉）、以及 token 成本随文件大小线性上升。精确串替换把模型的输出约束在**局部 diff** 上，天然更安全、更省。

**diff 从哪里来**：不依赖 git。`FileJournal` 在每次写入前记录**原始字节**，因此机器能算出真实 diff（`changed_paths(checkpoint_id)` + 字节比对）。这样设计的原因见 8.6。

### 5.5 防「只读空转」

`act` 状态有一个专门的守卫：**本任务连续 3 批纯 L0 调用却仍不写、不验收 → `ReadStreakCap` → `repair`（签名 `read_streak`）**，再出现一次即 `Stalled`。

这是实测中很常见的退化：模型反复 `read_file`/`file_search` 表示「正在研究」，但从不落到修改。纯 ReAct 对此无能为力（它会一直陪着读下去，直到轮数耗尽）；状态机可以显式计数并强制中断。

### 5.6 上下文成本控制

| 手段 | 作用 |
| --- | --- |
| 命中即读（只读搜索结果里在 scope 内的前 3 个） | 避免「全库精读」 |
| 行区间窗口（默认前 200 行） | 大文件不必整读 |
| 符号级切片（tree-sitter，失败降级 AST/启发式） | 切片边界落在函数/类上，而不是按字数硬切 |
| 观察环容量 6 条 | 更旧的折叠成一行摘要（见 12 节） |

---

## 6. 模块三｜沙箱终端（`shell.py` + `permissions.py`）

### 6.1 两级执行，各有分工

现有 `tools/executor.py` 的 RestrictedPython 沙箱是 **L2**：禁止 import，看不见工作区文件。它能做纯计算验证，但**不能编译项目、不能跑 pytest、不能读刚改的文件**——而「运行检验自测」这条链路必须落在真实进程上。

所以要补 **L3 `WorkspaceCommandRunner`（`tools/shell.py`，注册 `run_command`）**：

```python
def run_command(argv: list[str], cwd: str = ".", timeout_seconds: int = 60) -> ToolOutcome
```text

| 维度 | L2 RestrictedPython（保留） | L3 WorkspaceCommandRunner（新增） |
| --- | --- | --- |
| 用途 | 纯计算/算法验证 | 编译、测试、静态检查、构建 |
| 隔离 | 受限字节码 + 独立子进程 + 禁 import | OS 进程 + 权限分级 + argv 白名单 |
| 可见性 | 看不到工作区文件 | 工作目录锁在工作区 |
| 谁能发起 | 模型 | 模型，但**必须过 `authorize`** |

**不要让 L2 假装成 L3**：现有沙箱的文档已经写得很清楚（「不能冒充编译器或测试运行器」）。把两者的能力边界写进工具描述，避免模型在沙箱里做文件 IO 失败后反复重试。

### 6.2 权限分级（L0–L5）

**看副作用，不看工具名字像不像只读**。`permissions.classify(tool_name, arguments)` 是唯一判定点：

| 级 | 含义 | 工具 | `auto_workspace` | `confirm_writes` | `plan` |
| --- | --- | --- | --- | --- | --- |
| L0 | 不改盘、不起进程 | `list_dir` `read_file` `file_search` `rag_search` `calculator`；MCP 标注 `readOnlyHint=true` 的 | 自动 | 自动 | 规划确认后自动 |
| L1 | 改工作区文件 | `write_file` `edit_file` | 自动 | **暂停** | 确认计划后自动 |
| L2 | 无文件副作用的计算沙箱 | `run_python_code` | 自动 | 自动 | 确认计划后自动 |
| L3 | 工作区子进程 | `run_command` | 自动，且 argv0 在允许列表 | **暂停** | 确认计划后自动 |
| L4 | 可能改外部系统 | 未标注只读的 MCP 工具 | **暂停** | **暂停** | **暂停** |
| L5 | 工作区外 / 换盘符 / 解释器 `-c` 逃逸 | 解析后越界的任何路径或命令 | 拒绝 | 拒绝 | 拒绝 |

**模型自报的 `effect` 一律丢弃**，解析后由 `permissions.classify` 覆盖。

### 6.3 命令执行的具体约束

| 约束 | 值 | 理由 |
| --- | --- | --- |
| `shell` | **`False`**，不接受单字符串 `command` | 拼字符串 = 命令注入的入口；argv 数组让白名单判定可以精确到「第 0 个元素」 |
| argv0 允许列表 | `python` `pytest` `ruff` `pyright` `pip`（`pip` 仅 `show`/`list`） | 确定性判定，**不把安全裁决交给 7B 模型**；比较时小写并去掉 `.exe` |
| `cwd` | 经 `WorkspaceSecurity.resolve`，必须是目录 | 复用现有门闩，不新增一套路径逻辑 |
| 环境变量 | 当前进程环境副本，**不提供 `env` 参数给模型** | 模型不该能注入 `LD_PRELOAD`/`PYTHONPATH` |
| 超时 | 默认 60s，上限 120s（配置夹紧） | 与 `executor.timeout_seconds` 同族；`kill()` 后等 2s，与现有强杀语义对齐 |
| 输出 | 头尾对半截断（现有 `truncate_output`）**不适用**；命令类保留**尾部 80 行** + 退出码 | 错误栈在尾部；对半截断会把关键信息切掉。这是命令结果的专门化 |
| Windows | 不假设 bash；仅当决策显式请求 `shell` 形态时才用 `powershell -NoProfile -Command`，且仍受允许列表约束 | 本机是 Windows，但不把「有 shell」当默认能力 |

**为什么不做「用户说过可以」的放行**：那是把安全问题交给自然语言。授权只能来自 `ExecMode` 配置或未来的审批 API，不能来自对话内容。

### 6.4 执行档（`ExecMode`）

```python
class ExecMode(str, Enum):
    plan = "plan"                       # 任务图先暂停，确认后按 auto_workspace 走
    confirm_writes = "confirm_writes"   # L1/L3/L4 都暂停
    auto_workspace = "auto_workspace"   # 默认：L0–L3 工作区内自动，L4 暂停，L5 拒绝
    full_access = "full_access"         # 减少确认：已授权范围内 L0–L4 自动，L5 仍拒绝
```

工作台输入栏提供与截图一致的四档选择器（计划模式 / 变更前确认 / 自动编辑 / 完全访问），
选择写入 `data/local_runtime.json → capabilities.exec_mode`，经 `resolve_exec_mode`
热更新到 `LoopDeps.mode`，无需重启。

**`full_access` 不关掉 L5**。它只减少已授权范围内的确认次数；工作区越界、解释器
`-c` 逃逸、未授权本机路径仍由 `WorkspaceSecurity` / `AccessBroker` 硬拒绝。
本机单人、`127.0.0.1` 的威胁模型下，关掉 L5 的收益是负的。

| 级 | `plan`（确认后） | `confirm_writes` | `auto_workspace` | `full_access` |
| --- | --- | --- | --- | --- |
| L0–L3 | 自动 | L1/L3 确认 | 自动 | 自动 |
| L4 | 确认 | 确认 | 确认 | **自动** |
| L5 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

### 6.5 暂停协议与「失败关闭」

`authorize` 发出 `permission_request` 事件后，循环在内存里等一个 `asyncio.Future`。**第一期没有审批 UI**，因此 `confirm_writes` 与 L4 在 0 秒视为**拒绝**并 `blocked`，同时在 `thought` 里说明「当前界面不能确认高权限操作」。

这是**故意的失败关闭（fail-closed）**：不把「等不到人」实现成「自动放行」。`plan` 档同理，在有审批 API 之前等价于「只出计划然后停」。

---

## 7. 模块四｜自测验证（`verify.py`）

### 7.1 完成定义来自数据，不来自模型

`verify` 的输入是 `TaskNode` + 本任务检查点之后的 `FileJournal` 差异 + `Acceptance`，**不调用通用决策模型**（零模型调用，纯机器）。这是 I3 的实现。

### 7.2 三重门

按顺序，前面不过就不跑后面：

**门 1 · 范围门**：收集本任务检查点以来 `journal` 中的写入路径。任一路径不在 `scope` → `ScopeViolation`，**不再跑验收命令**（避免用违规改动的产物污染验证结论）。

**门 2 · 命令门**：顺序执行 `acceptance.commands`，工作目录是 workspace root。任一非 0 退出 → `TestFailed`，**后续命令不跑**（第一个失败通常就是根因，继续跑只会产生噪声）。

**门 3 · 诊断门**：对本次触碰的文件跑诊断。

- `ruff` 在 PATH → `["ruff", "check", *touched]`
- `pyright` 在 PATH → `["pyright", *touched]`
- 只把**新增** error 当失败。第一期没有基线快照，取不到「新增」时**把退出码非 0 视为 `DiagnosticError`**。
- 工具不存在 → `artifacts["diagnostics"] = "skipped"`，**不失败**。

三者都过 → `status=done`，依赖它的任务若全部前驱 `done` 则改 `ready`。

### 7.3 为什么 LSP 用 CLI 优先，而不是常驻语言服务器

| 方案 | 成本 | 收益 | 结论 |
| --- | --- | --- | --- |
| 常驻 LSP 会话（pyright --stdio） | 要管进程生命周期、初始化握手、`didChange` 通知、版本对齐；本地还要装 node 端 | 增量诊断、精准到字符级 | **第二期增强路径** |
| 对触碰文件跑一次 CLI | 一次 `subprocess`，退出码 + 解析文本 | 够用：能拦住语法错误、未定义名、类型错误 | **第一期采纳** |

关键点在于**诚实**：拿不到基线就写 `skipped`，不假装通过。「跳过」和「通过」在验证报告里必须是两个不同的值——否则整个 verify 状态就退化成一句口号。

### 7.4 为什么 `mark_done` 会被机器改写

模型可以在 `decide` 里提交 `kind=mark_done` 试图跳过验证。守卫规则：**只要本任务发生过 L1 或 L3，`mark_done` 一律被改写成 `DecisionIsVerify` → 进 `verify`**。

### 7.5 三类常见幻觉，各被哪道门拦住

| 幻觉 | 实际发生 | 被拦住的机制 |
| --- | --- | --- |
| 「我已修改 `auth.py` 并验证通过」 | 其实只调了 `read_file` | 门 1：`journal` 里没有该路径的写入记录 → 没有改动 → verify 无从通过；加上第 5.5 节的 `ReadStreakCap` |
| 「测试全部通过」 | 根本没跑测试 | 门 2：`acceptance.commands` 由机器执行，退出码是唯一事实来源 |
| 「只改了一个小函数，没影响别处」 | 顺手改了 `section` 外的文件 | 门 1：`ScopeViolation` + 自动回滚（见下一节） |

---

## 8. 模块五｜迭代修复（`repair.py` + `journal.py`）

### 8.1 核心前提：失败必须可分类

`ToolRegistry.execute` 目前把异常吞成 `[工具错误] ...` 字符串（为了兼容旧框架，这个行为保留）。`state_loop` 用适配器 `run_tool` 包一层：**优先读 handler 抛出的异常类型，其次解析已知前缀**，产出 `ToolOutcome`：

```python
@dataclass
class ToolOutcome:
    call_id: str
    name: str
    ok: bool
    failure: Failure | None
    text: str              # 给模型看的正文（已按 output_limit 截断）
    artifacts: dict        # 机器字段：exit_code、changed_paths、diagnostic_count、tail
```text

### 8.2 失败分类学

| kind | 判定 | retryable | repair 动作 |
| --- | --- | --- | --- |
| `ModelProtocolError` | 没有合法 `submit_decision` | 是，仅 1 次 | 同状态重试，**不进 repair** |
| `ArgError` | 缺参、JSON 损坏、schema 不符 | 是 | `RepairEdit`，错误原文进观察 |
| `NotFound` | 文件不存在 | 是 | `RepairEdit`；若发生在 `must_read` → `RepairReplan` |
| `PathDenied` | `PathTraversalError` | 否 | `Denied`，任务 `blocked` |
| `PermissionDenied` | 用户拒绝 / 执行档不允许 | 否 | 任务 `blocked`，**不自动改道重试同一调用** |
| `PatchConflict` | `edit_file` 的 `old_string` 匹配 0 次或多次 | 是 | `RepairRollback` 到本任务检查点，再 `perceive` |
| `CommandFailed` | 进程退出码非 0 | 是 | `RepairEdit`，观察只留尾部 80 行 + 退出码 |
| `Timeout` | 命令/沙箱超时 | 是，同命令最多 1 次 | 第二次同签名 → `Stalled` |
| `SandboxViolation` | RestrictedPython 编译拒绝或运行期拦截 | 是 | 提示改走 `run_command`；若模型再次拿 `run_python_code` 做文件 IO，算**同一签名** |
| `TestFailed` | 验收命令失败 | 是 | 同 `CommandFailed`，但 `attempts += 1` 发生在 `verify` |
| `DiagnosticError` | ruff/pyright 在触碰文件上报新增 error | 是 | `RepairEdit` |
| `ScopeViolation` | 实际写入路径不在 `scope` | **否** | `RepairRollback` → `RepairReplan` |
| `McpError` | 传输断开、`tools/call` 报错 | 是，仅 1 次 | 第二次 `blocked` |
| `LoopStall` | `failure_counts[signature] >= 2` | 否 | `halt(stalled)` |
| `BudgetExhausted` | 压缩后仍超硬阈值，或步数耗尽 | 否 | `halt(budget)` |
| `Cancelled` | 生成器被关闭 | 否 | `halt(cancelled)`，**已写文件不自动回滚** |

`SandboxViolation` 那一行的细节值得留意：**把「换了个工具做同一件错事」算作同一签名**。否则模型可以靠换工具名绕过停滞检测，无限循环。

### 8.3 修复策略判定树

```text
失败 → 分类
 ├─ ArgError / CommandFailed / TestFailed / DiagnosticError / NotFound(非must_read)
 │     → RepairEdit：带着失败原文回到 decide，让模型提新补丁
 ├─ PatchConflict / ScopeViolation / 连续同类错误
 │     → RepairRollback：还原到本任务 checkpoint，再 perceive（重新看真实文件）
 └─ 范围错误 / 停滞前最后一次升级
       → RepairReplan：回到 decompose 重做任务图
```

判定是**确定性代码**（基于 `kind` + `failure_counts` + `attempts` 余量），不是模型选。

### 8.4 失败签名归一化

```text
signature = sha1(f"{kind}|{tool}|{normalized}")[:12]
```

`normalized` 要去掉：行号、临时路径、耗时数字、ANSI 颜色码。

**为什么必须归一化**：同一个错误每次报的行号会变（因为改动位移），时间戳会变。不归一化的话，`failure_counts` 永远统计不到「同一错误重复出现」，停滞检测就完全失效——这是「逻辑上写了停滞检测但其实不生效」的典型陷阱。

### 8.5 预算归属：谁消耗 `attempts`

**`attempts` 只在 `verify` 失败或 `RepairRollback` 时 +1。**

只读工具的失败**不加**。理由是量级差异：一次 `file_search` 没命中是很便宜的事件，如果它也消耗修复预算，会出现「搜了 3 次没搜到 → 任务直接 blocked」这种荒谬结果。**预算要绑在「有代价的动作」上**。

### 8.6 FileJournal：为什么用字节快照而不是 git

```python
class FileJournal:
    def checkpoint(self, task_id: str, paths: list[Path]) -> str: ...
    def remember_write(self, call_id: str, path: Path, before: bytes | None): ...
    def restore(self, checkpoint_id: str) -> list[str]: ...
    def changed_paths(self, checkpoint_id: str) -> list[str]: ...
```

| 决策 | 理由 |
| --- | --- |
| 用内存字节快照，不用 `git stash`/`git checkout` | **不假设工作区是 git 仓库**（本仓库现在就不是）。而且混合「用户自己的未提交改动 + 我们的自动回滚」会让 git 状态不可解释 |
| 进入任务第一次 L1 之前 `checkpoint` | 回滚点是「任务开始前」的真实状态 |
| 目录 scope 不递归整棵树，只在具体写入路径上 `remember_write` | 递归快照整个目录树的成本不可控 |
| `before is None` 表示文件原先不存在 → `restore` 时删除 | 正确区分「还原」与「删除」 |
| `restore` 用原始字节写回，再经 `WorkspaceSecurity.resolve` 复查 | 防止检查点被换路径后误写 |
| 回滚后**清空该任务在观察环里的写入结果正文**，换成「已回滚到 checkpoint cx，原因…」 | 否则模型会对着**已经不存在的文件内容**打补丁，产生必然失败的补丁 |
| 日志只在内存，进程退出即丢 | 崩溃恢复不在第一期（见第 15 节） |

### 8.7 为什么 `repair` 自己不改文件

`repair` 只决定「下一个状态是什么」+「把哪个 `Failure` 放进下一次任务卡」。**补丁仍然由模型在 `decide` 里提出**，再走完整的 `authorize → act → verify`。

这条约束换来的东西很实在：

1. **模型是唯一的写入者**——审计时「谁改的」只有一个答案；
2. 修复路径和正常路径**走同一套权限与验证**，不存在「修复模式下的特权代码」；
3. `repair` 本身是纯函数，可单测。

### 8.8 用户取消时的语义

客户端断开 → `halt(cancelled)`，**保留磁盘改动**，并在收尾 `thought` 里列出本轮检查点 id。**不悄悄回滚**——用户可能已经看到了一半的文件，静默还原会造成更严重的困惑。自动回滚只发生在 `ScopeViolation` 与 `PatchConflict` 两种「明确是错的」情形。

---

## 9. 模块六｜子 Agent 委派（`delegate.py`）

### 9.1 触发条件（不是「想委派就委派」）

`schedule` 状态守卫 `CanDelegate` 三者必须同时成立：

1. 至少有 **2 个 `ready` 且 `perceived=false`** 的任务；
2. 它们的**写路径租约不相交**（`scheduler.lease(paths)`）；
3. 当前**深度为 0**（子代理不能再委派孙子代理）。

### 9.2 隔离矩阵

| 项 | 父循环 | 子循环 |
| --- | --- | --- |
| 消息 | 父自己的观察环 | **新建空观察环**，只含任务卡；从 `perceive` 起自己读码，不继承父观察 |
| 工具 | 全部已授权工具 | 同一 `ToolRegistry`，但 `authorize` 用**子任务的 `scope`** |
| 写租约 | `scheduler.lease(paths)` | 租约相交 → 不允许并行，改串行 `NeedDecide` |
| 深度 | 0 | **1**；子循环的 `CanDelegate` 守卫**恒假** |
| 预算 | 父预算 | 父剩余预算的 **40%**，硬顶 8000 字符 |
| 返回 | — | 只返回 `TaskResult`：status、changed_paths、verify 摘要、failure。**不返回子观察全文** |
| 会话库 | 父回合结束由 `routes_chat` 落盘 | 子循环**不调用** `HistoryStore` |
| 并发 | — | `asyncio.gather`，**上限 3**；合并时按 task id 排序写入父任务图 |

**为什么禁止并发 `append` 同一个共享 list**：这是现有 LangGraph 实现留下的实际教训（`tools_node` 里 `asyncio.gather` 并发往同一 `messages` append，顺序不确定）。子代理结果合并时按 task id 排序写回，保证结果稳定可复现。

### 9.3 为什么深度锁 1、并发 3

- **深度锁 1**：委派的价值在「并行」和「上下文隔离」，不在「层级」。开二级委派会让预算和状态归属变得难解释，而收益（多一层并行）在单机场景几乎为零。
- **并发 3**：默认模型是本地 Ollama 单实例。**模型推理本身是串行的**，并发子代理只在「工具调用/文件 IO 阶段」真正并行。并发数超过 3 主要增加队列长度与 token 争用，而不是吞吐。

### 9.4 与「角色扮演式多 Agent」的本质区别

现有 `config.yaml` 的 `frameworks.autogen.roles`（架构师/工程师/评审员）是**人格分工的对话**：三个角色轮流发言，用不同 system prompt 引导。

本设计的委派是**任务图上的并行执行**：

| 维度 | 角色扮演（autogen roles） | 任务图委派（本设计） |
| --- | --- | --- |
| 划分依据 | 人格/职责描述 | **依赖拓扑 + 写路径租约** |
| 结果归属 | 对话历史（散文） | 结构化 `TaskResult` |
| 冲突处理 | 靠提示词「请评审员检查」 | 靠租约：路径相交就**不允许并行** |
| 可测性 | 难（输出是对话） | 易（结果对象可断言） |

结论：**并行只在能证明「不冲突」时才发生**，不能靠人格分工假设它们各管一摊。

### 9.5 失败语义

子循环命中 `halt(blocked/stalled/budget)` → 父任务标 `blocked`，**父循环继续 `schedule` 其他兄弟任务**，不因此取消整个用户回合。只有全部兄弟都 blocked，父才 `GraphBlocked` 结束。

**部分成功好过全盘失败**：3 个并行任务挂了 1 个，另外 2 个的成果应当保留并交付。这一点和 ReAct（一个错误就整轮结束）有本质区别。

### 9.6 子代理共享 MCP 连接

子代理**不持有独立 MCP 连接**，共用父进程的 client，调用仍经父注册表。原因很具体：stdio 型 MCP server 是子进程，每个子代理各拉一个会导致 `n` 倍进程开销与握手延迟；共享客户端让连接数恒为「配置里的 server 数」。

---

## 10. 模块七｜MCP 原生扩展（`tools/mcp/`）

### 10.1 「原生」的定义（这是本节的核心）

MCP 不是「加一个调用外部服务的工具」，而是**一等工具源**。四条同构：

| 同构项 | 具体含义 |
| --- | --- |
| 同一契约 | MCP 工具桥接成 `tools.base.Tool`，进同一个 `ToolRegistry`，与内置工具**零差别** |
| 同一权限管线 | 走同一个 `authorize`；effect 由 `permissions.classify` 判定，不由 server 说了算 |
| 同一事件流 | `tool_call` / `tool_result` 事件形状与内置工具完全一致，前端不需要知道这是 MCP |
| 同一预算 | MCP 结果走同一 `output_limit` 截断与观察环折叠 |

反过来，**MCP 不构成第二套循环**：它没有自己的重试策略、没有自己的停止条件、不引入新状态。

### 10.2 生命周期

| 阶段 | 动作 | 失败策略 |
| --- | --- | --- |
| `load` | 解析 `config.yaml -> mcp.servers`；名称须匹配 `^[a-z][a-z0-9_]{0,31}$` | 非法配置 → 打印警告**跳过该条**，不阻止对话（与现有 RAG 初始化失败策略一致） |
| `connect` | stdio 拉起子进程；SSE / Streamable HTTP 建会话；握手 `initialize` | 该 server 标 `down`，工具不注册 |
| `discover` | `tools/list`，支持分页则拉全 | 失败 → `down` |
| `bridge` | 每个工具注册为 `mcp_<server>_<tool>`，handler 调 `tools/call`；描述用 server 的 description，schema 用 `inputSchema` | 同名冲突 → 跳过并警告 |
| `refresh` | 收到 `notifications/tools/list_changed` 时重新 list，增删注册表项 | 刷新失败保持旧表，记一条日志 |
| `call` | **仅当状态机处于 `act` 且该调用已 `Granted`** | 超时 30s → `McpError` |
| `close` | `Runtime` 丢弃或进程退出时 `shutdown`，stdio 子进程 terminate | 忽略关闭错误 |

### 10.3 三个值得展开的设计点

#### ① 工具目录（tool catalog）——上下文预算措施，不是第二协议

工具数 > 32 时，不把全部 schema 放进每次模型请求。`decide` 的可见工具 = 内置工具（完整 schema）+ MCP 工具目录（**仅 name + 一行 description**）。模型若调用目录中的工具，机器在执行前用完整 schema 校验参数；校验失败是 `ArgError`，**不会发到服务器**。

这样做的收益是双向的：省 token，且让「参数错误」在本地就被拦下（不发无效请求给远端）。

#### ② effect 判定：不猜描述文本

- server 级 `read_only: true`，**或**工具元数据含 MCP 官方 annotations 的 `readOnlyHint: true` → **L0**
- 其他一律 → **L4**（暂停等人）

**绝不根据 description 里的文字猜只读**。「查询」「获取」这类词出现在一个实际会创建资源的工具描述里是完全可能的，而猜错的代价是把外部状态改了。

#### ③ 失败降级与 RAG 同构

MCP server 连不上、`tools/list` 失败、OAuth 没走完 → 该 server 标记 `down`，**省略它的工具，对话继续**。这个策略和现有 RAG（向量库初始化失败 → `rag_enabled = False`，对话继续）是同一个哲学：**可选增强不得成为主链路的单点故障**。

### 10.4 明确不在范围内（第一期）

- **不把本进程暴露成 MCP Server**。本项目是消费方，不是能力提供方；开放出去会和「只监听 127.0.0.1、工作区锁定」的威胁模型直接冲突。
- **不桥接 resources 与 prompts**。会和 `prompts/*.md` + RAG 形成两套上下文来源，收益不明而冲突明显。

### 10.5 配置形态

```yaml
mcp:
  servers:
    - name: docs
      transport: stdio          # stdio | sse | streamable_http
      command: ["uvx", "some-server"]
      read_only: false          # true 则该 server 全部工具视为 L0
```text

---

## 11. 关键设计决策清单（Decision Log）

一页看完「选了什么、代价是什么、什么时候该重审」：

| # | 决策 | 相比替代方案的代价 | 何时需要重审 |
| --- | --- | --- | --- |
| 1 | 控制面用显式状态机，不用 LangGraph / ReAct 提示词 | 调度、预算、权限、快照、验收全要自己写 | 若出现「状态机无法表达」的新交互模式 |
| 2 | 模型只产出类型化 `Decision`，不从散文抠 JSON | 对模型协议遵从性有要求（弱模型需重试） | 换更强模型后可考虑放宽 schema |
| 3 | 完成判定 = 进程退出码 + 诊断，不由模型宣布 | 对「没有可运行验收命令」的任务（如纯文档）需要一个占位约定 | 引入文档类任务验收策略时 |
| 4 | 回滚用字节快照，不用 git | 不做跨进程持久化；进程退出即丢 | 需要崩溃续跑时（第二期） |
| 5 | L0–L5 与 argv 允许列表是确定性代码 | 灵活性低于「模型判断命令是否安全」 | 需要支持更宽的命令集时，扩白名单而非改成模型判定 |
| 6 | 四档执行模式，默认 `auto_workspace`；`full_access` 减少确认但不关 L5 | 高权限操作可人工确认；无审批通道时失败关闭 | 工作台已提供审批 UI/API |
| 7 | 子代理深度锁 1、并发 3、写租约必须不相交 | 并行度受限 | 换成可并发的模型服务后 |
| 8 | 观察环容量 6 + 软 70%/硬 92% 压缩阈值 | 更早触发摘要，信息有损 | 换更大窗口模型时 |
| 9 | MCP 只做客户端，effect 不猜描述 | 需要 server 提供 annotations 才能自动放行 | MCP 生态 `readOnlyHint` 普及后可减少 L4 暂停 |
| 10 | 一期不改前端事件 switch | 新事件（plan/task/verify/rollback）暂时只能以 `thought` 呈现 | 前端排期到位时 |

---

## 12. 上下文预算与压缩（`context.py`）

### 12.1 预算组成

默认 24000 字符（配置键 `agent.context_char_budget`）。**估算方式**：本地模型没有可靠 tokenizer，用 `tokens ≈ chars / 2`（中英混合的保守上界，**宁可早压缩**）。不引入 `tiktoken` 作为硬依赖。

| 区块 | 策略 |
| --- | --- |
| 系统提示词 | **图钉**（不可压缩）。复用现有 `compose_system_prompt`，但 `state_loop` 下删掉「任务流程」那一段，改为一句「流程由运行时执行，你只提交 Decision」 |
| 任务图 | **图钉**。每任务一行：id、状态、scope、验收 argv、attempts、最后失败 kind |
| 当前任务正文 | **图钉**。`detail` 全文，上限 2000 字 |
| 本任务最近观察 | **环**，默认 6 条。更旧的收成一行摘要（工具名、ok、kind、首行） |
| 会话历史 | 只保留用户原话 + 上一轮助手最终答复。**不回放旧框架的整段 tool 消息** |

最后一行是重要差别：`get_llm_messages` 返回的全量历史**不能直接塞进 `decide`**。旧框架把每条 tool 结果都留在历史里，那是导致上下文膨胀的主因。

### 12.2 两级压缩

进入 `decide` / `decompose` 之前估算。≥70% 触发 `compress`：

1. **确定性折叠（不调用模型）**：观察环超 6 条的部分换成摘要行；单条命令输出换成 `artifacts["tail"]`。
2. **仍 ≥70%** 才调一次模型，只允许输出 800 字以内的「已做完的事实」列表；输入是**被折叠的观察**，不是整段历史。摘要替换那些观察。失败则保留折叠结果，**不重试摘要**。
3. 折叠后仍 ≥92% → `halt(budget)`。

**压缩不改 `TaskGraph`，不改 `FileJournal`**——压缩只影响「模型看到什么」，不影响「机器知道什么」。这条边界很重要：否则压缩会污染控制面状态。

---

## 13. 移除 LangGraph：已完成的迁移

### 13.1 改动清单

| 文件 | 处理 |
| --- | --- |
| `agents/langgraph_agent.py` | **删除**（含 `__pycache__` 字节码） |
| `agents/registry.py` | 移除导入与 `_CORE` 注册；新增 `DEFAULT_FRAMEWORK = "native_react"`；对已移除的 `langgraph` 与未实现的 `state_loop` 给出**可执行的替代提示** |
| `agents/agent.py` | `resolve_framework` 兜底改为 `DEFAULT_FRAMEWORK`（单一来源）；装配失败时的 fallback 从 LangGraph 改为 `NativeReActAgent` |
| `agents/agent.yaml` | `framework: native_react` |
| `app/config.py` | 默认配置 `framework: native_react` |
| `requirements.txt` | 删除 `langgraph>=0.2.0` 依赖行，改为说明控制面为自研实现 |
| `README.md` | 移除四处 LangGraph 表述；「切换 Agent 框架」章节改写 |
| `agents/base.py`、`agents/events.py`、`tools/api_client.py` | 注释中的框架列举去掉 LangGraph |
| `tests/test_agent_spec.py` | 用例中的框架名改为可用框架 |
| `docs/agent-loop-architecture.md` | 顶部加修订说明；A1 与「明确不做」中「保留 LangGraph」的表述标注作废 |
| `config.yaml` | 新增 `mcp.servers: []` 与 `executor.command_timeout_seconds`（前向预留，当前不生效） |

### 13.2 保留而未改的两处

| 位置 | 为什么不改 |
| --- | --- |
| `app/web/prompt-optimizer/agent.js` 里的 `/import\s+langgraph/` 正则 | 这是**通用代码检测器**（判断一段用户代码是否用了某框架），不是本项目的依赖。删掉反而削弱它的能力 |
| `app/web/prompt-optimizer/ui.js` 的示例文本 | 同上，是演示数据 |

### 13.3 验证结果（本轮实测）

```text
supported = ['native_react', 'autogen', 'llamaindex', 'crewai']
default   = native_react
resolve(spec={}, cfg={}) = native_react
build_agent('langgraph') -> UnknownFrameworkError: langgraph 已从本项目移除…
import agents.langgraph_agent -> ImportError（已删除）
unittest: Ran 11 tests — OK
```

### 13.4 遗留事项（诚实记录）

**Python 环境里的 `langgraph` 包仍然装着**（`pip list` 可见）。它现在对本项目**完全无用**（没有任何代码导入它），但也没有副作用。是否 `pip uninstall langgraph`（会连带处理 `langchain-core` 等依赖）属于环境层面的操作，**留给使用者决定**，本项目不做静默卸载。

### 13.5 为什么默认先落在 `native_react` 而不是直接切 `state_loop`

因为 **`state_loop` 还没实现**。把默认框架写成一个尚不存在的名字，换来的是一次启动即崩。迁移路径是：

```text
langgraph（已删除）
    ↓
native_react（当前默认，零依赖、必定可用）
    ↓
state_loop（切片 1–5 通过测试后切换默认，native_react 保留为兜底）
```

`agents/registry.py` 对 `state_loop` 的报错文案已经写成「目标框架名，尚未实现，当前请使用 native_react」——让配置错误自带下一步。

---

## 14. 与 Zcode 的方案差异

> **材料范围声明**：本节只依据 Z.ai 官方文档（`zcode.z.ai` 的 Goal Mode、Subagents、MCP 页）与公开评测文章。
> 凡涉及「内部如何实现」的推断，一律显式标注**公开信息推断**，不写成事实。
> Zcode 的产品版本迭代很快（changelog 显示每周多次发布），以下对比的时点为 2026-09 的公开材料。

### 14.1 Zcode 的公开机制（如实转述）

| 能力 | 官方口径 |
| --- | --- |
| 产品形态 | 桌面 **Agentic Development Environment**（macOS/Windows/Linux），内置文件管理、终端、Git 面板、浏览器预览等 20+ 工具；应用免费，模型接入另计 |
| **目标模式（Goal Mode）** | `/goal` 设一个目标。每轮结束**自动校验目标是否达成**；未达成则给出下一步并**自动开始下一轮**；达成才收尾。支持 `pause`/`resume`/`replace`/`clear` |
| 校验的证据要求 | 官方明确：**计划、待办清单、跑了很久、听起来像结论的回复都不算数**；要有「改出来的文件、命令输出、测试结果」这类可核对的东西。且**只要还有待办未完成，校验不会判定完成** |
| 状态恢复 | 官方明确：**Goal 状态由系统存储**，关闭会话后重新打开仍在，可从断点继续；暂停不丢已完成轮次、工具调用历史与产出文件 |
| 目标终止条件 | 三种：校验判定完成、用户暂停/清除、**达到该目标的用量预算** |
| 执行档 | 5 档（另有资料记为 4 档）：Default（平衡）、Confirm Before Changes（改动前确认）、Auto Edit（编辑自动、命令确认）、Plan（先出计划待批）、Full Access（最少打断） |
| 子代理 | 内置 **General-purpose**（读写全工具）与 **Explore**（严格只读，官方称「硬保证」）；v3.2.0 起支持自定义子代理（用户级 `~/.zcode/agents/`，可配模型、描述、逐工具读写权限、system prompt）。**前台执行**：并行子代理并发跑，但主任务阻塞等待全部完成；**暂无后台委派** |
| 多代理协作 | orchestrator 自行判断任务可并行并派生 subagent；用户可在目标描述里强制单代理以避免冲突 |
| MCP | 客户端形态；支持 **stdio / SSE / HTTP**；支持 **OAuth** 远程服务；用户级与工作区级两种 scope；可从 Claude Code / Codex CLI / OpenCode **导入**已有配置；**某个 server 授权未及时完成时标记该 server 失败并继续会话**，不阻塞会话 |
| 扩展面 | Plugins 可捆绑 skills、commands、subagents、MCP servers；另有 hooks、automations、memory、remote control（微信/飞书/Telegram 手机端）、SSH/WSL/Docker 远程开发、Off-Peak 任务 |
| 上下文 | 依赖 GLM 系列 **1M token** 上下文窗口；官方披露约 98% 上下文缓存命中 |

### 14.2 逐项差异

| 维度 | Zcode（公开口径） | state_loop（本设计） | 差异性质 |
| --- | --- | --- | --- |
| **控制面归属** | Goal Mode 是「目标管理 + **每轮一次校验**」；文档未公开校验的内部实现（第三方评测建议视为「agent 检查自己的产出」，而非独立裁判） | 转移表是代码；`verify` 是**零模型调用**的状态，跑 `Acceptance.commands` 并读退出码 | **设计取向差异**：Zcode 把完成判定交给「每轮的一次校验」，本设计把完成判定交给 `Acceptance` 数据 + 子进程退出码 |
| **完成定义的存放位置** | 目标描述里的可校验表述（官方建议写「把 pnpm test 跑通且首屏 <2s」这类） | 结构化 `Acceptance.commands`（argv 数组，任务图的一部分，模型**不能在 verify 时临场改**） | 差别在**「谁能改完成定义」**：我们的验收命令在 `decompose` 就冻结了 |
| **证据态度** | 官方明确拒绝「计划/清单/努力/听起来像结论」 | 同样的哲学，但机制化：门 1 查 `journal` 有无真实写入、门 2 读退出码、门 3 读诊断 | **高度一致**，可以说 Zcode 文档把这条原则写得很清楚，本设计把它做成了硬门 |
| **状态恢复** | ✅ 系统级存储，关会话再开可续跑 | ❌ 第一期 `LoopState` 只在内存，进程退出即丢 | **本设计明确弱于 Zcode**（本设计第 15 节列为已知短板） |
| **执行档** | 5 档，含 **Full Access**（最少确认） | 4 档，默认 `auto_workspace`；`full_access` 减少 L4 确认，**不关 L5** | 威胁模型差异：我们仍硬拒绝越界；少确认不等于关掉路径门闩 |
| **审批交互** | 有 UI：权限提示展示将要执行的命令/文件变更/工具动作 | 第一期**无审批 UI**，L4/`confirm_writes` 一律**失败关闭**（0 秒视为拒绝） | 我们的第一期是「宁可不动」，等审批 API |
| **子代理的划分依据** | **角色与权限维度**：General-purpose / Explore（只读硬保证）/ 自定义（可配模型与逐工具权限）；orchestrator 按 subagent 描述匹配任务 | **任务图拓扑维度**：≥2 个 ready + 写路径租约不相交才并行；深度锁 1 | 取向差异：Zcode 以「能力/权限画像」分工，本设计以「写入冲突可证明不存在」为前提 |
| **子代理执行模型** | 并行子代理并发跑，**主任务阻塞等待全部完成**（前台，暂无后台委派） | `asyncio.gather`，上限 3；同样前台等待父回合 | **一致**（都选择前台）；我们用租约做前置约束，官方文档也提示了「冲突编辑、重复劳动」是主要失效模式 |
| **子代理模型分配** | ✅ 自定义子代理可 pin 不同模型（如只读研究用便宜模型） | ❌ 单 `LLMClient`，无 per-agent 模型 | **本设计弱项**（本地单实例模型场景下收益低） |
| **MCP 架构直觉** | 客户端、stdio/SSE/HTTP、单 server 失败不阻断会话 | 完全相同的三条原则 | **高度一致** —— 这是 MCP 作为协议该有的样子 |
| **MCP 生态完整度** | OAuth、外部 Agent 配置导入、用户级/工作区级 scope、插件捆绑 MCP、内置 MCP 服务（含视觉理解） | 第一期只有 connect/discover/bridge/refresh/call/close，无 OAuth、无导入、无 scope 分层、不做 MCP Server | **本设计在集成面明显更薄** |
| **MCP 权限判定** | 权限提示会展示将要执行的命令/文件变更/工具动作（交互层面） | `readOnlyHint` / server `read_only` → L0，否则 L4；**不猜描述文本**；且 MCP 工具与内置工具共用同一 `authorize` 与同一 `tool_call` 事件 | **本设计在权限判定上更机制化**（把 MCP 纳入统一 L0–L5 管线） |
| **上下文策略** | 靠 **1M token 窗口** + ~98% 缓存命中，把整个仓库留住 | 靠**预算 + 两级压缩**（24000 字符，软 70%/硬 92%），因为默认模型是本地 7B | **约束倒逼的差异**：窗口大小决定策略 |
| **可扩展面** | plugins（skills/commands/subagents/MCP/hooks/automations/memory） | skills（工具白名单 + 提示词）+ MCP。**明确不做 hooks/automations** | 产品面差距巨大，本设计**有意收窄** |
| **产品形态** | 桌面 IDE 级 ADE + 手机远控 + SSH/WSL/Docker | 本机 FastAPI 服务 + 自写原生前端 + SSE | 本设计是「可审计的最小内核」，不是产品 |
| **并发目标** | 多个 Goal 可同时跑，各自独立跟踪 | 一次用户消息 = 一张 `LoopState`，无跨会话并发调度 | 本设计**不做多 Goal 并发** |
| **失败处理** | 官方未公开逐类失败策略（公开信息推断：由模型在下一轮自行调整） | 17 种 `Failure.kind` + 归一化签名 + 停滞检测（同签名 2 次停机）+ 三种修复策略判定树 | **本设计的差异化重点**：把「迭代修复」做成可枚举的分类学 |

### 14.3 我们更强的地方

1. **完成判定不经过模型**。Zcode 的每轮校验按官方文档与第三方评测是「agent 自己查自己」；我们的 `verify` 状态**零模型调用**，只有 `subprocess` 退出码、`journal` 写入记录、诊断输出三个事实来源。这是 I3 的价值。
2. **失败可分类、可停机**。17 种 `Failure.kind` + 签名归一化 + 同签名两次即 `stalled`。Zcode 公开材料里没有对应的分类学（不排除内部有，只是未公开）。对本地小模型尤其重要——**重复犯同一个错是 7B 模型的典型行为**。
3. **子代理并行的前置证明**。我们用「写路径租约不相交」作为并行的**必要条件**，而不是事后发现冲突。官方文档自己也提示「冲突编辑、重复劳动」是多代理的主要失效模式。
4. **MCP 权限管线统一**。MCP 工具与内置工具共用 `authorize`、共用 L0–L5、共用事件形状。这让「第三方工具」不会成为权限的例外通道。
5. **全链路可审计、可回放**。每个状态转移一个事件，既发前端也写日志；`FileJournal` 提供真实 diff。整个控制面在自己仓库里，可以单测。

### 14.4 我们更弱的地方（必须承认）

1. **不做状态恢复**。Zcode 的 Goal 状态系统级存储、关会话可续；我们第一期进程退出即丢。**这是最实质的差距**——长时间任务的可续跑性，是本设计最大短板。
2. **没有审批 UI**。第一期的 L4/`confirm_writes` 等于「直接拒绝」，高权限能力实际上是**关着**的。
3. **MCP 集成面薄**。无 OAuth、无配置导入、无 scope 分层。
4. **子代理不能配不同模型**，也没有后台委派。
5. **没有产品化外壳**：无终端面板、无 Git 面板、无浏览器预览、无远程/手机端。
6. **上下文容量差一个量级**（24000 字符 vs 1M token）。这直接限制了单次可处理的任务规模。

### 14.5 与 Trae / Cursor 的一句话说清差异

| 产品 | 它是什么 | 与本设计的关键差异 |
| --- | --- | --- |
| **Trae** | IDE + SOLO/内置 Agent，先给可执行计划、用户确认后逐步做；MCP 同为客户端（stdio/SSE/Streamable HTTP） | Trae 的「计划确认」是产品交互；本设计把它做成 `authorize` 的一个暂停点，且**没有审批 UI 时失败关闭**——不自动开做 |
| **Cursor** | 子代理（Explore/Bash/Browser，可只读可后台）、Auto-review（允许列表 → 沙箱 → 分类器）、本地检查点、ACP 权限答复（allow-once/always、reject-once） | 我们**不引入分类器模型**判定命令安全性（L0–L5 + argv 白名单是确定性的，本地 7B 不承担安全裁决）；子代理深度锁 1 且必须租约不相交；回滚是文件字节检查点，不是跨消息检查点 |
| **Zcode** | 目标模式 + 子代理 + 桌面 ADE | 见 14.2；核心差异是**完成判定归属**与**失败分类学** |

### 14.6 一句话结论

**Zcode 的 Goal Mode 解决了「别让我一直说继续」；本设计的 `state_loop` 想解决的是「别让模型自己说做完了」。**

前者是交互层与产品层的进步（状态持久化、轮次可见、手机远控都很扎实），后者是控制层的纪律（完成判定经机器、失败必须分类、副作用必须授权）。两者不冲突——事实上 Zcode 文档里「校验看的是实据」和我们的 `verify` 是同一个信念，区别只在于**信念落在文档里，还是落在代码里**。

---

## 15. 落地路线图与风险

### 15.1 切片顺序（每片可独立验收）

| 切片 | 内容 | 验收方式 |
| --- | --- | --- |
| **1** | `state_loop/state.py` + `machine.py`：`LoopState`、转移表、停止条件（纯函数） | 转移表每一行一个用例；非法组合不落到别的状态 |
| **2** | `state_loop/journal.py`：检查点与 `restore` | 覆盖文件能还原；新建文件能删除；scope 外路径被拒 |
| **3** | `tools/shell.py`：`WorkspaceCommandRunner` + `run_command`；`permissions.py` | `shell=False`；argv0 白名单；`python -c` 判 L5；工作区外 `cwd` 判 L5；超时强杀 |
| **4** | `state_loop/verify.py` + `repair.py`：三重门、失败分类、停滞检测 | 写入 scope 外 → 不跑验收命令；同签名两次 → `Stalled` |
| **5** | `state_loop/agent.py`：接 `BaseAgent`，注册进 `registry._CORE`；`decompose`/`decide` 先用**可注入的假决策器** | 用假模型跑通「改文件 → 命令失败 → 回滚 → 再验证 → 通过」的完整闭环 |
| **6** | `tools/mcp/`：MCP 客户端 + 桥接 | 配置空列表不影响启动；server down 不阻断对话 |
| **7** | `state_loop/delegate.py`：子代理 | 租约相交时不并行；子 blocked 不取消父回合 |

**切片 5 之后才把 `agent.yaml` 的 `framework` 切到 `state_loop`。** 切片 6/7 依赖前面的 `authorize` 与 `ToolOutcome`，但可以后加而不改状态名。

模型相关测试全部用**假 `LLMClient`** 返回固定 `submit_decision`。不把真实 Ollama 放进单元测试。

### 15.2 风险与未决问题

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **本地模型的协议遵从性** | 7B 模型可能不总是输出合法的 `submit_decision` | 1 次重试 + 明确的重试提示；协议违反是显式失败（`halt(blocked)`），不静默降级 |
| **验收命令的设计质量** | 任务图上写的 `commands` 如果没意义（如 `python -c "pass"`），verify 就成了形式主义 | 第 6.2 节校验规则 5 限制 argv0 白名单；后续可加「验收命令必须引用 `scope` 内文件」的启发式告警 |
| **LSP 无基线** | 取不到「新增 error」基线时，退出码非 0 即失败，可能误伤历史遗留错误 | 第一期接受（显式记 `skipped` 或按退出码）；第二期引入诊断快照 |
| **子代理对单实例模型的争用** | 并发 3 个子循环共享一个 Ollama 实例，实际是串行排队 | 并发上限可配；文档写明「并发收益仅在工具/IO 阶段」 |
| **`state_loop` 未实现期间的双轨** | 默认框架是 `native_react`（ReAct），与设计目标不一致，用户可能困惑 | README 与 registry 报错文案都写明迁移路径 |
| **状态不持久化** | 进程重启丢失任务图与观察环（对照 Zcode 的弱项） | 明确列为第二期；`LoopState` 已按可 `asdict` 设计，落盘不需要改结构 |
| **未决**：是否支持多 Goal 并发 | Zcode 支持，本设计不支持 | 待明确产品形态后再定 |

---

## 16. 相关文档

| 文档 | 内容 |
| --- | --- |
| 本文 | 架构设计、模块设计逻辑、Zcode 对比 |
| [`agent-loop-architecture.md`](./agent-loop-architecture.md) | 本文的实现规格附录：逐行可测的转移矩阵、字段级 dataclass 定义、模型协议 JSON 形状、配置与测试清单 |
| `README.md` | 项目定位、启动方式、框架切换 |
| `agents/registry.py` | 框架注册表（含对已移除/未实现框架的提示文案） |

---

## 17. 实现回填（2026-09-21 完成实现后的偏差记录）

本文保持设计原样以便对照；**实现过程中与设计不一致或有补充的地方记在本节，以本节为准。**

| # | 设计原文 | 实现后的处理 | 性质 |
| --- | --- | --- | --- |
| 1 | §5.1 的 `repair` 只有 `RepairEdit` / `RepairRollback` / `RepairReplan` / `Stalled` 四个出口 | 新增 **`repair_blocked → schedule`**：任务被阻塞后继续调度兄弟任务（部分成功好过全盘失败）。原表缺少「任务阻塞但回合继续」的出口，只有「全部阻塞才由 `graph_blocked` 结束」这一条 | **设计补充** |
| 2 | §5.1 未描述「闲聊/单步问答短路」的转移 | `(intake, turn_accepted)` 增加守卫分支：非动作类消息直接 `halt(completed)`，不建任务图。规则写成「同事件两行、末行无守卫」，符合表纪律 | **设计补充** |
| 3 | §6.3 只写了「解释器 `-c` 逃逸」 | 明确区分 **`-c`（拒绝）** 与 **`-m`（按模块白名单放行：`pytest` / `unittest` / `py_compile` / `compileall` / `json.tool` / `pip`）**。一刀切禁 `-m` 会禁掉 §6.1 自己示例里最常用的跑测试写法 | **设计澄清** |
| 4 | §6.3 未规定 `argv[0]` 的形态 | `argv[0]` **不接受路径**（含 `/` 或 `\` 即拒）。否则白名单约束的只是「文件名长什么样」，在工作区放个同名可执行文件就能绕过 | **安全加固** |
| 5 | §8.2「同一失败签名」的构造 | 实现为：**异常类名优先**（`tools/errors.py` 的类名即失败类别），仅当类型缺失或属泛用类型（`ValueError`/`TypeError`/`OSError`…）时才回落到关键词匹配 | **实现细化** |
| 6 | §7 的失败分类表 | 新增 `ToolError` 作为**分类兜底**（宁可标为待修，也不要漏判）；新增 `tools/net.py`：**本机地址一律绕过系统代理**（否则 `HTTP_PROXY` 会把 `127.0.0.1` 的请求也送进代理并回 502） | **实现补充** |
| 7 | §6 的数据结构可变性约定 | 细化为三条并写入 `state.py` 模块头：**循环级字段值语义**（只由 `machine.transition` 落账）、**任务节点记录级可变**（各模块按职责就地更新）、`model_calls` 由运行时结算（只有它知道调用是否真发生） | **实现细化** |

**另一处值得记录的实现选择**：`context.build_messages` 把「执行档」印进系统提示词，
而执行档在 `LoopDeps.mode` 上、不在 `LoopDeps.config` 上（`config` 只装阈值）。
最初写错属性名后，异常被一处防御性的 `except: return False` 吞掉，
表现为「什么事件都没有就停机」——现已去掉该 except，让异常经 `agent.py` 转成可见的 error 事件。

**验证结果**见 [`verification-report-state-loop.md`](./verification-report-state-loop.md)：
248 项测试全绿（19.6s），其中含从 `/api/chat/stream` 打进去的 HTTP 端到端链路。
