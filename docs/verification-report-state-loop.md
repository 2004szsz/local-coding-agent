# state_loop 全链路验证报告

日期：2026-09-21　｜　范围：`agents/state_loop/`、`tools/shell.py`、`tools/mcp/`、契约层改动
方法：`python -m unittest discover -s tests -p "test_*.py"`（**248 项，全部通过，19.6s**）

---

## 0. 结论

**功能链路已经完整实现并验证通过**：从 HTTP 请求进来到收尾文本出去，中间每一次状态转移、
每一次权限判定、每一次工具执行与验收，都有可复现的证据。

但有**一条重要的未验证项**，必须先说清楚：

> **`state_loop` 未与真实模型做过联调。** 本次验证里，模型侧全部由
> `tests/fake_llm_server.py`（本地假 OpenAI 兼容服务）承担。本机 Ollama 未运行
> （探测结果：连接被拒绝），因此「真实 7B 模型能否稳定产出 `submit_decision`」
> 这件事**没有被实测**。

据此给出两条可执行结论：

| 项目 | 状态 |
| --- | --- |
| 控制面（状态机、权限、快照、验收、失败分类） | **已完成并验证** |
| 工具层（文件 / 检索 / 沙箱 / 工作区命令 / MCP） | **已完成并验证** |
| 与真实模型的协议遵从性 | **未验证**（本机无模型） |
| 默认框架 | **仍是 `native_react`**（刻意保守，见 §7） |

---

## 1. 验证范围与方法

| 层次 | 手段 | 覆盖 | 不覆盖 |
| --- | --- | --- | --- |
| L1 纯逻辑 | 单元测试，无 IO | 转移表与守卫、停止条件、权限分级、写前快照、任务图校验、失败分类与签名归一化、工具命名 | —— |
| L2 组件 | 单元测试，**真起子进程** | 工作区命令执行器（真实 spawn、真实超时强杀）；MCP 协议客户端（真实 stdio 子进程） | 真实第三方 MCP server |
| L3 状态机端到端 | 可注入决策器 + 真实工具与命令 | 改文件 → 验收失败 → 修复 → 再验收 → 通过 的完整闭环；回滚；越界拦截；空转打断；协议违反 | 真实模型 |
| L4 HTTP 端到端 | 真 FastAPI + SSE + 假模型服务 | 配置加载 → 运行时装配 → 框架选择 → SSE 事件形状 → 会话落盘 → 收尾文本 | 前端页面交互 |
| L5 环境探测 | 探针脚本 | Ollama 可达性、诊断工具是否在 PATH、代理环境变量、MCP 配置降级 | —— |

**为什么要有 L4**：L3 只驱动 `run_loop`，证明不了「装配对不对、事件发不发得出去、
配置读没读对」。实际就靠它抓到了一个 L3 永远抓不到的 bug（见 §6 P0-3）。

---

## 2. 结果总览

| 测试模块 | 用例数 | 结果 |
| --- | --- | --- |
| `test_state_loop_machine`（转移表 / 守卫 / 停止条件 / 限额解析） | 53 | OK |
| `test_mcp_bridge`（MCP 配置 / 协议 / 桥接 / 管理 / 权限接入） | 36 | OK |
| `test_state_loop_permissions`（分级 / 批次授权 / scope） | 30 | OK |
| `test_state_loop_planner`（任务图校验 / 短路判定） | 24 | OK |
| `test_state_loop_repair`（分类 / 签名 / 策略） | 24 | OK |
| `test_state_loop_e2e`（状态机端到端，4 类场景 + 真实协议路径） | 24 | OK |
| `test_state_loop_shell`（命令执行器，真实子进程） | 22 | OK |
| `test_state_loop_journal`（快照与回滚） | 15 | OK |
| `test_http_end_to_end`（HTTP + SSE 全链路） | 6 | OK |
| `test_agent_spec` / `test_calculator` / `test_history` / `test_search`（既有） | 11 | OK |
| `test_source_hygiene`（源码卫生，新增） | 3 | OK |
| **合计** | **248** | **OK** |

---

