# -*- coding: utf-8 -*-
"""
工具执行适配层：把 `tools.base.ToolResult` 变成 state_loop 的 `ToolOutcome`。

这是**唯一带副作用的工具执行入口**。所有调用（内置工具、沙箱、命令、MCP）都必须走这里，
因为只有它负责四件事：

1. **写前快照**——L1 调用在真正执行**之前**调 `journal.remember_write`。
   放在这里而不是工具内部，是因为「记账」是框架的职责，工具不该关心回滚；
   而放在这里之后又保证了「忘记记账」在结构上不可能发生。
2. **失败分类**——把异常类名交给 `repair.classify_error`，产出可驱动的 `Failure`。
3. **事件上报**——通过回调在**执行前**发 `tool_call`、执行后发 `tool_result`，
   这样前端能在命令跑着的时候就看到它，而不是等结果回来才出现。
4. **并发策略**——L0 允许并发（上限 4），L1 及以上强制按顺序。
   并发写共享列表是人类最容易犯的错：结果顺序不确定 → 事件顺序与实际不符 → 无法复盘。
   这里用**按下标回填**的方式保证顺序恒定。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable, List, Optional, Sequence

from tools.workspace import WorkspaceSecurity

from . import repair
from .journal import FileJournal
from .permissions import external_write_targets, write_targets
from .state import Effect, TaskNode, ToolCall, ToolOutcome

__all__ = ["run_tool", "run_batch", "MAX_READ_CONCURRENCY"]

#: 只读批次的并发上限。再高也只是把本地模型/磁盘的排队变长。
MAX_READ_CONCURRENCY = 4


async def run_tool(
    registry,
    call: ToolCall,
    *,
    journal: Optional[FileJournal] = None,
    task: Optional[TaskNode] = None,
    workspace: Optional[WorkspaceSecurity] = None,
    broker: Any = None,
) -> ToolOutcome:
    """
    执行一次工具调用并返回分类后的结果。**不抛异常**。

    同步的 `ToolRegistry.call` 丢到线程里跑，避免阻塞事件循环（命令可能跑几十秒）。
    """
    started = time.perf_counter()

    # ---- 写前快照（必须在执行之前）----
    if (call.effect is Effect.L1_WRITE and journal is not None
            and task is not None and workspace is not None):
        for target in write_targets(call.name, call.arguments):
            journal.remember_write(task.id, target,
                                   FileJournal.read_before(workspace, target))
    # ---- 外部根写入（fs_*，L4）同样记账：修复路径的回滚对外部根才能生效 ----
    if (call.effect is Effect.L4_MCP_MUTATE and broker is not None
            and journal is not None and task is not None):
        for root, rel in external_write_targets(call.name, call.arguments):
            try:
                target = broker.resolve(root, rel, need_write=True)
            except Exception:  # noqa: BLE001 - 解析失败的写入会在执行时同样失败，无需记账
                continue
            journal.remember_write_external(
                task.id, root, rel,
                before=FileJournal._read_bytes(target), abs_path=str(target))

    try:
        result = await asyncio.to_thread(registry.call, call.name, call.arguments)
        ok = bool(result.ok)
        text = result.text or ""
        artifacts = dict(result.artifacts or {})
        failure = None
        if not ok:
            failure = repair.classify_error(
                result.error_type, result.error_message or text, call.name, artifacts)
    except Exception as e:  # noqa: BLE001 - 线程层意外也不能让整个循环崩掉
        ok = False
        text = f"[工具错误] {type(e).__name__}: {e}"
        artifacts = {}
        failure = repair.classify_error(type(e).__name__, str(e), call.name)

    return ToolOutcome(
        call_id=call.id,
        name=call.name,
        ok=ok,
        text=text,
        failure=failure,
        artifacts=artifacts,
        effect=call.effect,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


async def run_batch(
    registry,
    calls: Sequence[ToolCall],
    *,
    journal: Optional[FileJournal] = None,
    task: Optional[TaskNode] = None,
    workspace: Optional[WorkspaceSecurity] = None,
    broker: Any = None,
    on_start: Optional[Callable[[ToolCall], None]] = None,
    on_done: Optional[Callable[[ToolOutcome], None]] = None,
    max_read_concurrency: int = MAX_READ_CONCURRENCY,
) -> List[ToolOutcome]:
    """
    执行一批调用，**返回顺序与入参顺序严格一致**。

    - 全部为 L0：并发执行（上限 `max_read_concurrency`）
    - 含任何 L1/L2/L3/L4：按调用顺序串行执行

    串行的理由不只是「怕冲突」：串行让「第 3 个调用看到了第 2 个的写入」成为确定事实，
    而出错时的现场（哪一步把文件改坏了）也因此可重现。
    """
    calls = list(calls)
    if not calls:
        return []

    slots: List[Optional[ToolOutcome]] = [None] * len(calls)
    read_only = all(call.effect is Effect.L0_READ for call in calls)

    async def _one(index: int, call: ToolCall) -> None:
        if on_start is not None:
            on_start(call)
        outcome = await run_tool(registry, call, journal=journal, task=task,
                                 workspace=workspace, broker=broker)
        if on_done is not None:
            on_done(outcome)
        slots[index] = outcome

    if read_only and len(calls) > 1:
        limit = max(1, min(max_read_concurrency, len(calls)))
        semaphore = asyncio.Semaphore(limit)

        async def _guarded(index: int, call: ToolCall) -> None:
            async with semaphore:
                await _one(index, call)

        await asyncio.gather(*(_guarded(i, c) for i, c in enumerate(calls)))
    else:
        for index, call in enumerate(calls):
            await _one(index, call)

    return [outcome for outcome in slots if outcome is not None]
