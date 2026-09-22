# -*- coding: utf-8 -*-
"""
state_loop 的状态与领域对象定义。

本模块**只有数据与纯计算，没有任何 IO**：不读文件、不起进程、不调模型。
这让「转移表 + 状态」能够完全离线单测（见 tests/test_state_loop_machine.py）。

可变性约定（实现时必须遵守，否则可测性与可观测性会一起塌掉）：

1. **循环级字段是值语义**——`phase`、`halt_reason`、计数器、`pending_calls`、
   `observations`、`resume_state`。它们只由 `machine.transition()` 通过
   `dataclasses.replace()` 生成新对象，相位推进永远只走那一个函数。
2. **记录级字段是可变记录**——`TaskGraph` 里的 `TaskNode`、`failure_counts` 的计数。
   它们由阶段执行器在其职责范围内就地更新（scheduler 改 status、verify 改 attempts、
   repair 记签名）。理由：这些字段是「对已发生事实的记账」，做成值语义只会让代码更绕。
3. **并发边界**——单次用户回合内主循环是单线程的。`delegate` 是唯一并发点，它的做法是
   每个子循环各建独立 `LoopState`，`asyncio.gather` 结束后由父循环**按 task id 排序**
   逐个写回，因此不存在「并发修改同一对象」或「并发 append 同一 list」的情况。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "Phase", "HaltReason", "ExecMode", "Effect", "TaskStatus", "DecisionKind",
    "FailureKind", "Acceptance", "TaskNode", "TaskGraph", "TaskResult",
    "Failure", "ToolCall", "ToolOutcome", "Decision", "LoopState",
    "PermissionKind", "EFFECT_LABELS",
]


# ======================================================================
# 枚举
# ======================================================================
class Phase(str, Enum):
    """主循环状态。除 halt 外都可由 transition() 互相推进。"""

    intake = "intake"          # 校验本轮消息、装载历史、初始化预算
    decompose = "decompose"    # 需求拆解 + 项目规划（唯一产出 TaskGraph 的状态）
    schedule = "schedule"      # 选下一个可运行任务 / 委派 / 结束
    perceive = "perceive"      # 仓库感知（强制读码）
    decide = "decide"          # 针对当前任务产出下一步动作
    authorize = "authorize"    # 权限判定：放行 / 拒绝 / 暂停等人
    act = "act"                # 执行已授权批次
    verify = "verify"          # 自测验证（三重门，零模型调用）
    repair = "repair"          # 失败分类与修复策略选择
    delegate = "delegate"      # 子代理并行
    compress = "compress"      # 上下文压缩（插桩，不是业务步）
    halt = "halt"              # 终态

    @property
    def is_terminal(self) -> bool:
        return self is Phase.halt

    def __str__(self) -> str:  # 让日志里出现 phase=decide 而不是 Phase.decide
        return self.value


class HaltReason(str, Enum):
    completed = "completed"
    blocked = "blocked"
    stalled = "stalled"
    budget = "budget"
    cancelled = "cancelled"

    def __str__(self) -> str:
        return self.value


class ExecMode(str, Enum):
    """
    执行档（对齐工作台四档选择器）。

    `full_access` 只减少 L4 确认次数，**不关掉 L5**：工作区越界、解释器
    `-c` 逃逸、未授权本机路径一律拒绝。本机读写仍走 WorkspaceSecurity /
    AccessBroker。
    """

    plan = "plan"
    confirm_writes = "confirm_writes"
    auto_workspace = "auto_workspace"
    full_access = "full_access"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: Any) -> "ExecMode":
        """容错解析。未知取值回落到默认档，不抛异常（配置错误不该让服务起不来）。"""
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for item in cls:
            if item.value == text:
                return item
        return cls.auto_workspace


class Effect(IntEnum):
    """
    副作用等级。**看副作用，不看工具名像不像只读**（架构文档第 6.2 节）。
    数值可比较：`effect <= 档位上限` 就是放行条件。
    """

    L0_READ = 0          # 不改盘、不起进程
    L1_WRITE = 1         # 改工作区文件
    L2_SANDBOX = 2       # 无文件副作用的计算沙箱
    L3_COMMAND = 3       # 工作区子进程
    L4_MCP_MUTATE = 4    # 可能改外部系统
    L5_ESCAPE = 5        # 工作区外 / 解释器逃逸 → 一律拒绝

    @property
    def code(self) -> str:
        return f"L{int(self)}"


EFFECT_LABELS: Dict[Effect, str] = {
    Effect.L0_READ: "只读，不改磁盘不起进程",
    Effect.L1_WRITE: "写工作区文件",
    Effect.L2_SANDBOX: "计算沙箱，无文件副作用",
    Effect.L3_COMMAND: "工作区子进程",
    Effect.L4_MCP_MUTATE: "可能改外部系统",
    Effect.L5_ESCAPE: "越出工作区或解释器逃逸",
}


class TaskStatus(str, Enum):
    pending = "pending"    # 依赖未满足
    ready = "ready"        # 可运行
    running = "running"
    done = "done"
    blocked = "blocked"
    skipped = "skipped"

    def __str__(self) -> str:
        return self.value

    @property
    def is_finished(self) -> bool:
        return self in (TaskStatus.done, TaskStatus.skipped)


class DecisionKind(str, Enum):
    """模型在 decide / decompose 状态唯一能提交的动作种类。"""

    tool_batch = "tool_batch"   # 提交一批工具调用
    mark_done = "mark_done"     # 声明任务完成（会被机器改写为进 verify）
    replan = "replan"           # 要求重做任务图
    ask_user = "ask_user"       # 需要用户澄清 → halt(blocked)

    def __str__(self) -> str:
        return self.value


class FailureKind(str, Enum):
    """
    失败分类学（架构文档第 8.2 节）。每一项对应一种「下一步该做什么」，
    而不是一种「错误长什么样」——分类的目的是驱动修复，不是打标签。
    """

    ModelProtocolError = "ModelProtocolError"   # 没有合法的 submit_decision
    ArgError = "ArgError"                       # 缺参 / JSON 损坏 / schema 不符
    NotFound = "NotFound"                       # 文件或工具不存在
    PathDenied = "PathDenied"                   # 路径越界
    PermissionDenied = "PermissionDenied"       # 用户拒绝或执行档不允许
    PatchConflict = "PatchConflict"             # edit_file 的 old_string 匹配 0 次或多次
    CommandFailed = "CommandFailed"             # 进程退出码非 0
    Timeout = "Timeout"                         # 命令或沙箱超时
    SandboxViolation = "SandboxViolation"       # 受限沙箱拒绝
    TestFailed = "TestFailed"                   # 验收命令失败
    DiagnosticError = "DiagnosticError"         # ruff/pyright 报新增 error
    ScopeViolation = "ScopeViolation"           # 写入路径不在任务 scope 内
    McpError = "McpError"                       # MCP 传输/调用失败
    ToolError = "ToolError"                     # 未归类的工具失败（兜底：宁可标为待修也不要漏判）
    LoopStall = "LoopStall"                     # 同一失败签名重复
    BudgetExhausted = "BudgetExhausted"         # 预算耗尽
    Cancelled = "Cancelled"                     # 客户端断开

    def __str__(self) -> str:
        return self.value


class PermissionKind(str, Enum):
    """authorize 暂停时请求确认的对象种类。"""

    plan = "plan"     # 确认整张任务图（ExecMode.plan）
    tools = "tools"   # 确认一批工具调用

    def __str__(self) -> str:
        return self.value


# ======================================================================
# 任务图
# ======================================================================
@dataclass
class Acceptance:
    """
    任务的「完成定义」。

    关键约束：**验收命令来自这里，不来自模型在 verify 时的临场发挥**。
    模型在 decompose 声明一次即冻结，之后无法在 decide 里改掉它。
    """

    commands: Tuple[Tuple[str, ...], ...] = ()       # argv 数组的数组，绝不是 shell 字符串
    diagnostics: Tuple[str, ...] = ("ruff", "pyright")
    must_read: Tuple[str, ...] = ()                   # perceive 至少读到这些相对路径

    def __post_init__(self) -> None:
        # 容忍从 JSON 反序列化出来的 list，统一成 tuple，便于哈希与比较
        self.commands = tuple(tuple(c) for c in self.commands)
        self.diagnostics = tuple(self.diagnostics)
        self.must_read = tuple(self.must_read)


@dataclass
class TaskNode:
    """
    一个可执行、可验收、可并行的子任务。

    字段没有装饰性的：每个都承担一个控制职责。
        scope      安全边界 + 并行判据（scope 前缀不相交才允许并行）
        acceptance 完成定义
        perceived  强制感知门闩（False 时不允许产生写操作）
        attempts   修复预算（只在 verify 失败或回滚时 +1）
    """

    id: str
    title: str
    detail: str = ""
    depends_on: Tuple[str, ...] = ()
    scope: Tuple[str, ...] = ()
    acceptance: Acceptance = field(default_factory=Acceptance)
    status: TaskStatus = TaskStatus.pending
    perceived: bool = False
    attempts: int = 0
    max_attempts: int = 3
    checkpoint_id: Optional[str] = None
    last_failure: Optional["Failure"] = None

    def __post_init__(self) -> None:
        self.depends_on = tuple(self.depends_on)
        self.scope = tuple(self.scope)

    def card(self, limit: int = 2000) -> str:
        """任务卡：给模型看的当前任务正文（图钉区块，压缩时不动它）。"""
        lines = [f"[{self.id}] {self.title}"]
        if self.detail:
            lines.append(self.detail[:limit])
        if self.scope:
            lines.append(f"可写范围: {', '.join(self.scope)}")
        if self.acceptance.commands:
            cmds = "; ".join(" ".join(c) for c in self.acceptance.commands)
            lines.append(f"验收命令: {cmds}")
        if self.acceptance.must_read:
            lines.append(f"必读文件: {', '.join(self.acceptance.must_read)}")
        lines.append(f"状态: {self.status}｜已尝试 {self.attempts}/{self.max_attempts}")
        if self.last_failure is not None:
            lines.append(f"上次失败: {self.last_failure.kind}｜{self.last_failure.message[:400]}")
        return "\n".join(lines)


@dataclass
class TaskGraph:
    """
    一次 decompose 的产物。计划是**一个对象**，因此重规划就是整张替换，
    不会出现「新任务与旧任务混在一起」的中间态。
    """

    summary: str = ""
    tasks: Dict[str, TaskNode] = field(default_factory=dict)
    order: List[str] = field(default_factory=list)   # 拓扑序，稳定，用于 UI 与合并排序

    def __post_init__(self) -> None:
        if not self.order:
            self.order = list(self.tasks.keys())

    def nodes(self) -> List[TaskNode]:
        """按拓扑序返回任务节点（跳过已不存在的 id）。"""
        return [self.tasks[tid] for tid in self.order if tid in self.tasks]

    def get(self, task_id: Optional[str]) -> Optional[TaskNode]:
        if not task_id:
            return None
        return self.tasks.get(task_id)

    def ready_ids(self) -> List[str]:
        """依赖已全部完成、且自身未结束的任务，按拓扑序。"""
        out: List[str] = []
        for node in self.nodes():
            if node.status is not TaskStatus.ready:
                continue
            if all(self.tasks.get(d) is None or self.tasks[d].status.is_finished
                   for d in node.depends_on):
                out.append(node.id)
        return out

    def promote_ready(self) -> List[str]:
        """
        把依赖已满足的 pending 任务提升为 ready。返回本次被提升的 id。
        在任务标 done 之后调用（新解锁的并行度来自此处）。
        """
        promoted: List[str] = []
        for node in self.nodes():
            if node.status is not TaskStatus.pending:
                continue
            if all(self.tasks.get(d) is None or self.tasks[d].status.is_finished
                   for d in node.depends_on):
                node.status = TaskStatus.ready
                promoted.append(node.id)
        return promoted

    def all_finished(self) -> bool:
        return all(node.status.is_finished for node in self.nodes())

    def blocked_ids(self) -> List[str]:
        return [n.id for n in self.nodes() if n.status is TaskStatus.blocked]

    def unfinished_ids(self) -> List[str]:
        return [n.id for n in self.nodes() if not n.status.is_finished]

    def progress_line(self) -> str:
        """一行进度摘要，用于收尾文本与事件。"""
        total = len(self.tasks)
        done = sum(1 for n in self.nodes() if n.status is TaskStatus.done)
        return f"{done}/{total} 完成"


@dataclass
class TaskResult:
    """子循环交回父循环的结构化结果。**不携带子观察全文**（否则隔离就没意义了）。"""

    task_id: str
    status: TaskStatus
    changed_paths: Tuple[str, ...] = ()
    verify_summary: str = ""
    failure: Optional["Failure"] = None
    halt_reason: Optional[HaltReason] = None

    def one_line(self) -> str:
        head = f"[{self.task_id}] {self.status}"
        if self.changed_paths:
            head += f"｜改动 {len(self.changed_paths)} 个文件"
        if self.failure is not None:
            head += f"｜{self.failure.kind}"
        return head


# ======================================================================
# 失败与调用
# ======================================================================
@dataclass
class Failure:
    """
    一个**可分类**的失败。

    `signature` 是归一化后的稳定标识，用来做停滞检测；`message` 只用于给人/模型看。
    两者绝不能混用：message 里的行号每次都会变，拿它做计数等于没有计数。
    """

    kind: str
    message: str
    signature: str = ""
    tool: Optional[str] = None
    retryable: bool = True
    artifacts: Dict[str, Any] = field(default_factory=dict)

    def one_line(self) -> str:
        tail = self.message.strip().splitlines()
        head = tail[0][:200] if tail else ""
        return f"{self.kind}" + (f"（{self.tool}）" if self.tool else "") + (f": {head}" if head else "")


@dataclass
class ToolCall:
    """一次待执行的工具调用。`effect` 由 permissions 计算，模型自报的值一律丢弃。"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    effect: Effect = Effect.L0_READ

    def one_line(self) -> str:
        args = ", ".join(f"{k}={_short(v)}" for k, v in list(self.arguments.items())[:3])
        return f"{self.name}({args})"