## 3. 稳定性：正常与异常场景

### 3.1 正常场景（全部有对应用例）

| 场景 | 机器行为 |
| --- | --- |
| 一句可执行的需求 | 建任务图 → 强制读码 → 决策 → 授权 → 执行 → 验收通过 → 任务 done → 完成 |
| 闲聊 / 单步问答 | **不建任务图**，一次无工具回复后收尾（`intake` 的守卫分流） |
| 多条互不相干的任务 | 写路径租约不相交时并行委派；相交则退回串行 |
| 有依赖的任务 | 无依赖的先跑，前驱完成后由 `promote_ready` 解锁后继 |
| 只读探索 | 连续 3 批纯只读即判定空转并打断 |

### 3.2 异常场景（这是本次验证的重点）

| 异常输入 / 环境 | 机器行为 | 是否停机 | 依据用例 |
| --- | --- | --- | --- |
| 空消息 / 全空白 | 拒收，HTTP 400 | 是（`blocked`） | `test_06_empty_message_is_rejected` |
| 不存在的会话 id | HTTP 404 | 是 | `test_05_unknown_session_is_rejected` |
| 模型回散文不调函数 | 记协议违反 → 重试 1 次 → 仍失败则停机 | 是（`blocked`）且**只重试一次** | `test_protocol_violation_is_reported_not_swallowed`、`test_protocol_violation_retries_then_halts` |
| 模型编造工具名 | 协议层拦下（`name` 用 enum 锁定），不计入执行 | 否（可重试） | `test_decision_invalid_retries_once_then_halts` |
| 提交的 JSON 非法 | 归类 `ArgError`，带原文回到决策 | 否 | `test_keyword_fallback_for_plain_value_error` |
| 写入任务范围之外 | `authorize` 当场拒绝，**文件根本不落盘** | 否（首犯回滚，再犯重规划） | `test_out_of_scope_write_is_rejected`、`test_write_out_of_scope_is_denied_with_scope_violation` |
| 路径越出工作区（`../`、绝对路径、符号链接） | 拒绝（L5），无确认通道 | 是（任务 `blocked`） | `test_write_outside_workspace_is_denied`、`test_escape_is_denied_in_every_mode` |
| 危险命令（`rm` / `curl` / `bash` / `git`） | 拒绝（argv0 不在白名单） | 是（任务 `blocked`） | `test_unknown_argv0_is_denied` |
| `python -c "任意代码"` | 拒绝（解释器逃逸） | 是 | `test_python_c_is_denied` |
| `python -m 非白名单模块` | 拒绝；但 `python -m pytest` **放行** | 是 / —— | `test_python_m_unknown_module_is_denied`、`test_python_m_pytest_is_allowed` |
| `pip install` / `pip uninstall` | 拒绝（不改解释器环境） | 是 | `test_pip_install_is_denied_but_show_is_allowed` |
| `edit_file` 定位串 0 次匹配 | 归类 `PatchConflict` → **回滚到检查点** → 重新读文件 | 否 | `test_patch_conflict_triggers_rollback` |
| 命令超时（30s 睡眠，1s 限时） | 强杀进程树，记 `Timeout` | 否 | `test_timeout_kills_process` |
| 命令退出码非 0 | 记 `CommandFailed`，观察里保留**输出尾部** | 否 | `test_failure_reports_exit_code_and_tail` |
| 验收命令不通过 | 消耗一次尝试额度 → 增量修复 | 否 | `test_failed_verify_then_incremental_fix` |
| 同一个错误重复出现 | **签名归一化后计数，2 次即停机**（不等额度耗尽） | 是（`stalled`） | `test_stalled_halts`、`test_line_numbers_are_normalized_away` |
| 只读空转（连续 3 批） | 判定空转 → 打断；再犯即停机 | 是（`stalled`） | `test_read_only_spin_is_interrupted` |
| 高权限操作且无审批界面 | **失败关闭**：按拒绝处理，绝不自动放行 | 是（任务 `blocked`） | `test_auto_workspace_allows_commands_but_pauses_on_mcp_mutation` |
| MCP server 启动失败 | 该 server 标 `down`，其余照常，对话继续 | 否 | `test_dead_server_does_not_break_the_others` |
| MCP 配置非法（名字/传输/缺字段） | **逐条跳过并告警**，不影响其他 server 与对话 | 否 | `test_load_servers_degrades_entry_by_entry` |
| MCP 在响应前插通知 | 按 id 匹配响应，不被通知带偏 | 否 | `test_notification_is_not_mistaken_for_response` |
| 客户端断开 | 取消驱动任务，**已写入的文件保留不自动回滚** | 是（`cancelled`） | `test_force_halt_clears_transient_fields`（语义）＋ `routes_chat` 的 finally 落盘 |
| 步数 / 模型调用到顶 | 停机，已完成任务保留，未完成任务保持磁盘现状 | 是（`budget`） | `test_step_cap_wins_over_budget`、`test_model_call_cap_triggers_step_cap` |
| 框架名写错（如残留 `langgraph`） | 启动期明确报错并给出替代方案，回退 `native_react` | —— | `test_framework_prefers_yaml_over_config_default` |

