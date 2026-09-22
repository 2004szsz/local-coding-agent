# -*- coding: utf-8 -*-
"""
阶段执行器与主循环驱动。

分层职责（新加功能时先想清楚该放哪一层）：

    machine.py   纯状态转移 —— 谁都不许绕过，不碰 IO
    runtime.py   每个阶段「具体做什么」—— **唯一有 IO 的地方**
    agent.py     协议适配 —— 把事件转成 SSE，接上 BaseAgent

`LoopDeps` 把外部能力**显式**列出来，带来两个好处：
1. 单元测试只需替换 `decision_hook` 就能驱动完整循环，不必启动模型；
2. 新增能力时改一个 dataclass，而不是往各模块里塞全局变量。

关键约定：**阶段执行器只产出事件，不改循环级字段**。
`graph` / `observations` / `current_task_id` / `pending_calls` 都通过事件 payload
交给 `machine.transition` 落账——状态推进因此只有一条路径，回放与单测才成立。
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from tools.shell import WorkspaceCommandRunner
from tools.workspace import WorkspaceSecurity

from . import (
    context,
    decisions,
    outcome,
    permissions,
    planner,
    repair,
    scheduler,
    verify,
)
from . import delegate as delegate_mod
from .journal import FileJournal
from .machine import (
    DEFAULT_LIMITS,
    EV,
    Event,
    IllegalTransition,
    LoopLimits,
    force_halt,
    interrupt_event,
    transition,
)
from .state import (
    DecisionKind,
    Effect,
    ExecMode,
    Failure,
    FailureKind,
    HaltReason,
    LoopState,
    PermissionKind,
    Phase,
    TaskStatus,
    ToolCall,
    ToolOutcome,
)

__all__ = ["LoopDeps", "build_initial_state", "build_limits", "execute_phase",
           "run_loop", "fallback_summary"]

#: 可注入的决策器（测试用假实现，生产用模型）。签名：`(state, purpose) -> 原始参数`
DecisionHook = Callable[[LoopState, str], Awaitable[Any]]
#: 可注入的确认处理器：`(kind, calls, request_id) -> bool`。
#: request_id 由主循环生成（NEEDS_CONFIRM 事件里的那个），审批通道按它登记/决议。
#: 缺省（None）= 失败关闭。
ConfirmHandler = Callable[[PermissionKind, Tuple[ToolCall, ...], str], Awaitable[bool]]


# ======================================================================
# 依赖
# ======================================================================
@dataclass
class LoopDeps:
    llm: Any = None
    registry: Any = None
    workspace: Optional[WorkspaceSecurity] = None
    journal: FileJournal = field(default_factory=FileJournal)
    runner: Optional[WorkspaceCommandRunner] = None
    config: LoopLimits = DEFAULT_LIMITS
    mode: ExecMode = ExecMode.auto_workspace
    system_prompt: str = ""
    allowed_tools: Tuple[str, ...] = ()
    rag_available: bool = False
    decision_hook: Optional[DecisionHook] = None
    confirm_handler: Optional[ConfirmHandler] = None
    cancel_check: Optional[Callable[[], bool]] = None
    mcp_manager: Any = None
    #: 本地访问门闩（AccessBroker）。未启用时为 None；外部根写入的
    #: before-bytes 快照与回滚复查依赖它（见 outcome / journal）。
    broker: Any = None
    max_iterations: int = 12
    debug: bool = False

    # ---------------- 能力查询 ----------------
    def visible_tools(self) -> Tuple[str, ...]:
        """模型可见工具：显式名单优先，未指定时用注册表全集（再按注册表存在性过滤）。"""
        if self.registry is None:
            return ()
        names = self.allowed_tools or tuple(self.registry.names())
        return tuple(name for name in names if self.registry.has(name))

    def can_call_model(self) -> bool:
        return self.decision_hook is not None or self.llm is not None

    def is_cancelled(self) -> bool:
        try:
            return bool(self.cancel_check and self.cancel_check())
        except Exception:  # noqa: BLE001 - 取消探测本身不该成为故障源
            return False

    async def call_model(self, messages: List[Dict[str, Any]],
                         tools: Optional[List[Dict[str, Any]]] = None,
                         tool_choice: Any = None,
                         emit: Any = None) -> Dict[str, Any]:
        if self.llm is None:
            raise RuntimeError("未配置模型客户端")
        message = await asyncio.to_thread(self.llm.chat, messages, tools, tool_choice)
        if emit:
            from app.reasoning import emit_llm_call_side_events
            from agents import events as ev

            def _emit(kind: str, data: Dict[str, Any]) -> None:
                emit(kind, data)

            for event in emit_llm_call_side_events(
                    getattr(self.llm, "last_call_meta", {}), ev.make_event):
                _emit(event["type"], event["data"])
        return message

    async def ask_decision(self, state: LoopState, purpose: str,
                           emit: Any = None
                           ) -> Tuple[Any, Optional[Failure]]:
        """
        向模型（或测试里的假决策器）要一次决策，返回**原始参数**。

        这里不做 schema 校验——那是 `decisions` 的职责。本方法只负责
        「把请求发出去、把原始回包拿回来、把协议违反识别成 Failure」。
        """
        if self.decision_hook is not None:
            return await self.decision_hook(state, purpose), None

        spec = (decisions.act_spec(self.visible_tools()) if purpose == "decide"
                else decisions.plan_spec())
        messages = context.build_messages(state, self, purpose)
        try:
            message = await self.call_model(
                messages, tools=[spec], tool_choice=decisions.force_choice(), emit=emit)
        except Exception as e:  # noqa: BLE001 - 模型/网络异常也要变成可分类失败
            return None, repair.classify_error(type(e).__name__, str(e), "llm")

        return decisions.extract_tool_arguments(message)


def build_initial_state(user_text: str, mode: ExecMode) -> LoopState:
    return LoopState(phase=Phase.intake, mode=mode, user_text=user_text)


def build_limits(spec: Dict[str, Any], cfg: Dict[str, Any]) -> LoopLimits:
    """
    从 `agent.yaml`（spec）与 `config.yaml`（cfg）读循环阈值。

    优先级沿用项目既有约定：agent.yaml 优先，config.yaml 兜底，最后是代码默认值。
    全部走 `_int()` 容错转换：**配置写错一个数字不该让整个服务起不来**，
    退化成默认值并把循环跑完，比启动即崩对用户更有用。
    """
    executor_cfg = (cfg or {}).get("executor") or {}
    agent_cfg = (cfg or {}).get("agent") or {}
    max_iterations = _int(spec.get("max_iterations"),
                          _int(agent_cfg.get("max_iterations"), 12))

    return LoopLimits(
        max_steps=_int(spec.get("max_steps"), max_iterations * 4),
        max_model_calls=max_iterations,
        max_replan=max(0, _int(spec.get("max_replan"), 2)),
        max_attempts=max(1, _int(spec.get("max_attempts"), 3)),
        context_char_budget=max(4000, _int(spec.get("context_char_budget"), 24000)),
        max_command_seconds=max(1, _int(executor_cfg.get("command_timeout_seconds"), 60)),
        command_timeout_ceiling=max(1, _int(executor_cfg.get("command_timeout_ceiling"), 120)),
        delegate_parallel=min(6, max(1, _int(spec.get("delegate_parallel"), 3))),
        tail_lines=DEFAULT_LIMITS.tail_lines,
    )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ======================================================================
# 主循环
# ======================================================================
async def run_loop(deps: LoopDeps, state: LoopState, emit) -> LoopState:
    """
    驱动状态机直到停机，返回终态。

    中断优先级由 `machine.interrupt_event` 决定（取消 > 步数上限 > 软预算），
    这里不重复判断——停止条件是机器的事，而且只该有一处定义。
    """
    while not state.phase.is_terminal:
        event = interrupt_event(
            state, deps.config,
            cancel_requested=deps.is_cancelled(),
            over_soft_budget=_over_soft_budget(state, deps),
        )
        if event is None:
            event = await _safe_execute(deps, state, emit)
        if event is None:
            state = force_halt(state, HaltReason.blocked, "阶段执行器抛出未捕获异常")
            break

        try:
            state = transition(state, event, deps.config)
        except IllegalTransition as e:
            # 走到这里说明框架自身有缺陷（缺规则或守卫写错）。
            # 明确报出来而不是悄悄吞掉——这是「失败可分类」对框架自身的要求。
            _emit(emit, "error", {"message": f"非法状态转移（框架缺陷）: {e}"})
            state = force_halt(state, HaltReason.blocked, str(e))
    return state


async def _safe_execute(deps: LoopDeps, state: LoopState, emit) -> Optional[Event]:
    """执行一个阶段。异常在这里收拢，绝不让主循环带着异常退出。"""
    try:
        return await execute_phase(deps, state, emit)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        if deps.debug:
            import traceback

            traceback.print_exc()
        _emit(emit, "error", {
            "message": f"阶段 {state.phase} 执行异常: {type(e).__name__}: {e}"})
        return None


async def execute_phase(deps: LoopDeps, state: LoopState, emit) -> Event:
    """按当前状态分派到具体执行体。**每个分支都必须返回一个事件。**"""
    phase = state.phase
    if phase is Phase.intake:
        return _phase_intake(deps, state)
    if phase is Phase.decompose:
        return await _phase_decompose(deps, state, emit)
    if phase is Phase.schedule:
        return _phase_schedule(deps, state, emit)
    if phase is Phase.perceive:
        return await _phase_perceive(deps, state, emit)
    if phase is Phase.decide:
        return await _phase_decide(deps, state, emit)
    if phase is Phase.authorize:
        return await _phase_authorize(deps, state, emit)
    if phase is Phase.act:
        return await _phase_act(deps, state, emit)
    if phase is Phase.verify:
        return await verify.run(state, deps, emit)
    if phase is Phase.repair:
        return await _phase_repair(deps, state, emit)
    if phase is Phase.delegate:
        return await delegate_mod.run(state, deps, emit)
    if phase is Phase.compress:
        return await context.compress(state, deps, deps.config, emit)
    raise IllegalTransition(f"未知状态: {phase}")


# ======================================================================
# 各阶段
# ======================================================================
def _phase_intake(deps: LoopDeps, state: LoopState) -> Event:
    """校验本轮消息，并判定是否值得建任务图。"""
    text = (state.user_text or "").strip()
    if not text:
        return Event(EV.TURN_REJECTED, {"reason": "消息为空"})
    trivial = planner.is_trivial(text)
    if trivial:
        state.note("本轮判为闲聊或单步问答，不建任务图")
    return Event(EV.TURN_ACCEPTED, {"trivial": trivial})


async def _phase_decompose(deps: LoopDeps, state: LoopState, emit) -> Event:
    """需求拆解 + 项目规划。模型只产出任务图，「能不能执行」由 planner 判定。"""
    raw, failure = await deps.ask_decision(state, "decompose", emit=emit)
    _count_model_call(state)

    if failure is not None:
        return _protocol_rejected(deps, state, emit, failure, EV.PLAN_INVALID)

    parsed, failure = decisions.parse_plan(raw)
    if failure is not None:
        return _protocol_rejected(deps, state, emit, failure, EV.PLAN_INVALID)

    graph, failure = planner.accept(parsed, deps.workspace, deps.config,
                                    broker=deps.broker)
    if failure is not None:
        return _protocol_rejected(deps, state, emit, failure, EV.PLAN_INVALID)

    _emit(emit, "plan", {
        "summary": graph.summary,
        "tasks": [{"id": n.id, "title": n.title, "status": str(n.status),
                   "scope": list(n.scope),
                   "commands": [" ".join(c) for c in n.acceptance.commands]}
                  for n in graph.nodes()],
    })
    state.note(f"建立任务图：{len(graph.tasks)} 个任务｜{graph.summary or '（无摘要）'}")
    _emit(emit, "thought", {
        "content": f"计划：{graph.summary or '（无摘要）'}（{len(graph.tasks)} 个任务）"})

    if deps.mode is ExecMode.plan:
        return Event(EV.PLAN_NEEDS_CONFIRM, {
            "graph": graph, "request_kind": str(PermissionKind.plan),
            "request_id": _new_request_id(),
        })
    return Event(EV.PLAN_ACCEPTED, {"graph": graph, "tasks": len(graph.tasks)})


def _phase_schedule(deps: LoopDeps, state: LoopState, emit) -> Event:
    """选下一步：感知 / 决策 / 委派 / 结束 / 阻塞。全部由 scheduler 决定。"""
    event = scheduler.pick(state, deps.config)
    if event.name in (EV.NEED_PERCEIVE, EV.NEED_DECIDE):
        task = state.graph.get(event.get("task_id")) if state.graph else None
        _emit(emit, "task", {
            "id": event.get("task_id"),
            "status": str(task.status) if task else "",
            "attempts": task.attempts if task else 0,
        })
    return event


async def _phase_perceive(deps: LoopDeps, state: LoopState, emit) -> Event:
    """
    仓库感知。两段式：先按 must_read 读，再按检索结果精读。

    不能合成一批——第二段读哪些文件取决于第一段的检索输出。
    """
    task = state.current_task
    if task is None:
        return Event(EV.PERCEIVE_FAILED, {"reason": "没有当前任务"})

    batch, notes = scheduler.perceive_batch(
        task, deps.visible_tools(), rag_available=deps.rag_available)
    for note in notes:
        _emit(emit, "thought", {"content": f"[{task.id}] {note}"})

    outcomes = await _run_calls(deps, batch, task, emit)

    search_outcomes = [item for item in outcomes
                       if item.name in ("file_search", "rag_search", "fs_search")]
    extras = scheduler.perceive_extras(task, search_outcomes, _read_paths(batch))
    if extras:
        outcomes = outcomes + await _run_calls(deps, extras, task, emit)

    failures = [item for item in outcomes if not item.ok]
    if failures:
        primary = failures[0]
        failure = primary.failure or repair.classify_error("ToolError", primary.text, primary.name)
        # must_read 里的文件读不到 → 任务描述的文件名本身就是错的，
        # 该重新规划而不是「改改参数再试」。用 artifacts 标记，让 repair 能分辨。
        if failure.kind == str(FailureKind.NotFound) and _is_must_read(task, primary.text):
            failure.artifacts["must_read"] = True
            failure.signature = repair.signature(failure.kind, failure.tool, failure.message)
        task.last_failure = failure
        count = repair.record(state, failure)
        return Event(EV.PERCEIVE_FAILED, {
            "outcomes": outcomes, "failure": failure.one_line(),
            "signature": failure.signature, "count": count,
        })

    task.perceived = True
    state.note(f"[{task.id}] 已读 {len(outcomes)} 项代码/检索结果")
    return Event(EV.PERCEIVE_OK, {"outcomes": outcomes})


async def _phase_decide(deps: LoopDeps, state: LoopState, emit) -> Event:
    """模型针对当前任务给出下一步动作。意图合法性与权限都不在这里判。"""
    raw, failure = await deps.ask_decision(state, "decide", emit=emit)
    _count_model_call(state)
    if failure is not None:
        return _protocol_rejected(deps, state, emit, failure, EV.DECISION_INVALID)

    decision, failure = decisions.parse_decision(raw, deps.visible_tools())
    if failure is not None:
        return _protocol_rejected(deps, state, emit, failure, EV.DECISION_INVALID)

    if decision.note:
        _emit(emit, "thought", {"content": decision.describe()})

    if decision.kind is DecisionKind.mark_done:
        # 机器改写：无论有没有写过盘，mark_done 一律进验收。
        # 「你说了不算，机器跑一遍才算」——这条不能省。
        return Event(EV.DECISION_IS_VERIFY, {"note": decision.note})

    if decision.kind is DecisionKind.replan:
        state.note(f"模型请求重新规划：{decision.replan_reason}")
        return Event(EV.DECISION_REPLAN, {"reason": decision.replan_reason})

    if decision.kind is DecisionKind.ask_user:
        state.note(f"需要用户澄清：{decision.question}")
        return Event(EV.DECISION_ASK, {"question": decision.question})

    calls = tuple(decision.calls)
    if not calls:
        return _protocol_rejected(
            deps, state,
            repair.classify_error("ModelProtocolError",
                                  "kind=tool_batch 却没有给出任何调用", "decide"),
            emit, EV.DECISION_INVALID)

    _ensure_checkpoint(deps, state)
    # 键名必须是 pending_calls：转移表只落账这个键。
    # 用别的名字不会报错，只会让 authorize 拿到一个空批次——静默失效，
    # 表现为「一条工具调用记录都没有、验收却一直失败」，极难定位。
    return Event(EV.DECISION_ACCEPTED, {"pending_calls": calls, "count": len(calls)})


async def _phase_authorize(deps: LoopDeps, state: LoopState, emit) -> Event:
    """权限判定。**唯一决定放行 / 拒绝 / 暂停的地方。**"""
    task = state.current_task
    scope = task.scope if task is not None else ()

    # 已有登记的权限请求（plan 或 tools）→ 进入决议：
    # 第一次进 authorize 只发 NEEDS_CONFIRM 登记请求；第二次进 authorize
    # 必须走 _resolve_permission 等人/等超时。没有这条分支，tools 请求会
    # 无限循环发 NEEDS_CONFIRM，永远等不到人也永远不拒绝。
    if state.permission_kind is not None:
        return await _resolve_permission(
            deps, state, emit,
            plan_only=state.permission_kind is PermissionKind.plan)

    auth = permissions.authorize(state.pending_calls, deps.mode,
                                deps.registry, deps.workspace, scope=scope)

    if auth.denied:
        failure = auth.failure or Failure(
            kind=str(FailureKind.PermissionDenied),
            message=f"当前执行档 {deps.mode} 不允许该等级操作（{auth.max_effect.code}）",
            tool="authorize", retryable=False)
        if not failure.signature:
            failure.signature = repair.signature(failure.kind, failure.tool, failure.message)
        if task is not None:
            task.last_failure = failure
        count = repair.record(state, failure)
        _emit(emit, "thought", {"content": f"拒绝执行：{failure.one_line()}"})
        return Event(EV.DENIED, {
            "calls": [call.one_line() for call in auth.calls], "reason": failure.message,
            "failure": failure.one_line(), "signature": failure.signature, "count": count,
        })

    if auth.needs_confirm:
        # 第一步：停在 authorize，把请求登记进状态。
        # 这是一次**真实可持久化的暂停点**，不是「打印一行日志就继续」。
        return Event(EV.NEEDS_CONFIRM, {
            "pending_calls": auth.calls,
            "request_kind": str(PermissionKind.tools),
            "request_id": _new_request_id(),
            "calls": [call.one_line() for call in auth.calls],
        })

    return Event(EV.GRANTED, {
        "pending_calls": auth.calls,
        "effects": [effect.code for effect in permissions.effect_map(auth.calls)],
    })


async def _resolve_permission(deps: LoopDeps, state: LoopState, emit, *,
                              plan_only: bool = False) -> Event:
    """
    解决一个已登记的权限请求。

    **缺省失败关闭**：没有审批处理器（第一期没有审批 UI）时按拒绝处理，
    而不是「等不到人就自动放行」。这条是整套权限体系的地基，不能改成自动放行。
    """
    kind = state.permission_kind
    request_id = state.permission_request_id
    pending = state.pending_calls

    granted = False
    payload = {
        "request_id": request_id,
        "kind": str(kind) if kind else "",
        "calls": [call.one_line() for call in pending] if pending else [],
        "task": state.current_task_id,
    }
    hub = getattr(deps.confirm_handler, "hub", None) if deps.confirm_handler else None
    if hub is not None:
        hub.register(kind, pending, request_id or "")
        _emit(emit, "permission_request", payload)
        try:
            granted = bool(await hub.wait(request_id or ""))
        except Exception as e:  # noqa: BLE001 - 审批通道异常同样按拒绝处理
            _emit(emit, "thought", {"content": f"确认通道异常，按拒绝处理: {e}"})
            granted = False
    elif deps.confirm_handler is not None:
        _emit(emit, "permission_request", payload)
        try:
            granted = bool(await deps.confirm_handler(kind, pending, request_id or ""))
        except Exception as e:  # noqa: BLE001
            _emit(emit, "thought", {"content": f"确认通道异常，按拒绝处理: {e}"})
            granted = False
    else:
        _emit(emit, "permission_request", payload)
        _emit(emit, "thought", {
            "content": "当前界面无法确认高权限操作，已按拒绝处理（不会自动放行）。"})

    if granted:
        return Event(EV.CONFIRM_GRANTED,
                     {"note": "计划已确认"} if plan_only else {"pending_calls": pending})

    if plan_only:
        state.note("计划未获确认，本轮仅输出计划")
        return Event(EV.CONFIRM_REJECTED, {"reason": "计划未获确认"})

    failure = Failure(kind=str(FailureKind.PermissionDenied),
                      message="高权限操作未获确认，已拒绝",
                      tool="authorize", retryable=False)
    failure.signature = repair.signature(failure.kind, failure.tool, failure.message)
    if state.current_task is not None:
        state.current_task.last_failure = failure
    count = repair.record(state, failure)
    return Event(EV.CONFIRM_REJECTED, {
        "reason": failure.message, "failure": failure.one_line(),
        "signature": failure.signature, "count": count,
    })


async def _phase_act(deps: LoopDeps, state: LoopState, emit) -> Event:
    """执行已授权批次。写入前的快照记账在 `outcome.run_tool` 里，不在本函数里。"""
    task = state.current_task

    # 不变量检查：授权过的批次不可能是空的。走到这里说明编排有缺陷
    # （例如事件 payload 用了错的键名导致没落账）。显式报错，不要静默变成 no-op——
    # 「什么都没做却继续往下走」会让失败在很远的地方才浮现。
    if not state.pending_calls:
        failure = repair.classify_error(
            "ArgError", "已授权批次为空：没有任何工具调用可执行（编排缺陷）", "act")
        if task is not None:
            task.last_failure = failure
        count = repair.record(state, failure)
        _emit(emit, "error", {"message": failure.message})
        return Event(EV.ACT_FAILED, {
            "outcomes": [], "failure": failure.one_line(),
            "failure_kind": failure.kind, "signature": failure.signature, "count": count,
        })

    outcomes = await _run_calls(deps, state.pending_calls, task, emit)

    failures = [item for item in outcomes if not item.ok]
    if failures:
        primary = failures[0]
        failure = primary.failure or repair.classify_error("ToolError", primary.text, primary.name)
        if task is not None:
            task.last_failure = failure
        count = repair.record(state, failure)
        _emit(emit, "thought", {
            "content": f"执行失败：{failure.one_line()}（第 {count} 次同类失败）"})
        return Event(EV.ACT_FAILED, {
            "outcomes": outcomes, "failure": failure.one_line(),
            "failure_kind": failure.kind, "signature": failure.signature, "count": count,
            "tail": primary.tail,
        })

    if all(item.effect is Effect.L0_READ for item in outcomes):
        return Event(EV.ACT_OK_READONLY, {"outcomes": outcomes})

    writes = [item for item in outcomes if item.effect is Effect.L1_WRITE]
    if writes and task is not None:
        state.note(f"[{task.id}] 写入 {len(writes)} 处改动")
    return Event(EV.ACT_OK, {
        "outcomes": outcomes,
        "effects": [item.effect.code for item in outcomes],
    })


async def _phase_repair(deps: LoopDeps, state: LoopState, emit) -> Event:
    """
    修复策略选择 + 回滚执行。

    `repair.choose` 是纯函数（只选事件名）；本函数负责它选不动的那些**有副作用的动作**：
    回滚磁盘、清空观察环、标记任务阻塞。
    """
    task = state.current_task
    failure = task.last_failure if task is not None else None
    if failure is None:
        # 没有失败对象却进了 repair。最常见的来源是**非失败型中断**
        # （连续只读空转触顶），而不是编排缺陷。
        # 这里合成一个可分类的 Failure：策略选择与停机原因才都有据可依，
        # 否则只能报一句「框架缺陷」，把一次正常的空转判定说成故障。
        failure = repair.classify_error(
            str(FailureKind.LoopStall),
            f"连续 {state.read_streak} 批只读调用仍未推进任务，判定为空转",
            tool="loop")
        if task is not None:
            task.last_failure = failure

    count = state.failure_counts.get(failure.signature, 1)
    event_name = repair.choose(state, failure, deps.config, count)
    payload: Dict[str, Any] = {
        "failure": failure.one_line(), "kind": failure.kind,
        "signature": failure.signature, "count": count,
        "action": repair.FAILURE_KIND_HINTS.get(failure.kind, ""),
    }

    if event_name == EV.REPAIR_ROLLBACK and task is not None:
        restored = deps.journal.restore(task.id, deps.workspace, broker=deps.broker)
        task.perceived = False           # 回滚后必须重新感知真实文件
        payload["restored"] = restored
        _emit(emit, "rollback", {
            "task": task.id, "paths": restored,
            "checkpoint": deps.journal.checkpoint_of(task.id) or "",
        })
        _emit(emit, "thought", {
            "content": f"已回滚 {len(restored)} 个文件到检查点，接下来重新读取真实内容。",
        })
        state.note(f"[{task.id}] 回滚 {len(restored)} 个文件（{failure.kind}）")
        # 观察环里的内容已经与磁盘不一致，继续拿它只会打出必然失败的补丁 → 清空
        payload["reset_observations"] = True

    if event_name == EV.REPAIR_BLOCKED and task is not None:
        task.status = TaskStatus.blocked
        payload["blocked"] = task.id
        state.note(f"[{task.id}] 任务阻塞（{failure.kind}）")
        _emit(emit, "task", {"id": task.id, "status": str(task.status),
                             "attempts": task.attempts})

    if event_name == EV.REPAIR_REPLAN and task is not None:
        task.status = TaskStatus.pending
        payload["reset_observations"] = True

    if event_name == EV.STALLED:
        payload["reason"] = f"同一失败重复 {count} 次：{failure.one_line()}"

    payload["outcomes"] = [_failure_observation(failure)]
    return Event(event_name, payload)


# ======================================================================
# 小工具
# ======================================================================
async def _run_calls(deps: LoopDeps, calls, task, emit) -> List[ToolOutcome]:
    """执行一批调用并把事件流出去。抽出来是为了让 perceive / act 走同一条路径。"""
    return await outcome.run_batch(
        deps.registry, calls, journal=deps.journal, task=task, workspace=deps.workspace,
        broker=deps.broker,
        on_start=lambda call: _emit_tool_call(emit, call),
        on_done=lambda item: _emit_tool_result(emit, item),
    )


def _protocol_rejected(deps: LoopDeps, state: LoopState, emit, failure: Failure,
                       event_name: str) -> Event:
    """
    协议违反的统一处理：记一次失败计数 + 明确提示「只调用 submit_decision」。

    **不从散文里抠 JSON。** 那会把「模型没按协议输出」这个信号静默成功，
    而协议违反必须是可见、可计数、可停机的一等事件。
    """
    count = repair.record(state, failure)
    _emit(emit, "thought", {
        "content": f"模型未按协议提交决策（第 {count} 次）：请只调用 submit_decision。"})
    return Event(event_name, {
        "reason": failure.message, "signature": failure.signature, "count": count,
    })


def _ensure_checkpoint(deps: LoopDeps, state: LoopState) -> None:
    """进入任务的第一次写之前建立检查点。幂等，重复调用无副作用。"""
    task = state.current_task
    if task is None:
        return
    from .planner import parse_root_rel

    workspace_paths = [
        item for item in task.scope if parse_root_rel(str(item)) is None
    ] + [
        target for call in state.pending_calls
        for target in permissions.write_targets(call.name, call.arguments)
    ]
    deps.journal.checkpoint(task.id, workspace_paths, deps.workspace,
                           reason=f"进入任务 {task.id} 首次写入前")
    if task.checkpoint_id is None:
        task.checkpoint_id = deps.journal.checkpoint_of(task.id)


def _failure_observation(failure: Failure) -> ToolOutcome:
    """
    把失败本身作为一条「观察」放回观察环。

    这样模型在下次 `decide` 里看到的是**结构化的失败原因**（新尝试的依据），
    而不是一段不知道从哪来的报错文本。
    """
    tail = str(failure.artifacts.get("tail") or "")
    text = failure.message if not tail else f"{failure.message}\n[输出尾部]\n{tail}"
    return ToolOutcome(
        call_id="failure",
        name=f"[失败:{failure.tool or 'loop'}]",
        ok=False, text=text, failure=failure,
        artifacts={"tail": tail}, effect=Effect.L0_READ,
    )


def _emit_tool_call(emit, call: ToolCall) -> None:
    _emit(emit, "tool_call", {
        "id": call.id, "name": call.name, "arguments": call.arguments,
        "effect": call.effect.code,
    })


def _emit_tool_result(emit, item: ToolOutcome) -> None:
    _emit(emit, "tool_result", {
        "id": item.call_id, "name": item.name, "output": item.text,
        "is_error": not item.ok, "duration_ms": item.duration_ms,
        "failure_kind": item.failure.kind if item.failure else "",
    })


def _emit(emit, kind: str, data: Dict[str, Any]) -> None:
    if emit is not None:
        emit(kind, data)


def _new_request_id() -> str:
    return f"perm_{uuid.uuid4().hex[:8]}"


def _count_model_call(state: LoopState) -> None:
    """
    记一次模型调用。

    这是循环级字段里**唯一由运行时写入**的计数器：只有真正发起请求的地方才知道
    调用是否发生（协议重试、异常都可能让它没发生），而停机判断读的就是它，
    因此必须准。除此之外循环级字段一律由 `machine.transition` 落账。
    """
    state.model_calls += 1


def _over_soft_budget(state: LoopState, deps: LoopDeps) -> bool:
    """
    软预算探测。

    只对 `decide` 做完整估算：它每轮都跑且上下文最大；
    `decompose` 只在开头跑一次，那时上下文必然很小，估它只是白跑一次目录遍历。

    **刻意不吞异常**：这里一吞，`build_messages` 里的真 bug 就会被伪装成
    「预算正常」，表现为「什么事件都没有就停机了」，极难定位。
    让它抛出去，由 agent 层转成可见的 error 事件（见 agent.py 的 _drive）。
    """
    if state.phase is not Phase.decide:
        return False
    estimate = context.estimate_chars(context.build_messages(state, deps, "decide"))
    return context.over_soft_budget(estimate, deps.config)


def _read_paths(calls) -> List[str]:
    """本批次里已经读过哪些文件（从调用参数取，比从结果里反解更可靠）。"""
    paths: List[str] = []
    for call in calls:
        if call.name == "fs_read":
            root = call.arguments.get("root")
            rel = call.arguments.get("path")
            if isinstance(root, str) and isinstance(rel, str):
                key = f"{root}:{rel}"
                if key not in paths:
                    paths.append(key)
            continue
        if call.name != "read_file":
            continue
        path = call.arguments.get("path")
        if isinstance(path, str) and path not in paths:
            paths.append(path)
    return paths


def _is_must_read(task, text: str) -> bool:
    body = text or ""
    return any(path and path in body for path in task.acceptance.must_read)


# ======================================================================
# 收尾
# ======================================================================
def fallback_summary(state: LoopState) -> str:
    """
    机器自己拼的收尾文本。

    用在**最终那次模型调用失败时**：宁可给一段机器摘要，也不回退到工具循环，
    更不给一段空回复。终态之后禁止任何副作用，这条没有例外。
    """
    lines: List[str] = []
    reason = state.halt_reason or HaltReason.completed

    if state.graph is None:
        lines.append("本轮没有建立任务图。" if reason is HaltReason.completed
                     else f"本轮在建立任务图之前结束（{reason}）。")
    else:
        done = [n for n in state.graph.nodes() if n.status is TaskStatus.done]
        blocked = state.graph.blocked_ids()
        lines.append(f"任务进度：{state.graph.progress_line()}。")
        if done:
            lines.append("已完成：" + "，".join(f"{n.id} {n.title}" for n in done))
        if blocked:
            lines.append("未完成（阻塞）：" + "，".join(blocked))
        pending = [n.id for n in state.graph.nodes()
                   if not n.status.is_finished and n.id not in blocked]
        if pending:
            lines.append("未完成：" + "，".join(pending))

    if state.verify_log:
        last = state.verify_log[-1]
        lines.append(f"最后一次验收：{'通过' if last.get('ok') else '未通过'}"
                     f"（{last.get('detail', '')}）")

    if reason is HaltReason.stalled:
        lines.append("停止原因：同一个错误重复出现，继续尝试只会重复失败。")
    elif reason is HaltReason.budget:
        lines.append("停止原因：本轮步数或预算已用尽，未完成的改动保留在磁盘上。")
    elif reason is HaltReason.cancelled:
        lines.append("停止原因：会话被中断，已写入的文件未自动回滚。")
    return "\n".join(lines)