@dataclass
class ToolOutcome:
    """
    一次工具执行的结果。相比 tools.base.ToolResult 多了一层**失败分类**与**耗时**，
    是进入观察环的对象。
    """

    call_id: str
    name: str
    ok: bool
    text: str
    failure: Optional[Failure] = None
    artifacts: Dict[str, Any] = field(default_factory=dict)
    effect: Effect = Effect.L0_READ
    duration_ms: int = 0

    @property
    def exit_code(self) -> Optional[int]:
        value = self.artifacts.get("exit_code")
        return int(value) if isinstance(value, int) else None

    @property
    def tail(self) -> str:
        """命令类结果的尾部（错误栈在这里）。非命令结果返回空串。"""
        value = self.artifacts.get("tail")
        return value if isinstance(value, str) else ""

    def summary_line(self) -> str:
        mark = "ok" if self.ok else f"failed:{self.failure.kind if self.failure else 'Error'}"
        first = (self.text or "").strip().splitlines()
        head = (first[0][:80] if first else "")
        return f"{self.name} → {mark}｜{head}"


@dataclass
class Decision:
    """
    模型在一次 decide / decompose 里能提交的唯一对象。

    注意 `Decision` 里**没有 effect 字段**：副作用的判定权在 permissions.classify，
    模型无法通过自报 effect 来给自己提权。
    """

    kind: DecisionKind = DecisionKind.tool_batch
    calls: Tuple[ToolCall, ...] = ()
    note: str = ""
    replan_reason: str = ""
    question: str = ""

    def __post_init__(self) -> None:
        self.calls = tuple(self.calls)

    def describe(self) -> str:
        """给前端 thought 事件用的一行描述。"""
        if self.kind is DecisionKind.tool_batch:
            names = ", ".join(c.name for c in self.calls)
            return f"{self.note or '执行工具'}｜{names}".strip("｜")
        if self.kind is DecisionKind.mark_done:
            return self.note or "声明任务完成"
        if self.kind is DecisionKind.replan:
            return f"重新规划：{self.replan_reason or self.note}"
        return f"需要澄清：{self.question or self.note}"