**稳定性小结**：所有异常都收敛到 5 种可枚举终态之一（`completed` / `blocked` / `stalled` /
`budget` / `cancelled`），**没有一条路径会让循环无限跑或静默失败**。
这一点由转移表的「分组最后一条必须是无守卫兜底行」自检守着（`validate_table()`）。

---

## 4. 准确性：各环节输出与预期的一致性

| 环节 | 预期 | 实测 |
| --- | --- | --- |
| 需求拆解 | 产出可执行 / 可验收 / 可并行的任务图 | 9 条校验全部生效：0 任务、>8 任务、标题重复、scope 空、scope 越界、无验收命令、依赖成环、自依赖、未知依赖、argv0 越界 —— 逐条拒绝且**不执行任何工具** |
| 项目规划 | 依赖拓扑稳定、并行度可推导 | 同一份计划两次生成的拓扑序一致；无依赖任务为 `ready` |
| 仓库感知 | 强制先读后写；只读批次固定 | `must_read` 逐个读；描述含文件名时不触发冗余检索；`perceived` 为假时不允许写 |
| 决策 | 只接受类型化决策；协议违反是一等事件 | 消息里实测带上**任务卡 + 任务图进度 + 观察 + 工具目录 + 执行档 + 要求**；`tool_choice` 实测为强制形式（非 `auto`） |
| 权限判定 | L0–L5 唯一判定点；模型不能提权 | 模型自报的 `effect` 被覆盖（`test_model_reported_effect_is_overwritten`）；批次策略取**最严格**而非数值最大 |
| 执行 | L0 并发、L1+ 串行、顺序确定 | 结果按下标回填，顺序与入参一致 |
| 命令输出 | 错误栈（尾部）必须保留 | 200 行输出实测保留「前 20 + 后 60 行」，中段省略；`artifacts.tail` 另存尾 80 行 |
| 验收 | 退出码是唯一事实；证据不足要说 `skipped` | 范围门 → 命令门 → 诊断门顺序执行；`ruff`/`pyright` 不在 PATH 时记 `skipped` 而**不算通过** |
| 迭代 | 同签名 2 次停机；尝试额度只被验收消耗 | 行号/耗时/临时路径/ANSI 全部归一化（4 类噪声各有用例）；一次验收失败恰好消耗 1 次额度 |
| 收尾 | 模型失败时用机器摘要兜底 | 模型无响应 → `fallback_summary` 输出进度 + 最后验收 + 停机原因 |

---

## 5. 适配性：输入、环境与边界

### 5.1 输入维度
空消息、纯空白、闲聊、中文长句、编造工具名、参数写成字符串、JSON 损坏、参数类型错误、
越界相对路径、绝对路径、反斜杠路径、`python.exe` 后缀、超长工具输出、200 行命令输出 —— 均有对应用例。

### 5.2 环境维度（本机实测）

