# 本地文件系统与系统数据能力改造方案

> 状态：**切片 1–5 已落地**；**循环集成（切片 6）已补齐**：`native_react` 执行前过 `agents/gate.py` 审批闸，`planner.accept` 支持 `root:rel` scope，感知/验收/租约对外部根生效。落地记录见文末第 9、10 节。
> 目标：在不动 Agent 循环编排的前提下，为本机编码智能体补齐「工作区之外的本地文件系统访问」与「系统数据读取 / 受控系统操作」两类能力。
> 关联：[`agent-runtime-architecture.md`](./agent-runtime-architecture.md)（控制面权威文档，本文严格服从其四条不变量 I1–I4）。

---

## 0. 一句话结论

**不要把安全边界打穿，要把它从「一个根」扩成「一组可授权的根」。**

新增能力全部桥接成 `tools.base.Tool`，进同一个 `ToolRegistry`，走同一套事件与同一套权限判定；
控制流一个状态都不加。

---

## 1. 现状诊断：到底缺什么

一个常见误解是「项目没有文件能力」。实际上文件能力是有的，问题在**边界的形状**：

| 层 | 现状 | 缺口 |
| --- | --- | --- |
| 路径门闩 | `tools/workspace.py` 的 `WorkspaceSecurity`，单根 `server.workspace_root`（默认 `./workspaces/demo`） | 工作区之外的任何路径一律 `PathTraversalError`。`C:\Users\dell\Desktop`、D 盘、其他项目目录全部不可见 |
| 文件原语 | `list_dir` / `read_file` / `write_file` / `edit_file`，硬绑定单个 `WorkspaceSecurity` 实例 | 没有跨根概念，没有元信息（`stat`）、没有移动/复制/删除 |
| 检索 | `file_search`（关键词/正则）、`rag_search`（向量），同样锁在工作区 | 无法在本地其他目录检索 |
| 终端 | `tools/shell.py` 的 L3 `run_command`，argv 白名单只有 `python/pytest/ruff/pyright/pip`，`cwd` 锁工作区 | 不能查系统状态、不能起白名单外进程 |
| 沙箱 | `RestrictedPythonExecutor`（L2），禁 import、禁文件/网络 | **故意**如此，不该改造成系统通道 |
| **系统数据** | **完全空白** | 无进程、CPU/内存/磁盘、网卡、环境变量、已装软件、服务、剪贴板、窗口等任何只读事实源 |
| **系统操作** | **完全空白** | 无打开文件、通知、截图、启动应用 |
| 授权交互 | 无审批 UI；`state_loop` 设计里 L4 / `confirm_writes` 一律**失败关闭** | 新增的高权限能力若无审批通道，等于「装了但不能用」 |

所以改造要补齐的是三件事：**跨根文件访问、系统数据只读面、受控系统动作**，外加一条**审批通道**——没有它，前三者只能以「默认关闭」的姿态存在。

---

## 2. 架构思路

### 2.1 三条不变量（违反任意一条即为设计事故）

| # | 不变量 | 含义 |
| --- | --- | --- |
| **N1** | 控制流零改动 | 不为新能力新增 `state_loop` 状态、不加框架、不加第二套循环。新能力 = 新 `Tool` 实现 |
| **N2** | 单一门闩 | 工作区外的任何路径/系统查询，都必须经过新增的 `AccessBroker`。**不允许任何工具自己 `open()` 或 `subprocess` 拼系统命令** |
| **N3** | 默认关闭 | 未显式授权的根不注册对应工具。不是「注册了再拒绝」，而是「根本不存在」 |

N3 尤其重要，理由见 4.1：当前默认框架 `native_react` **没有 `authorize` 状态**，运行期拦截无处落地，安全必须前置到注册期。

### 2.2 门闩的升级：从单根到多根 + 作用域分级

`WorkspaceSecurity` **不改动**，它继续做工作区根的实现。在其上新增 `AccessBroker`：

```python
@dataclass(frozen=True)
class AccessRoot:
    name: str              # 模型可见的逻辑名，如 "desktop"
    path: Path             # 已 resolve 的绝对路径
    read: bool = True
    write: bool = False    # 默认只读；写权限必须显式打开
    tier: str = "granted"  # workspace | granted | system

class AccessBroker:
    def resolve(self, root: str, relpath: str, *, need_write: bool) -> Path
    def roots_view(self) -> list[dict]        # 给 fs_roots 工具用
    def audit(self, root, path, action, nbytes, decision) -> None
```text

