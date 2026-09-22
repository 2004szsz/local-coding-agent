# -*- coding: utf-8 -*-
"""
子代理委派：**任务图上的并行**，不是角色扮演。

与「多角色 Agent」（让架构师/工程师/评审员轮流发言）的本质区别在于**划分依据**：

    角色扮演：按人格与职责描述分工，冲突靠提示词约定「请评审员检查」
    本设计：  按**依赖拓扑 + 写路径租约**分工，冲突靠租约证明「不可能发生」

并行只在能证明「不冲突」时才发生。租约相交的任务不会并行，而是退回串行——
这不是保守，而是承认一个事实：两个子代理同时改同一个文件时，
没有任何提示词能保证它们不互相覆盖。

隔离规则（架构文档第 9 节）：子循环有独立的观察环、独立预算、深度锁 1，
**只把结构化 `TaskResult` 交回父循环**，不回传子观察全文——否则上下文隔离就白做了。
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Dict, List, Optional

from . import repair, scheduler
from .machine import EV, Event, LoopLimits
from .state import LoopState, TaskGraph, TaskNode, TaskResult, TaskStatus

__all__ = ["run", "child_state", "child_config"]


async def run(state: LoopState, deps, emit) -> Event:
    """
    并行执行一批互不相交的子任务。

    :return: `children_joined`（全部正常返回）或 `child_crashed`（有子循环抛异常）。
             两者都回到 schedule——**部分成功好过全盘失败**：
             一个子任务失败不该取消其他兄弟的成果，也不该结束整个用户回合。
    """
    from . import runtime          # 局部导入：runtime 需要 delegate，模块级导入会成环

    limits: LoopLimits = deps.config
    batch = scheduler.select_parallel_batch(state, limits)
    if len(batch) < 2:
        return Event(EV.CHILDREN_JOINED, {
            "results": [], "note": "可安全并行的任务不足 2 个，退回串行执行",
        })

    config = child_config(limits)
    crashed = False

    async def _one(node: TaskNode) -> TaskResult:
        nonlocal crashed
        node.status = TaskStatus.running
        child_emit = _forward(emit, node.id) if emit is not None else _noop
        try:
            final = await runtime.run_loop(
                dataclasses.replace(deps, config=config),
                child_state(state, node),
                child_emit,
            )
        except Exception as e:  # noqa: BLE001 - 子循环的任何异常都要收拢成结果，不能让父回合崩掉
            crashed = True
            node.last_failure = repair.classify_error(type(e).__name__, str(e), "delegate")
            node.status = TaskStatus.blocked
            return TaskResult(task_id=node.id, status=node.status,
                              failure=node.last_failure)

        # 子循环以 blocked / stalled / budget 结束时，它自己不会标任务状态——
        # 那一步由父循环补上，避免任务图里留下一个永远「running」的僵尸节点。
        if not node.status.is_finished:
            node.status = TaskStatus.blocked

        journal = getattr(deps, "journal", None)
        changed = tuple(journal.changed_paths(node.id)) if journal is not None else ()
        summary = ""
        if final.verify_log:
            last = final.verify_log[-1]
            summary = str(last.get("detail") or "")
        return TaskResult(
            task_id=node.id,
            status=node.status,
            changed_paths=changed,
            verify_summary=summary,
            failure=node.last_failure,
            halt_reason=final.halt_reason,
        )

    results: List[TaskResult] = list(await asyncio.gather(*(_one(node) for node in batch)))
    # 按 task id 排序后交给父循环：并发完成顺序不确定，而事件顺序必须确定，否则无法复盘
    results.sort(key=lambda item: item.task_id)
    state.child_results = tuple(results)
    state.note(f"并行处理 {len(results)} 个子任务："
               + "，".join(r.one_line() for r in results))

    if emit is not None:
        emit("delegate", {
            "parent": state.current_task_id,
            "children": [node.id for node in batch],
            "results": [r.one_line() for r in results],
        })

    payload: Dict[str, Any] = {"results": [r.one_line() for r in results],
                               "task_ids": [r.task_id for r in results]}
    if crashed:
        payload["reason"] = "有子循环抛出未捕获异常，该任务已标为 blocked"
    return Event(EV.CHILD_CRASHED if crashed else EV.CHILDREN_JOINED, payload)


def child_config(limits: LoopLimits) -> LoopLimits:
    """
    子循环预算 = 父预算的固定比例，并有硬顶。

    用「比例 + 硬顶」而不是「父剩余预算的实时比例」：后者会让子循环的预算
    取决于调度顺序（谁先跑谁拿得多），同一份任务图两次运行行为不同。
    """
    budget = max(2000, int(limits.context_char_budget * limits.delegate_budget_ratio))
    return dataclasses.replace(
        limits,
        context_char_budget=min(budget, limits.delegate_budget_ceiling),
        max_depth=0,                      # 子循环禁止再委派（深度锁 1）
        max_replan=0,                     # 子循环不做重规划：任务划分问题交回父循环
    )


def child_state(parent: LoopState, node: TaskNode) -> LoopState:
    """
    构造子循环状态：**空观察环** + 只含本任务的图。

    子任务节点对象与父图**共享同一个引用**——这样状态、感知标记、尝试次数
    天然落在父任务图上，不需要一次易错的「结果合并拷贝」。
    并发只触及各自的节点，不存在两个子循环写同一个对象的情况。
    """
    return LoopState(
        phase=_first_phase(node),
        mode=parent.mode,
        user_text=parent.user_text,
        graph=TaskGraph(summary=f"子任务 {node.id}", tasks={node.id: node}, order=[node.id]),
        current_task_id=node.id,
        depth=parent.depth + 1,
    )


def _first_phase(node: TaskNode) -> "Any":
    from .state import Phase

    return Phase.perceive if not node.perceived else Phase.decide


def _forward(emit, task_id: str):
    """把子循环的事件转发给父 emit，并标注归属，便于前端区分是谁在跑。"""

    def _emit(kind: str, data: Dict[str, Any]) -> None:
        emit(kind, {**(data or {}), "child": task_id})

    return _emit


def _noop(kind: str, data: Dict[str, Any]) -> None:
    return None