| 环境事实 | 影响 | 处理 |
| --- | --- | --- |
| **系统设了 `HTTP_PROXY=http://127.0.0.1:10470`** | httpx 默认会把 `127.0.0.1:11434` 也送进代理 → **502**，表现为「Ollama 开着却连不上」 | 新增 `tools/net.py`：本机地址一律绕过代理（远程地址仍尊重代理）。**已修** |
| **Ollama 未运行**（连接被拒） | 真实模型无法联调 | 用假 OpenAI 兼容服务完成全链路验证；真实模型联调**未做** |
| `ruff` / `pyright` 不在 PATH | 诊断门无工具可用 | 记 `skipped`，不假装通过（设计如此） |
| `pytest` 未安装 | 不能跑 pytest | 测试全用 `unittest`；README 已注明 |
| 无 git、无 venv | 不能假设 git 回滚 | 回滚用字节快照，不调 git |
| Windows 文件锁 | 持有句柄时文件删不掉 | `Runtime.shutdown()` 关闭 SQLite / MCP / 向量库。**已修** |
| Bash 工具 PATH 时常整体失效 | 无法用 shell 排查 | 全部改用 PowerShell 执行与取证 |

### 5.3 边界条件
0 个任务、超过 8 个任务、循环依赖、单任务、连续只读 3 批、命令超时与夹紧到上限、
文件不存在、补丁匹配 0 次、写一个原先不存在的文件（回滚应删除而非写空）、
空授权批次（不变量检查）、终态后再转移（抛非法转移）。
以上逐条有对应用例。

---

## 6. 发现的问题

### 已修复

| # | 严重度 | 问题 | 根因 / 证据 | 修复 |
| --- | --- | --- | --- | --- |
| P0-1 | **高** | `agents/state_loop/runtime.py` 被写成 **3.2 万个 NUL 字节**，整个包无法导入 | 一次 `Edit` 报告成功却写坏了文件。Python 报错点在 `from . import runtime` 那一行，**完全看不出坏的是 runtime.py** | 重建文件；新增 `tests/test_source_hygiene.py`（查空字节/零宽字符/语法/关键模块可导入） |
| P0-2 | **高** | `decide` 阶段发出的 payload 用了 `calls` 键，而转移表只认 `pending_calls` → **authorize 拿到空批次，编辑从未执行**；表现为「一条工具调用记录都没有、验收却一直失败」 | 静默 no-op，且 `act_ok_readonly` 对空批次恒真，把错误伪装成「只读」 | 改用正确键名；在 `_phase_act` 加**空批次不变量检查**（显式报错而非继续） |
| P0-3 | **高** | `context.build_messages` 写成 `deps.config.mode`（`config` 是 `LoopLimits`，无 `mode`）→ `AttributeError` | **被我自己的防御性 `except Exception: return False` 吞掉**，表现为「什么事件都没有就停机」。离线 e2e 因走假决策器、从不构建消息而全绿 | 去掉该 except（让异常经 agent 层变成可见 error 事件）；新增 `RealProtocolPathTests` 强制走真实协议路径 |
| P1-1 | 中 | 本机地址被系统代理拦截 → 502 | `HTTP_PROXY` 存在时 httpx 默认 `trust_env=True` | 新增 `tools/net.py`，本机地址绕过代理（`api_client` / `vector_store` / `mcp.client` 三处接入） |
| P1-2 | 中 | **`python -m pytest` 被自己的逃逸规则误杀** —— 一刀切禁 `-m` 会顺手禁掉最常用的跑测试写法 | 设计文档示例本身用的就是 `python -m pytest`，规则与示例矛盾 | `-c` 与 `-m` 区别对待：`-m` 按模块白名单放行（pytest/unittest/py_compile/compileall/json.tool/pip） |
| P1-3 | 中 | `HistoryStore` 有 `close()` 却从没人调用 → Windows 下 `history.db` 句柄不释放，临时目录删不掉 | 装配层只看重「能起来」，没管退出路径 | `Runtime.shutdown()` 统一关闭 MCP / SQLite / 向量库 |
| P1-4 | 低 | MCP 子进程的管道未关闭（`ResourceWarning`） | 只关了 stdin | 先 terminate 再关三根管道（顺序有讲究：反了会在 Windows 上对着仍有读者的管道 close） |
| P2-1 | 低 | `argv[0]` 允许传路径 → 白名单只约束了「文件名」，可在工作区放同名可执行文件绕过 | 只做了 `basename` 归一化 | 新增 `argv0_violation()`：**拒绝含路径分隔符的 argv0** |
| P2-2 | 低 | MCP server 名被静默转小写，配置与运行时出现两个名字 | 校验前做了 `.lower()` | 改为严格拒绝，提示语里说明「不要用大写或连字符」 |

