# -*- coding: utf-8 -*-
"""
状态机：转移表、守卫与停止条件。

**本模块是纯函数模块**——不读文件、不起进程、不调模型，只有查表与计数。
`transition()` 是相位推进的唯一入口：任何模块都不允许自己写 `state.phase = ...`。

三条纪律（违反任何一条，转移表就会退化成「模糊路由」）：

1. **同事件多规则时，最后一条必须是无守卫的兜底行。** 守卫全不过 → 抛
   `IllegalTransition`，绝不「跳到一个看起来合理的邻居状态」。`validate_table()` 会自检这一条。
2. **失败一律走表中写明的失败行**，不在守卫里做隐式降级。
3. **终态之后禁止转移**（`halt` 不接受任何事件）。

关于「谁消费预算」的分工（刻意如此，不是疏漏）：
- 循环级预算（`step_count`、`protocol_retries`、`replan_count`）由本模块在转移时结算；
- 任务级预算（`TaskNode.attempts`）由 `verify` 模块结算——它才知道「这次验证算不算一次尝试」。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple

from .state import (
    HaltReason,
    LoopState,
    PermissionKind,
    Phase,
    TaskStatus,
)

__all__ = [
    "LoopLimits", "DEFAULT_LIMITS", "Event", "EV", "Rule", "TransitionView",
    "IllegalTransition", "transition", "interrupt_event", "validate_table",
    "table_rows", "is_model_phase",
]


# ======================================================================
# 配置
# ======================================================================
@dataclass(frozen=True)
class LoopLimits:
    """
    循环阈值。定义在机器模块里，因为**转移表的守卫与停止条件消费的是同一组阈值**，
    分成两处会出现「守卫用的上限和停机用的上限不是同一个值」这种难查的错。
    """

    max_steps: int = 48                 # 默认 max_iterations * 4
    max_model_calls: int = 12           # 默认 max_iterations
    max_replan: int = 2
    protocol_retries: int = 1           # 协议违反的重试次数
    max_attempts: int = 3               # 单任务验证尝试上限
    read_streak_cap: int = 3            # 连续纯只读批次上限
    context_char_budget: int = 24000
    soft_budget_ratio: float = 0.70
    hard_budget_ratio: float = 0.92
    max_tasks: int = 8
    max_command_seconds: int = 60
    command_timeout_ceiling: int = 120
    delegate_parallel: int = 3
    delegate_budget_ratio: float = 0.40
    delegate_budget_ceiling: int = 8000
    max_depth: int = 1
    tail_lines: int = 80                # 命令输出保尾行数


DEFAULT_LIMITS = LoopLimits()


# ======================================================================
# 事件
# ======================================================================
class EV:
    """机器事件名。模型的自由文本永远不会出现在这里。"""

    # intake
    TURN_ACCEPTED = "turn_accepted"
    TURN_REJECTED = "turn_rejected"
    # decompose
    PLAN_ACCEPTED = "plan_accepted"
    PLAN_NEEDS_CONFIRM = "plan_needs_confirm"
    PLAN_INVALID = "plan_invalid"
    # schedule
    NEED_PERCEIVE = "need_perceive"
    NEED_DECIDE = "need_decide"
    CAN_DELEGATE = "can_delegate"
    ALL_DONE = "all_done"
    GRAPH_BLOCKED = "graph_blocked"
    # perceive
    PERCEIVE_OK = "perceive_ok"
    PERCEIVE_FAILED = "perceive_failed"
    # decide
    DECISION_ACCEPTED = "decision_accepted"
    DECISION_IS_VERIFY = "decision_is_verify"
    DECISION_REPLAN = "decision_replan"
    DECISION_ASK = "decision_ask"
    DECISION_INVALID = "decision_invalid"
    # authorize
    GRANTED = "granted"
    NEEDS_CONFIRM = "needs_confirm"
    CONFIRM_GRANTED = "confirm_granted"
    CONFIRM_REJECTED = "confirm_rejected"
    DENIED = "denied"
    # act
    ACT_OK = "act_ok"
    ACT_OK_READONLY = "act_ok_readonly"
    ACT_FAILED = "act_failed"
    # verify
    VERIFY_PASSED = "verify_passed"
    VERIFY_FAILED = "verify_failed"
    # repair
    REPAIR_EDIT = "repair_edit"
    REPAIR_ROLLBACK = "repair_rollback"
    REPAIR_REPLAN = "repair_replan"
    REPAIR_BLOCKED = "repair_blocked"
    STALLED = "stalled"
    # delegate
    CHILDREN_JOINED = "children_joined"
    CHILD_CRASHED = "child_crashed"
    # compress
    COMPRESSED = "compressed"
    BUDGET_HARD = "budget_hard"
    # 全局中断（由 interrupt_event 产出）
    BUDGET_SOFT = "budget_soft"
    CANCEL = "cancel"
    STEP_CAP = "step_cap"


@dataclass(frozen=True)
class Event:
    """机器事件。payload 只承载数据，不承载控制意图。"""

    name: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


#: 会消耗一次协议重试额度的事件
_PROTOCOL_EVENTS = frozenset({EV.PLAN_INVALID, EV.DECISION_INVALID})
#: 会消耗一次重规划额度的事件
_REPLAN_EVENTS = frozenset({EV.DECISION_REPLAN, EV.REPAIR_REPLAN})
#: 任务收尾事件：这些事件发生后当前任务焦点必须清空，避免下一个任务
#: 沿用上一个任务的 id 去读 scope / 写文件
_TASK_CLOSING_EVENTS = frozenset({
    EV.VERIFY_PASSED, EV.REPAIR_BLOCKED, EV.DECISION_REPLAN, EV.REPAIR_REPLAN, EV.ALL_DONE,
})
#: 需要模型参与的状态（用于统计 model_calls 的期望上限，实际计数由运行时负责）
_MODEL_PHASES = frozenset({Phase.decompose, Phase.decide})


def is_model_phase(phase: Phase) -> bool:
    return phase in _MODEL_PHASES


# ======================================================================
# 转移表
# ======================================================================
@dataclass(frozen=True)
class Rule:
    """
    一条转移规则。

    :param to:    目标状态；可为 Phase，也可为 `Callable[[LoopState], Phase]`（compress 回跳用）
    :param guard: 守卫 `(state, event, limits) -> bool`；None 表示无条件（兜底行）
    :param halt:  当 to 为 None（停机）时的终止原因
    :param note:  规则说明。既是文档也是测试断言的依据
    """

    to: "Phase | Callable[[LoopState], Phase] | None" = None
    guard: Optional[Callable[[LoopState, Event, LoopLimits], bool]] = None
    halt: Optional[HaltReason] = None
    note: str = ""


def _attempts_left(state: LoopState) -> bool:
    node = state.current_task
    return node is not None and node.attempts < node.max_attempts


def _replan_left(state: LoopState, limits: LoopLimits) -> bool:
    return state.replan_count < limits.max_replan


def _any_l0(state: LoopState) -> bool:
    return bool(state.pending_calls) and all(c.effect == 0 for c in state.pending_calls)


def _resume_target(state: LoopState) -> Phase:
    """compress 完成后回到进入压缩前的状态（缺省回 schedule，保证一定能继续）。"""
    target = state.resume_state
    if target is None or target is Phase.compress or target.is_terminal:
        return Phase.schedule
    return target


_TRANSITIONS: Dict[Tuple[Phase, str], Tuple[Rule, ...]] = {
    # ---------------- intake ----------------
    (Phase.intake, EV.TURN_ACCEPTED): (
        Rule(Phase.decompose,
             guard=lambda s, e, l: not e.get("trivial"),
             note="消息是一件事 → 进入拆解"),
        Rule(None, halt=HaltReason.completed,
             note="闲聊或单步问答 → 不建任务图，直接收尾（宁可不短路，也不漏拆）"),
    ),
    (Phase.intake, EV.TURN_REJECTED): (
        Rule(None, halt=HaltReason.blocked, note="空消息或历史损坏 → 停机"),
    ),

    # ---------------- decompose ----------------
    (Phase.decompose, EV.PLAN_ACCEPTED): (
        Rule(Phase.schedule, note="任务图合法且执行档非 plan → 开始调度"),
    ),
    (Phase.decompose, EV.PLAN_NEEDS_CONFIRM): (
        Rule(Phase.authorize, note="执行档为 plan → 先确认计划"),
    ),
    (Phase.decompose, EV.PLAN_INVALID): (
        Rule(Phase.decompose,
             guard=lambda s, e, l: s.protocol_retries < l.protocol_retries,
             note="协议不合法但还有重试额度 → 原地重试"),
        Rule(None, halt=HaltReason.blocked, note="重试耗尽 → 停机（不执行半张图）"),
    ),

    # ---------------- schedule ----------------
    (Phase.schedule, EV.NEED_PERCEIVE): (
        Rule(Phase.perceive, note="存在 ready 且未感知的任务 → 强制读码"),
    ),
    (Phase.schedule, EV.NEED_DECIDE): (
        Rule(Phase.decide, note="当前任务已感知、未完成、本步不委派"),
    ),
    (Phase.schedule, EV.CAN_DELEGATE): (
        Rule(Phase.delegate, note="≥2 个 ready、写租约不相交、深度为 0 → 并行"),
    ),
    (Phase.schedule, EV.ALL_DONE): (
        Rule(None, halt=HaltReason.completed, note="全部任务 done/skipped → 完成"),
    ),
    (Phase.schedule, EV.GRAPH_BLOCKED): (
        Rule(None, halt=HaltReason.blocked, note="无 ready 且存在 blocked/依赖未满足"),
    ),

    # ---------------- perceive ----------------
    (Phase.perceive, EV.PERCEIVE_OK): (
        Rule(Phase.decide, note="只读批次全部 ok/empty → 进入决策"),
    ),
    (Phase.perceive, EV.PERCEIVE_FAILED): (
        Rule(Phase.repair, guard=lambda s, e, l: _attempts_left(s),
             note="感知失败且还有尝试额度 → 修复"),
        Rule(None, halt=HaltReason.blocked, note="感知反复失败且额度耗尽"),
    ),

    # ---------------- decide ----------------
    (Phase.decide, EV.DECISION_ACCEPTED): (
        Rule(Phase.authorize, note="决策合法 → 权限判定"),
    ),
    (Phase.decide, EV.DECISION_IS_VERIFY): (
        Rule(Phase.verify, note="mark_done 或本任务已写过盘/跑过命令 → 必须验收"),
    ),
    (Phase.decide, EV.DECISION_REPLAN): (
        Rule(Phase.decompose, guard=lambda s, e, l: _replan_left(s, l),
             note="模型请求重规划且还有额度"),
        Rule(None, halt=HaltReason.blocked, note="重规划额度耗尽"),
    ),
    (Phase.decide, EV.DECISION_ASK): (
        Rule(None, halt=HaltReason.blocked, note="模型要求澄清 → 交还给用户"),
    ),
    (Phase.decide, EV.DECISION_INVALID): (
        Rule(Phase.decide,
             guard=lambda s, e, l: s.protocol_retries < l.protocol_retries,
             note="协议不合法但还有重试额度 → 原地重试"),
        Rule(None, halt=HaltReason.blocked, note="重试耗尽 → 停机"),
    ),

    # ---------------- authorize ----------------
    (Phase.authorize, EV.GRANTED): (
        Rule(Phase.act, note="全部调用在档位内且路径合法 → 执行"),
    ),
    (Phase.authorize, EV.NEEDS_CONFIRM): (
        Rule(Phase.authorize, note="含 L4 或档位要求确认 → 停在原地发请求"),
    ),
    (Phase.authorize, EV.CONFIRM_GRANTED): (
        Rule(Phase.schedule,
             guard=lambda s, e, l: s.permission_kind is PermissionKind.plan,
             note="计划被确认 → 开始调度"),
        Rule(Phase.act, note="工具批次被确认 → 执行"),
    ),
    (Phase.authorize, EV.CONFIRM_REJECTED): (
        Rule(Phase.repair, note="用户拒绝 → 交给修复分类（通常任务 blocked）"),
    ),
    (Phase.authorize, EV.DENIED): (
        Rule(Phase.repair, note="L5 或命令不在允许列表 → 交给修复分类"),
    ),

    # ---------------- act ----------------
    (Phase.act, EV.ACT_OK): (
        Rule(Phase.verify, note="批次无错且含写或命令 → 必须验收"),
    ),
    (Phase.act, EV.ACT_OK_READONLY): (
        Rule(Phase.decide,
             guard=lambda s, e, l: s.read_streak < l.read_streak_cap,
             note="纯只读批次且未触顶 → 继续决策"),
        Rule(Phase.repair,
             guard=lambda s, e, l: _attempts_left(s),
             note="连续只读触顶 → 视为空转，进入修复"),
        Rule(None, halt=HaltReason.stalled, note="空转且无尝试额度 → 停机"),
    ),
    (Phase.act, EV.ACT_FAILED): (
        Rule(Phase.repair, note="批次出现失败 → 交给修复分类"),
    ),

    # ---------------- verify ----------------
    (Phase.verify, EV.VERIFY_PASSED): (
        Rule(Phase.schedule, note="三重门通过 → 任务标 done，调度下一个"),
    ),
    (Phase.verify, EV.VERIFY_FAILED): (
        Rule(Phase.repair, guard=lambda s, e, l: _attempts_left(s),
             note="失败且还有尝试额度 → 增量修复"),
        Rule(Phase.decompose, guard=lambda s, e, l: _replan_left(s, l),
             note="尝试耗尽但还有重规划额度 → 重做任务图"),
        Rule(None, halt=HaltReason.blocked, note="尝试与重规划都耗尽 → 停机"),
    ),

    # ---------------- repair ----------------
    (Phase.repair, EV.REPAIR_EDIT): (
        Rule(Phase.decide, note="可增量修复 → 让模型提新补丁"),
    ),
    (Phase.repair, EV.REPAIR_ROLLBACK): (
        Rule(Phase.perceive, note="补丁冲突/越界 → 回滚后重新感知真实文件"),
    ),
    (Phase.repair, EV.REPAIR_REPLAN): (
        Rule(Phase.decompose, guard=lambda s, e, l: _replan_left(s, l),
             note="范围性错误 → 重做任务图"),
        Rule(None, halt=HaltReason.blocked, note="重规划额度耗尽"),
    ),
    (Phase.repair, EV.REPAIR_BLOCKED): (
        Rule(Phase.schedule, note="任务被阻塞 → 继续调度兄弟任务（部分成功好过全盘失败）"),
    ),
    (Phase.repair, EV.STALLED): (
        Rule(None, halt=HaltReason.stalled, note="同一失败签名重复 → 停机"),
    ),

    # ---------------- delegate ----------------
    (Phase.delegate, EV.CHILDREN_JOINED): (
        Rule(Phase.schedule, note="子循环全部返回 → 合并后继续调度"),
    ),
    (Phase.delegate, EV.CHILD_CRASHED): (
        Rule(Phase.schedule, note="子循环未捕获异常 → 该任务标 blocked，其余照常合并"),
    ),

    # ---------------- compress ----------------
    (Phase.compress, EV.COMPRESSED): (
        Rule(_resume_target, note="压缩后回到进入压缩前的状态（不消耗尝试预算）"),
    ),
    (Phase.compress, EV.BUDGET_HARD): (
        Rule(None, halt=HaltReason.budget, note="压缩后仍超硬阈值 → 停机"),
    ),
}

#: 全局中断规则。任何非终态都可被它们截断，因此不进上面那张二维表。
_GLOBAL_RULES: Dict[str, Rule] = {
    EV.CANCEL: Rule(None, halt=HaltReason.cancelled, note="客户端断开"),
    EV.STEP_CAP: Rule(None, halt=HaltReason.budget, note="步数或模型调用到顶"),
    EV.BUDGET_SOFT: Rule(Phase.compress, note="上下文超软阈值 → 插桩压缩"),
}


# ======================================================================
# 转移
# ======================================================================
class IllegalTransition(RuntimeError):
    """非法转移：当前状态不接受该事件，或该事件的守卫全部不通过。"""


def _select(state: LoopState, event: Event, limits: LoopLimits) -> Rule:
    """选出唯一一条匹配规则。全局中断优先，其次按表顺序取第一条守卫通过的。"""
    rule = _GLOBAL_RULES.get(event.name)
    if rule is not None:
        if event.name == EV.BUDGET_SOFT and state.phase is Phase.compress:
            raise IllegalTransition("已在 compress 状态，不能再次进入压缩")
        return rule

    group = _TRANSITIONS.get((state.phase, event.name))
    if not group:
        raise IllegalTransition(
            f"状态 {state.phase} 不接受事件 {event.name}（缺规则，请显式补表而不是加分支）"
        )
    for item in group:
        if item.guard is None or item.guard(state, event, limits):
            return item
    raise IllegalTransition(
        f"状态 {state.phase} 的事件 {event.name} 所有守卫都不通过"
        f"（分组最后一条必须是无守卫兜底行，见 validate_table）"
    )


def transition(state: LoopState, event: Event, limits: LoopLimits = DEFAULT_LIMITS) -> LoopState:
    """
    推进一个状态。返回**新对象**，不改入参。

    :raises IllegalTransition: 非法组合。上层应把它当编程错误（不是运行时噪声）
    """
    if state.phase.is_terminal:
        raise IllegalTransition(f"终态 {state.phase} 之后禁止再转移（事件 {event.name}）")

    rule = _select(state, event, limits)
    origin = state.phase
    target = rule.to(state) if callable(rule.to) else rule.to
    if target is None:
        target = origin  # 仅作占位，halt 分支会覆盖

    updates: Dict[str, Any] = {
        "step_count": state.step_count + 1,
        "phase": target,
    }

    # 循环级预算结算（任务级预算由 verify 负责）
    if event.name in _PROTOCOL_EVENTS:
        updates["protocol_retries"] = state.protocol_retries + 1
    if event.name in _REPLAN_EVENTS:
        updates["replan_count"] = state.replan_count + 1

    # 终态
    if rule.to is None:
        updates["phase"] = Phase.halt
        updates["halt_reason"] = rule.halt or HaltReason.blocked

    # 压缩插桩：进入时记住回跳点，完成时消费掉
    if target is Phase.compress and origin is not Phase.compress:
        updates["resume_state"] = origin
    elif origin is Phase.compress:
        updates["resume_state"] = None

    # 事件携带的记录型数据统一在这里落账。让阶段执行器做到「只产出事件、不改循环级字段」，
    # 是状态转移可单测、过程可回放的前提。
    payload = event.payload if isinstance(event.payload, dict) else {}
    graph = payload.get("graph")
    if graph is not None:
        updates["graph"] = graph
    outcomes = payload.get("outcomes")
    if payload.get("reset_observations"):
        # 回滚之后磁盘内容已经变了，旧观察会诱导模型对着不存在的文件打补丁。
        # 整环清空，只保留本次事件带回来的那条失败观察作为新的起点。
        updates["observations"] = tuple(outcomes or ())
    elif outcomes:
        updates["observations"] = tuple(state.observations) + tuple(outcomes)
    pending_calls = payload.get("pending_calls")
    if pending_calls is not None:
        updates["pending_calls"] = tuple(pending_calls)

    # 权限请求：登记进状态，让「停在 authorize 等人」成为可持久化的暂停点
    request_kind = payload.get("request_kind")
    if request_kind is not None:
        updates["permission_kind"] = PermissionKind(request_kind)
        updates["permission_request_id"] = payload.get("request_id")

    # 任务焦点：调度器在事件里指定，由这里记录——scheduler 因此不必自己改状态字段
    task_id = payload.get("task_id")
    if task_id:
        updates["current_task_id"] = task_id
    if event.name in _TASK_CLOSING_EVENTS:
        updates["current_task_id"] = None

    # 压缩会把测量结果带回来（chars_used 是观测量，不是控制量，但仍由转移统一落账）
    if "chars_used" in payload:
        updates["chars_used"] = int(payload["chars_used"])

    # 只读空转计数：由已授权批次的实际副作用决定，不看工具名
    if event.name == EV.GRANTED:
        updates["read_streak"] = state.read_streak + 1 if _any_l0(state) else 0

    # 离开 act/authorize 时清理瞬时字段，避免上一次的残留被下一次误读
    if origin is Phase.act and target is not Phase.act:
        updates["pending_calls"] = ()
    if origin is Phase.authorize and target is not Phase.authorize:
        updates["permission_request_id"] = None
        updates["permission_kind"] = None

    return _replace(state, updates)


def _replace(state: LoopState, updates: Dict[str, Any]) -> LoopState:
    """按 updates 生成新状态。transition 不改入参，靠它落地。"""
    return dataclasses.replace(state, **updates)


def interrupt_event(
    state: LoopState,
    limits: LoopLimits = DEFAULT_LIMITS,
    *,
    cancel_requested: bool = False,
    over_soft_budget: bool = False,
) -> Optional[Event]:
    """
    停止条件与全局中断的统一检查点。优先级：取消 > 硬性步数上限 > 软预算压缩。

    由主循环在每个 step 之前调用；这样「谁先停」是代码里写死的顺序，
    而不是散落在各阶段里的零散 if。
    """
    if state.phase.is_terminal:
        return None
    if cancel_requested:
        return Event(EV.CANCEL, {"reason": "客户端断开或显式取消"})
    if state.step_count >= limits.max_steps or state.model_calls >= limits.max_model_calls:
        return Event(EV.STEP_CAP, {
            "step_count": state.step_count,
            "model_calls": state.model_calls,
            "max_steps": limits.max_steps,
            "max_model_calls": limits.max_model_calls,
        })
    if over_soft_budget and state.phase is not Phase.compress:
        return Event(EV.BUDGET_SOFT, {"chars_used": state.chars_used})
    return None


# ======================================================================
# 自检与文档
# ======================================================================
def validate_table() -> list[str]:
    """
    转移表自检，返回问题列表（空列表 = 通过）。

    检查项：
      1. 每个 (状态, 事件) 分组的最后一条规则必须无守卫——否则守卫全不过时会变成
         「无规则可用」，把编程错误伪装成运行时噪声；
      2. halt 只能由 to=None 产生，不允许显式 `Rule(Phase.halt)`（否则会漏设 halt_reason）；
      3. 非 halt 状态不允许存在无法到达的孤岛（每个状态至少能被一个规则的 to 指向）。
    """
    problems: list[str] = []
    for (phase, event_name), group in _TRANSITIONS.items():
        if not group:
            problems.append(f"({phase}, {event_name}) 分组为空")
            continue
        if group[-1].guard is not None:
            problems.append(f"({phase}, {event_name}) 最后一条规则带守卫，缺少兜底行")
        for rule in group:
            if rule.to is Phase.halt:
                problems.append(f"({phase}, {event_name}) 显式指向 halt，应使用 to=None + halt=…")
            if rule.to is None and rule.halt is None:
                problems.append(f"({phase}, {event_name}) 停机行缺少 halt 原因")

    reachable = {Phase.intake}
    for group in _TRANSITIONS.values():
        for rule in group:
            if isinstance(rule.to, Phase):
                reachable.add(rule.to)
    for rule in _GLOBAL_RULES.values():
        if isinstance(rule.to, Phase):
            reachable.add(rule.to)
    for phase in Phase:
        if phase is Phase.halt:
            continue          # halt 只能由 to=None 到达，它没有「入边」是设计使然
        if phase not in reachable:
            problems.append(f"状态 {phase} 不可达")
    return problems


def force_halt(state: LoopState, reason: HaltReason, note: str = "") -> LoopState:
    """
    强制停机。**只用于两处**：捕获到编程错误（非法转移）时的兜底，以及外部取消。

    正常停机路径必须走 `transition()`——那里会落账 halt_reason、清理瞬时字段。
    这个方法存在的意义是：把「状态机自身出 bug」与「业务上的停机」分开，
    前者要能在日志里一眼看出来，而不是伪装成一次正常的 blocked。
    """
    if state.phase.is_terminal:
        return state
    updates: Dict[str, Any] = {
        "phase": Phase.halt,
        "halt_reason": reason,
        "pending_calls": (),
        "permission_request_id": None,
        "permission_kind": None,
    }
    return _replace(state, updates)


@dataclass(frozen=True)
class TransitionView:
    """一条转移的可读视图，供文档生成与测试枚举使用。"""

    phase: Phase
    event: str
    target: str
    guarded: bool
    halt: Optional[HaltReason]
    note: str


def table_rows() -> list[TransitionView]:
    """扁平化转移表。测试用它保证「每一行至少一个用例」可被追溯。"""
    rows: list[TransitionView] = []
    for (phase, event_name), group in _TRANSITIONS.items():
        for rule in group:
            if rule.to is None:
                target = "halt"
            elif callable(rule.to):
                target = "<dynamic>"
            else:
                target = str(rule.to)
            rows.append(TransitionView(phase, event_name, target,
                                       rule.guard is not None, rule.halt, rule.note))
    for event_name, rule in _GLOBAL_RULES.items():
        target = "halt" if rule.to is None else str(rule.to)
        rows.append(TransitionView(Phase.halt, event_name, target, rule.guard is not None,
                                   rule.halt, rule.note))
    return rows