**关键点**：工具的 `path` 参数不再是「相对工作区的路径」，而是「`root` + 相对该根的路径」二元组。
这样模型的每一步动作都显式声明作用域，**作用域不能靠路径猜**——这正是现有单根设计里不存在、而多根场景下最容易出事的地方。

### 2.3 硬编码拒绝清单（不可配置放宽）

无论授权了哪个根，以下一律拒绝，且不提供配置项去关掉：

```text
C:\Windows, C:\Program Files, C:\Program Files (x86), C:\System32, C:\ProgramData
**/.ssh/**, **/.aws/**, **/.gnupg/**, **/.kube/
**/AppData/Roaming/** 下的凭据类（凭据管理器、浏览器 profile、Cookies/Login Data）
**/.env, **/.npmrc, **/.netrc, **/*.pem, **/id_rsa, **/*.pfx, **/*.key
其它用户的 C:\Users\<他人>\**（非当前用户）
```

理由：这是**个人电脑上的 Web 服务**，威胁模型是「本机单人使用、只监听 127.0.0.1」。
真正的风险不是外部攻击者，而是模型自己「顺手读到不该读的东西并写进上下文/日志」。

---

## 3. 功能模块清单

### 3.1 模块一：`tools/fs_access.py` —— 访问网关（新增，唯一入口）

| 项 | 内容 |
| --- | --- |
| 职责 | 根注册、路径解析、作用域判定、拒绝清单、限流、审计 |
| 关键方法 | `resolve(root, relpath, need_write)` / `roots_view()` / `audit()` |
| 失败语义 | 一律抛 `PathTraversalError`（越界）/ `PermissionError`（根只读或命中拒绝清单）；由 `ToolRegistry.call` 转成 `error_type` |
| 复用 | 编码检测直接复用 `WorkspaceSecurity._detect_encoding`（UTF-8 → GBK → latin-1 三级回退） |

### 3.2 模块二：本地文件工具组（新增，前缀 `fs_`）

现有四个工作区工具**保持不动**（模型已熟悉其语义与提示词）。新增一组专用于工作区外：

| 工具 | 级 | 说明 |
| --- | --- | --- |
| `fs_roots` | L0 | 列出已授权根及其读写权限。模型第一步该调它 |
| `fs_list` | L0 | 列目录，条目上限 2000，分页游标 |
| `fs_read` | L0 | 读文件，上限 512KB / 2000 行；二进制只返回元信息 |
| `fs_search` | L0 | 文件名 glob 与内容正则，结果上限 50，深度上限 4 |
| `fs_stat` | L0 | 大小/时间/类型/是否符号链接 |
| `fs_write` | L1 | 仅 writable 根；写前 before-bytes 快照 |
| `fs_edit` | L1 | 精确串替换（要求唯一匹配），语义与 `edit_file` 对齐 |
| `fs_copy` / `fs_move` | L1 | 目标必须在 writable 根内 |
| `fs_delete` | **L4** | **永不真删** → 移入隔离目录 `.workbuddy/.trash/<ts>/`，需确认 |

### 3.3 模块三：系统数据只读层（新增，前缀 `sys_`，全部 L0）

只读事实源，不产生副作用，因此可放心自动放行：

| 工具 | 依赖 | 返回 |
| --- | --- | --- |
| `sys_overview` | psutil | OS 版本、CPU 型号/核数/当前占用、内存、开机时长 |
| `sys_processes` | psutil | Top N 进程（pid/名/CPU/内存/用户），**cmdline 截断 200 字符并脱敏** |
| `sys_disks` | psutil | 各分区总量/已用/剩余/文件系统类型 |
| `sys_network` | psutil | 网卡地址、IO 计数、连接数（连接详情默认关，慢且敏感） |
| `sys_env` | os.environ | **白名单 + 脱敏**：匹配 KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/SESSION 的一律屏蔽 |
| `sys_battery` | psutil | 电量/是否充电（笔记本） |
| `sys_hardware` | psutil | CPU 频率、物理核/逻辑核、总内存 |
| `sys_services` | pywin32（可选） | Windows 服务名/状态/启动类型 |
| `sys_installed_apps` | 注册表或卸载项（可选） | 已安装软件名/版本/厂商 |
| `sys_clipboard` | pywin32 或 `powershell Get-Clipboard` | 剪贴板文本（**默认关闭**，隐私敏感） |
| `sys_windows` | pywin32 或 `powershell` | 顶层窗口标题与进程（默认关闭） |