### 未修复 / 遗留（**这些是已知边界，不是遗漏**）

| # | 项 | 说明 | 影响 |
| --- | --- | --- | --- |
| R-1 | **真实模型未联调** | 本机 Ollama 未运行，`submit_decision` 的协议遵从性未实测 | 决定默认框架能否切到 `state_loop`（见 §7） |
| R-2 | `LoopState` 不持久化 | 进程退出即丢任务图与观察环 | 长任务无法崩溃续跑（对比 Zcode 的弱项） |
| R-3 | 无审批 UI | `confirm_writes` 档与 L4 一律失败关闭 | 高权限能力实际是关着的；接入 `confirm_handler` 即可打开 |
| R-4 | MCP `sse` 传输未实现 | 只实现 `stdio` 与 `streamable_http`；配了 `sse` 会在配置解析期被明确拒绝并告警 | 少数只提供 SSE 的 server 用不了 |
| R-5 | 前端未适配新事件 | `plan` / `task` / `verify` / `rollback` / `delegate` / `permission_request` 是增量事件，旧前端会忽略；同时另发了一条 `thought` 作摘要 | 能看到内容，但看不到任务卡与验收卡片 |
| R-6 | 诊断门在本机恒 `skipped` | `ruff`/`pyright` 未安装 | 本机验证时静态检查这一门实际没生效 |
| R-7 | 子代理只在任务图维度并行 | 无后台委派、无 per-agent 模型 | 并行度受「写租约不相交」约束 |

### 验证方法本身的一次误判（记录在案）
按模块单独跑时，脚本一度报告 `test_state_loop_e2e` FAILED。复核后确认是**验证脚本的假阳性**：
PowerShell 的 `-match` 默认不区分大小写，而 asyncio 调试输出里的协程名
`test_failed_verify_then_incremental_fix` 含 "failed"。实测 `exit_code=0`、
大小写敏感的 `FAILED` 出现 **0** 次。**测试本身始终是通过的。**

---

## 7. 结论与建议

### 7.1 链路是否完成
**是。** 用户给定的六步链路（需求拆解 → 项目规划 → 读代码 → 执行命令 → 检验自测 → 迭代）
在状态机里逐环落地，且每一环都有机器守卫，无法靠提示词跳过。七大能力模块全部实现并通过验证。

### 7.2 为什么默认框架仍是 `native_react`
这是**刻意的决定，不是遗漏**：

- `state_loop` 依赖模型的**原生 Function Calling**，且要求它服从强制的 `tool_choice`。
  本机没有可用的模型，这件事无法验证。
- 一旦默认切过去而模型不服从协议，用户看到的是「每轮都 blocked」，
  而 `native_react` 至少有文本动作兜底。
- 切换成本极低：改 `agents/agent.yaml` 一行，或临时设 `AGENT_FRAMEWORK=state_loop`。

### 7.3 建议的下一步（按价值排序）
1. **启动 Ollama 后做一次真实联调**：跑 `AGENT_FRAMEWORK=state_loop python run.py`，
   给一个「改文件 + 跑测试」的小需求。**这是解锁默认切换的唯一前置条件。**
2. 装 `ruff`，让诊断门真正生效（否则本机的「自测验证」只有命令门在起作用）。
3. 需要长任务续跑 → 落盘 `LoopState`（结构已按可序列化设计，不需要改动字段）。
4. 需要审批能力 → 注入 `LoopDeps.confirm_handler`，暂停点与事件都已就绪。
5. 前端适配新事件（任务卡 / 验收卡片）。