# ======================================================================
# 主循环状态
# ======================================================================
@dataclass
class LoopState:
    """
    一次用户消息的可持久化状态。

    字段全部可 `dataclasses.asdict`，为第二期落盘/崩溃续跑预留（第一期只活在内存）。
    """

    phase: Phase = Phase.intake
    mode: ExecMode = ExecMode.auto_workspace
    user_text: str = ""

    graph: Optional[TaskGraph] = None
    current_task_id: Optional[str] = None

    observations: Tuple[ToolOutcome, ...] = ()      # 环，压缩时折叠旧项
    pending_calls: Tuple[ToolCall, ...] = ()        # 已授权、待执行的批次

    halt_reason: Optional[HaltReason] = None
    resume_state: Optional[Phase] = None            # compress 用：压缩后回到哪个状态
    permission_request_id: Optional[str] = None
    permission_kind: Optional[PermissionKind] = None

    step_count: int = 0
    model_calls: int = 0
    replan_count: int = 0
    protocol_retries: int = 0
    read_streak: int = 0                            # 本任务连续纯只读批次数
    depth: int = 0                                  # 子循环深度（0=主循环）
    chars_used: int = 0

    failure_counts: Dict[str, int] = field(default_factory=dict)  # signature → 次数
    notes: List[str] = field(default_factory=list)                # 机器记录的里程碑，用于收尾摘要
    verify_log: List[Dict[str, Any]] = field(default_factory=list)
    child_results: Tuple[TaskResult, ...] = ()

    # ---------------- 便捷访问 ----------------
    @property
    def tasks(self) -> Dict[str, TaskNode]:
        return self.graph.tasks if self.graph else {}

    @property
    def current_task(self) -> Optional[TaskNode]:
        return self.graph.get(self.current_task_id) if self.graph else None

    @property
    def is_halted(self) -> bool:
        return self.phase.is_terminal

    @property
    def active_task_is_applicable(self) -> bool:
        node = self.current_task
        return node is not None and not node.status.is_finished

    def recent_observations(self, limit: int = 6) -> Tuple[ToolOutcome, ...]:
        return self.observations[-limit:] if limit > 0 else ()

    def note(self, text: str) -> None:
        """记一条机器里程碑（进收尾摘要，不进模型上下文）。"""
        if text:
            self.notes.append(text)

    def to_jsonable(self) -> Dict[str, Any]:
        """可 JSON 序列化的快照（枚举转字符串，bytes 不参与）。"""
        def convert(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return {k: convert(v) for k, v in dataclasses.asdict(value).items()}
            if isinstance(value, dict):
                return {str(k): convert(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(v) for v in value]
            return value

        return convert(self)


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"