**依赖策略**：`psutil` 是**可选依赖**。缺失时整组系统工具**不注册**，打印一行警告，对话继续——与现有 RAG 初始化失败的降级哲学完全一致（可选增强不得成为主链路单点故障）。

**脱敏是硬要求**：系统数据里最容易泄露的是进程命令行（常含 `--api-key xxx`）和环境变量。
宁可少给字段，不可事后补救——日志和上下文一旦写进去就收不回。

### 3.4 模块四：受控系统动作（新增，全部需确认）

| 工具 | 级 | 说明 |
| --- | --- | --- |
| `sys_open_path` | L4 | 用系统默认程序打开文件或目录（`os.startfile` / `open` / `xdg-open`） |
| `sys_notify` | L2 | 桌面通知（标题 + 正文），无文件系统副作用 |
| `sys_screenshot` | **L4** | 截图落盘到工作区，**默认关闭**，隐私极敏感 |
| `sys_launch` | L4 | 启动白名单内应用（配置 `system.allow_apps`），不接受任意命令行 |

**一律不接受自由命令行**。这是从 `tools/shell.py` 继承的纪律：`shell=False` + argv 白名单，不把安全裁决交给 7B 模型。

### 3.5 模块五：审批通道与审计（新增，本方案的「能否启用」开关）

| 项 | 内容 |
| --- | --- |
| SSE 事件 | `permission_request`（含 root、路径、动作、影响字节数）+ 前端确认卡片 |
| 回复接口 | `POST /api/permissions/{id}`（`allow` / `deny`） |
| 失败关闭 | 无 UI 或未在 60s 内答复 → **视为拒绝**，与 `state_loop` 现有设计一致 |
| 审计日志 | `data/logs/fs_access.jsonl` 追加：`ts, root, path, action, bytes, decision, root_tier` |

**为什么这一块不能省**：Zcode 作为桌面应用天然拥有全盘权限，靠**权限提示 UI** 让人兜底；
我们是 127.0.0.1 上的 Web 服务，扩张本地能力时，审批 UI 是**唯一等价的兜底**。
没有它，高权限能力只能永远关着。

### 3.6 模块六：技能与配置（新增）

`skills/local_system.py`：

```python
SKILL = Skill(
    name="local_system",
    title="本地文件与系统信息",
    summary="在用户显式授权的目录内读取文件，并查询本机系统状态。",
    tools=("fs_roots", "fs_list", "fs_read", "fs_search", "fs_stat",
           "sys_overview", "sys_processes", "sys_disks"),
    guidance="...",
)
```text

技能只声明工具元组 + 提示词，**不包执行器**（符合 `skills/base.py` 的既有约定）。
写入类（`fs_write`/`fs_edit`/`fs_delete`）与动作类单独放 `local_system_write` 技能，**默认不启用**。

`config.yaml` 新增：

```yaml
local_access:
  enabled: false                 # 默认关闭；不开则不注册任何 fs_* 工具
  roots: []                      # 例: - {name: desktop, path: "C:/Users/dell/Desktop", read: true, write: false}
  max_read_bytes: 524288
  max_list_entries: 2000
  max_search_results: 50
  trash_dir: "./data/.trash"
  audit_log: "./data/logs/fs_access.jsonl"

system:
  enabled: false                 # 默认关闭；psutil 缺失时自动降级
  allow_actions: []              # 例: ["notify"]
  expose: ["overview", "processes", "disks", "network", "env"]
```

---

## 4. 与现有 Agent 循环编排的集成方式

### 4.1 当前默认框架 `native_react` 期间

`native_react` 是提示词驱动的 ReAct，**没有 `authorize` 状态**。这意味着运行期拦截无处落地。
因此安全必须**前置到注册期**：

```text
build_runtime():
    registry 注册全部工具
        ↓
    _select_tools(registry, tools_for_skills(skills))   # 技能白名单过滤
        ↓
    【新增】local_access.enabled == false → fs_* 工具不注册
    【新增】root.write == false        → fs_write/fs_edit 不注册
```

即：**模型看不到 = 调不到**，而不是「调了再拒绝」。
这是本方案在 `native_react` 下唯一可靠的拦截点，必须写死在装配路径里。

### 4.2 `state_loop` 落地之后

扩展 `permissions.classify`，但**不新增状态**：

