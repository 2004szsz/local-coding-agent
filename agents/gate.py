# -*- coding: utf-8 -*-
"""
框架共用的工具审批闸。

`state_loop` 在 `authorize` 状态等人确认；默认框架 `native_react` 以及
autogen / llamaindex / crewai 没有这个状态。本模块把「L4 / 需确认档位」
的暂停点抽成一段可复用逻辑，让任何框架在真正 `execute` 之前都能：

    1. 用 `permissions.classify` 判定副作用（权威仍在 permissions.py）；
    2. 需要确认时发出 `permission_request` SSE，走同一条 PermissionHub；
    3. 无处理器或超时 → 失败关闭，工具不执行。

失败关闭是整套权限体系的地基：等不到人绝不自动放行。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Tuple

from tools.base import ERROR_PREFIX

from . import events as ev
from .state_loop.permissions import Policy, classify
from .state_loop.state import Effect, ExecMode, PermissionKind, ToolCall


@dataclass(frozen=True)
class GateDecision:
    """一次工具调用的闸门结论。`output` 非空表示已经给出失败正文，调用方不要再执行。"""

    allowed: bool
    output: str = ""
    events: Tuple[Dict[str, Any], ...] = ()


def _denied_text(message: str) -> str:
    return f"{ERROR_PREFIX} {message}"


def _new_request_id() -> str:
    return f"perm_{uuid.uuid4().hex[:8]}"


def _effective_mode(mode: ExecMode, loop_deps: Any = None) -> ExecMode:
    """
    native_react 没有任务图暂停点。plan 档下：本轮尚未确认时对 L1+ 走确认；
    用户点允许后 `plan_confirmed=True`，后续按 auto_workspace（L4 仍确认）。
    """
    if mode is ExecMode.plan and loop_deps is not None and getattr(
            loop_deps, "plan_confirmed", False):
        return ExecMode.auto_workspace
    return mode if isinstance(mode, ExecMode) else ExecMode.auto_workspace


def _policy_for(effect: Effect, mode: ExecMode) -> Policy:
    from .state_loop.permissions import MODE_POLICY

    table = MODE_POLICY.get(mode, MODE_POLICY[ExecMode.auto_workspace])
    return table.get(effect, Policy.confirm)


async def authorize_tool(
    name: str,
    arguments: Any,
    *,
    registry,
    workspace,
    confirm_handler=None,
    mode: ExecMode = ExecMode.auto_workspace,
    call_id: str = "",
    loop_deps: Any = None,
) -> GateDecision:
    """
    在任意框架执行工具之前做权限判定。

    :returns: `allowed=True` 才可以 `registry.execute`；否则 `output` 就是给模型的失败正文。
    """
    kwargs = arguments if isinstance(arguments, dict) else {}
    result = classify(name, kwargs, registry, workspace)
    events: list = []

    if result.failure is not None:
        return GateDecision(
            allowed=False,
            output=_denied_text(result.failure.message),
            events=tuple(events),
        )

    effective = _effective_mode(mode, loop_deps)
    # ReAct 没有 decompose 暂停：plan 档下首次写盘/命令/外部写入需确认。
    if (effective is ExecMode.plan
            and result.effect in (Effect.L1_WRITE, Effect.L3_COMMAND, Effect.L4_MCP_MUTATE)):
        from .state_loop.permissions import Policy as _P
        policy = _P.confirm
    else:
        policy = _policy_for(result.effect, effective)
    if policy is Policy.auto:
        return GateDecision(allowed=True)

    if policy is Policy.deny:
        return GateDecision(
            allowed=False,
            output=_denied_text(
                f"当前执行档不允许该等级操作（{result.effect.code}）"),
        )

    # Policy.confirm：发出审批请求，等人或超时。
    request_id = _new_request_id()
    call = ToolCall(
        id=call_id or request_id,
        name=name,
        arguments=kwargs if isinstance(kwargs, dict) else {},
        effect=result.effect,
    )
    kind = PermissionKind.plan if effective is ExecMode.plan else PermissionKind.tools
    payload = {
        "request_id": request_id,
        "kind": str(kind),
        "calls": [call.one_line()],
        "task": "",
    }

    granted = False
    hub = getattr(confirm_handler, "hub", None) if confirm_handler is not None else None
    if hub is not None:
        hub.register(kind, (call,), request_id)
        events.append(ev.make_event(ev.PERMISSION_REQUEST, payload))
        try:
            granted = bool(await hub.wait(request_id))
        except Exception as e:  # noqa: BLE001 - 审批通道异常同样按拒绝处理
            events.append(ev.make_event(ev.THOUGHT, {
                "content": f"确认通道异常，按拒绝处理: {e}",
            }))
            granted = False
    elif confirm_handler is not None:
        events.append(ev.make_event(ev.PERMISSION_REQUEST, payload))
        try:
            granted = bool(await confirm_handler(
                kind, (call,), request_id))
        except Exception as e:  # noqa: BLE001
            events.append(ev.make_event(ev.THOUGHT, {
                "content": f"确认通道异常，按拒绝处理: {e}",
            }))
            granted = False
    else:
        events.append(ev.make_event(ev.PERMISSION_REQUEST, payload))
        events.append(ev.make_event(ev.THOUGHT, {
            "content": "当前界面无法确认高权限操作，已按拒绝处理（不会自动放行）。",
        }))

    if granted:
        if mode is ExecMode.plan and loop_deps is not None:
            loop_deps.plan_confirmed = True
        return GateDecision(allowed=True, events=tuple(events))

    return GateDecision(
        allowed=False,
        output=_denied_text("高权限操作未获确认，已拒绝"),
        events=tuple(events),
    )


def loop_confirm_handler(deps) -> Any:
    """从 AgentDependencies.loop_deps 取出 confirm_handler；没有则 None。"""
    loop = getattr(deps, "loop_deps", None)
    return getattr(loop, "confirm_handler", None) if loop is not None else None


def loop_mode(deps) -> ExecMode:
    loop = getattr(deps, "loop_deps", None)
    mode = getattr(loop, "mode", None) if loop is not None else None
    return mode if isinstance(mode, ExecMode) else ExecMode.auto_workspace


def loop_workspace(deps):
    loop = getattr(deps, "loop_deps", None)
    if loop is not None and getattr(loop, "workspace", None) is not None:
        return loop.workspace
    return None


def execute_gated_sync(deps, name: str, arguments: Any) -> str:
    """
    同步框架（AutoGen / LlamaIndex / CrewAI）的工具执行入口。

    这些适配器在同步回调里调工具，没法 await PermissionHub。
    L0–L3 按档位自动放行；需要确认的调用失败关闭，避免在没有审批 UI
    的同步路径上裸写外部根。要用人确认的写入请走 native_react / state_loop。
    """
    from .state_loop.permissions import Policy, classify
    from .state_loop.state import ExecMode

    kwargs = arguments if isinstance(arguments, dict) else {}
    result = classify(name, kwargs, deps.tools, loop_workspace(deps))
    if result.failure is not None:
        return _denied_text(result.failure.message)
    loop = getattr(deps, "loop_deps", None)
    effective = _effective_mode(loop_mode(deps), loop)
    if (effective is ExecMode.plan
            and result.effect in (Effect.L1_WRITE, Effect.L3_COMMAND, Effect.L4_MCP_MUTATE)):
        policy = Policy.confirm
    else:
        policy = _policy_for(result.effect, effective)
    if policy is Policy.auto:
        return deps.tools.execute(name, arguments)
    if policy is Policy.deny:
        return _denied_text(f"当前执行档不允许该等级操作（{result.effect.code}）")
    return _denied_text(
        "该操作需要人工确认。当前框架无法弹出审批卡片，已拒绝执行。"
        "请改用 native_react 或 state_loop，或在配置里不要打开写入根 / allow_actions。")