| 现有状态 | 改造点 |
| --- | --- |
| `intake` | 不变 |
| `decompose` | `TaskNode.scope` 校验从「工作区内」扩为「已授权根内」；跨根任务图仍需逐项校验 |
| `perceive` | 只读批次可跨根（`fs_read` / `fs_search`），但 `must_read` 必须显式带 root |
| `authorize` | 判定改为二维矩阵（见下）；外部写入进 `NeedsConfirm` |
| `act` | 不变；`FileJournal` 扩展为支持外部根的 before-bytes 快照 |
| `verify` | **不变**。验收命令仍只在工作区跑——外部目录不参与验收语义 |
| `repair` | 新增 `failure.kind` 复用 `PathDenied` / `PermissionDenied`，不新增种类 |
| `delegate` | **外部根写入不进子代理**：写路径租约只对 workspace 根生效，跨根任务强制串行 |

**权限判定从一维变二维**：

|  | workspace 根 | granted 根（只读） | granted 根（可写） | system 只读 | system 动作 |
| --- | --- | --- | --- | --- | --- |
| 读 | 自动 | 自动 | 自动 | 自动 | — |
| 写 | 自动 | 拒绝 | **确认** | — | — |
| 起进程 | 白名单内自动 | — | — | — | **确认** |
| 删除 | 隔离目录 | 拒绝 | **确认** | — | — |

矩阵是**纯函数、可单测**——这与「L0–L5 与 argv 白名单必须是确定性代码」是同一条纪律。
危险度（动作）与作用域（根）正交，比把它们揉进一个 0–9 的级别更容易审计。

### 4.3 工具数量与上下文预算

架构文档 10.3 已定：**工具数 > 32 时改用工具目录**（只给 name + 一行描述，调用前再校验完整 schema）。
本方案新增约 20 个工具，加上现有约 8 个，逼近阈值。因此：

- 默认只启用 8 个（`local_system` 技能），其余靠配置按需打开；
- 打开后超过阈值，`decide` 阶段自动切 catalog 模式，复用既有机制，不另发明。

---

## 5. 关键实现要点（按踩坑概率排序）

1. **Junction / 符号链接逃逸**。Windows 的 junction 无需特权即可创建，能指向任意目录。
   `Path.resolve()` 在 3.8+ 会解析大部分 reparse point，但仍需在 `AccessBroker` 里**对最终路径再判一次归属**，
   不能只判输入路径。这是现有 `WorkspaceSecurity` 已处理的老问题，跨根后风险面放大。

2. **大小写不敏感与 8.3 短名**。Windows 上 `C:\Progra~1` 与 `C:\Program Files` 等价。
   比较前统一 `os.path.normcase()`，拒绝清单匹配用规范化后的路径，否则能被短名绕过。

3. **UNC 与 `\\?\` 前缀**。解析前先剥离 `\\?\` 再判定归属，否则字符串比较形同虚设。

4. **删除走隔离目录，不要真删**。Windows 下没有可靠的「送回收站」通道（COM 不可用、`Remove-Item` 可能被拦截且静默失败）。
   统一移入 `.workbuddy/.trash/<timestamp>/`，记录原路径，提供 `fs_restore`。**永不硬删除**。

5. **二进制与超大文件保护**。读前先看前 8KB：含 `\x00` 即判定二进制，只返回「类型/大小/修改时间」不返回内容。
   超过 `max_read_bytes` 截断并明确告知模型「已截断」，不要让它以为看到了全貌。

6. **编码**。中文 Windows 上文件名与文件内容都可能是 GBK。
   读内容复用 `WorkspaceSecurity._detect_encoding`；调 PowerShell 时固定 `-OutputFormat Text` 并强制 UTF-8，
   否则中文输出变乱码，模型在修复阶段会读不懂错误栈。

7. **系统调用的超时与缓存**。`psutil.cpu_percent(interval=None)` 首次返回无意义值；`net_connections()` 在 Windows 上较慢。
   统一包一层：`interval=0.5` 采样、结果缓存 5 秒、整体超时 10 秒。

8. **权限异常不得崩循环**。`PermissionError` / `OSError` 一律转成 `ToolResult.failure(..., "PathDenied", ...)`，
   交给上层按既有分类学处理。工具层「任何异常都不抛出」是 `ToolRegistry.call` 的既有契约。

9. **审计必须落在门闩内部**，不能靠各工具自觉调用。
   `AccessBroker.resolve()` 成功时即记账，保证「没经过 broker 就等于没访问」这条性质成立。

10. **`sys_env` 与进程命令行一律脱敏**。正则屏蔽 `*KEY*/*TOKEN*/*SECRET*/*PASSWORD*/*CREDENTIAL*/*SESSION*`；
    cmdline 截断 200 字符。上下文和日志一旦写入就不可收回。

11. **工作区外写入也要 FileJournal 快照**，否则 `repair` 的回滚对外部根失效。
    `restore` 前必须再走一次 `broker.resolve` 复查，防止检查点被换路径误写。

12. **不暴露成本进程为 MCP Server**（沿用架构 10.4 的决定）。本方案只增消费侧能力，不开能力出口。

---

## 6. 落地切片

| 切片 | 内容 | 验收 |
| --- | --- | --- |
| **1** | `tools/fs_access.py`：`AccessRoot` + `AccessBroker`，含拒绝清单、junction 复查、审计 | 越界路径被拒；junction 指向外部被拒；短名绕过被拒；拒绝清单命中被拒 |
| **2** | `fs_roots` / `fs_list` / `fs_read` / `fs_search` / `fs_stat`（只读全组）+ `skills/local_system.py` | 配置关闭时工具不注册；大文件截断生效；二进制只读元信息 |
| **3** | `tools/system_tools.py`：`sys_*` 只读组 + psutil 可选依赖降级 | 卸载 psutil 后启动正常、工具不注册、对话继续；env/cmdline 脱敏生效 |
| **4** | 写入组 `fs_write` / `fs_edit` / `fs_copy` / `fs_move` / `fs_delete` + FileJournal 扩展 | 只读根写入被拒；删除进隔离目录可还原；scope 外写入触发回滚 |
| **5** | 审批通道：SSE `permission_request` + `POST /api/permissions/{id}` + 前端确认卡片 | 无答复 60s 视为拒绝；审批通过后才执行；审计日志条数正确 |

切片 1–3 完成即可安全启用只读能力；**切片 5 完成前，写入与系统动作保持关闭**。

---

## 7. 风险与不做的事

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| **模型选错工具** | 本地 7B 面对 20 个新工具会乱调、反复 `fs_list` 空转 | 默认只开 8 个；`state_loop` 的 `ReadStreakCap`（连续 3 批纯只读即中断）自动兜底 |
| **隐私泄露** | 系统数据含敏感信息，且会进上下文与日志 | 脱敏 + 白名单 + 默认关闭高风险项（剪贴板、窗口、截图） |
| **新增依赖** | psutil / pywin32 引入安装成本 | 全部可选，缺失降级；`requirements.txt` 列为 extras |
| **审批 UI 缺失** | 高权限能力实际关闭 | 切片 5 之前不启用写入与动作；不是「降级成自动放行」 |
| **外部写入不可回滚** | 用户自己的文档目录被改坏代价高 | 默认只读；写入根逐个显式开启；before-bytes 快照 + 隔离目录 |

**明确不做**：不做注册表写入；不做服务/进程启停（除白名单应用启动）；不做跨用户目录访问；
不做任意命令行执行（沿用 argv 白名单）；不做远程/手机控制；不把本进程暴露为 MCP Server。

---

## 8. 与 Zcode 的对照（借鉴什么，不借鉴什么）

> 材料来源：`zcode.z.ai` 官方文档（任务与文件管理、Agent 框架页）与公开评测。涉内部实现的一律标注推断。

| 维度 | Zcode（公开口径） | 本方案 | 结论 |
| --- | --- | --- | --- |
| 产品形态 | 桌面 ADE，天然拥有全盘文件系统 + 终端 + Git + 浏览器 | 127.0.0.1 Web 服务 | **形态不同，权限起点不同**：Zcode 生而有全盘权限，我们必须逐根授权 |
| 权限兜底 | 权限提示 UI 展示将要执行的命令/文件变更/工具动作 | 补齐 `permission_request` + 确认卡片 | **直接借鉴**，这是本方案能否启用的关键 |
| 执行档 | 5 档，含 Full Access | 保持 3 档，**不提供完全访问**（沿用架构 6.4） | **不借鉴**。威胁模型不同：本机单人服务，关掉 L5 收益为负 |
| 只读子代理 | Explore 子代理「严格只读」硬保证 | granted 根默认 `write: false`，等价于根级只读保证 | **思想借鉴**，落地在根配置而非角色 |
| Goal 状态持久化 | 系统级存储，关会话可续跑 | 不做 | 沿用架构既有决定（第二期） |
| MCP / 插件 / 远程 | OAuth、插件市场、手机远控、SSH/Docker | 不做 | 有意收窄，与架构第 14 节一致 |

一句话：**Zcode 靠「桌面应用天然有权限 + 权限提示 UI 兜底」，我们靠「逐根显式授权 + 审计 + 审批通道」。**
路径不同，目标一致：让能力扩张的同时，人对「动了我什么」始终有知情权。

---

## 9. 落地记录（2026-09-21）

切片 1–5 已全部实现并通过测试（全量 316 项，含原有 248 项零回归）。

| 切片 | 落地物 | 与方案的偏差 |
| --- | --- | --- |
| 1 | `tools/fs_access.py`：AccessRoot / AccessBroker / 硬拒绝清单 / 短名展开 / junction 复查 / 审计 / 限流；`tests/test_fs_access.py` 17 项 | 无；junction 与 8.3 短名用例在真实 Windows 环境验证 |
| 2 | `tools/fs_tools.py`（fs_roots/list/read/search/stat）+ `skills/local_system.py` / `local_system_write.py`；装配注册门 `agents/agent.py::_build_local_access`；`tests/test_fs_tools.py` 20 项 | fs 写入组代码随切片 2 一次落地、按切片 4 验收；技能名单在装配期动态并入（agent.yaml 不需要预声明） |
| 3 | `tools/system_tools.py`（sys_* 只读 8 类 + 动作 3 类）+ 脱敏纯函数；`tests/test_system_tools.py` 12 项 | sys_services 需 pywin32、sys_clipboard/sys_windows 默认不开放（expose 白名单控制）；本机无 psutil，降级路径实测 |
| 4 | `permissions.py`：TOOL_EFFECTS 登记 + `external_write_targets`；`journal.py`：外部根快照与回滚复查；`outcome.py`：L4 写前记账；`tests/test_journal_external.py` 6 项 | fs 写入登记为 L4（确认通道），矩阵「可写根写=确认、只读根写=门闩拒绝」落在 L4 + `resolve(need_write)` 两层 |
| 5 | `app/api/routes_permissions.py`（PermissionHub + POST /api/permissions/{id} + GET /pending）+ 前端确认卡片（app.js/style.css）+ 主循环确认分支修正；`tests/test_permission_hub.py` 6 项 + `tests/test_permission_flow.py` 4 项 + HTTP 冒烟 3 项 | 修正了一处原实现缺陷：tools 类确认请求此前会在 authorize 无限循环发 NEEDS_CONFIRM（现已走决议通道，无审批处理器时失败关闭） |

启用方式（默认全部关闭）：

```yaml
local_access:
  enabled: true
  roots:
    - {name: desktop, path: "C:/Users/<用户名>/Desktop", read: true, write: false}
system:
  enabled: true            # 需 pip install psutil
```text

写入类工具仅在存在 `write: true` 的根时注册，执行前会通过审批卡片请求人工确认（60 秒未答复 = 拒绝）。

## 10. 循环集成（切片 6，2026-09-21）

切片 1–5 把工具和审批通道装上了，但默认框架 `native_react` 仍直接 `execute`，`planner.accept` 仍只认工作区路径。本切片把能力接到控制面，不加新状态。

| 落点 | 内容 |
| --- | --- |
| `agents/gate.py` | 框架共用审批闸。`authorize_tool` 供 native_react 异步等人；`execute_gated_sync` 供 autogen/llamaindex/crewai 失败关闭 |
| `agents/native_react.py` | 工具执行前过闸；L4 发 `permission_request`，与 state_loop 共用 PermissionHub |
| `planner.accept(..., broker=)` | scope / must_read 允许 `root:rel`；未启用 local_access 时拒绝该形态 |
| `permissions.path_in_scope` | 工作区路径与 `root:rel` 正交匹配；外部根写入越出任务 scope → ScopeViolation |
| `scheduler.perceive_batch` | `desktop:notes.md` 走 `fs_read`；纯外部根任务检索优先 `fs_search`；外部根任务不进并行委派 |
| `verify` | 纯外部根任务跳过工作区验收命令（记 skipped），诊断只跑工作区触碰文件 |
| `FileJournal.has_writes` | 外部根写入同样算写过盘 |
| 装配 | `_build_local_access` 把 workspace 传给截图工具（修 NameError） |
