# -*- coding: utf-8 -*-
"""
StateLoopAgent：把状态机接到 BaseAgent 与 SSE 协议上。

这一层**只做协议适配**，不含任何业务判断。它解决两个很具体的问题：

1. **依赖来源**：状态机需要的能力（注册表、工作区、日志、命令执行器、预算、执行档）
   打包在 `LoopDeps` 里，从 `AgentDependencies.loop_deps` 取出。缺失时立刻报错，
   而不是在循环跑到一半时抛 `AttributeError`。

2. **中途事件**：`tool_call` 必须在命令**开始执行之前**就发给前端。
   否则一条跑 60 秒的命令要等结果出来才出现在界面上，用户会以为程序卡死。
   所以这里用 `asyncio.Queue` + `asyncio.wait` 双路等待：阶段在跑，事件随时流出。
   这也是「阶段执行器只产出事件、不改循环级字段」那条约定的配套设施。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

from .. import events as ev
from ..base import AgentDependencies, BaseAgent
from . import context, machine, runtime
from .state import HaltReason, LoopState

__all__ = ["StateLoopAgent"]

#: 收尾文本的伪流式分片大小。本地模型拿不到真流式时，切块下发保证「逐字出现」的观感。
_CHUNK = 24


class StateLoopAgent(BaseAgent):
    """状态驱动主循环。与 native_react / autogen 等平级，通过 registry 注册。"""

    framework_name = "state_loop"

    def __init__(self, deps: AgentDependencies):
        super().__init__(deps)
        loop_deps = getattr(deps, "loop_deps", None)
        if loop_deps is None:
            raise ValueError(
                "state_loop 需要 AgentDependencies.loop_deps（agents/state_loop/runtime.LoopDeps）。"
                "请通过 agents.agent.build_runtime 装配，或显式构造 LoopDeps 传进来。"
            )
        self.loop = loop_deps
        if not getattr(self.loop, "system_prompt", ""):
            self.loop.system_prompt = deps.system_prompt

    # ---------------- 对外流式入口 ----------------
    async def astream_run(self, history: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        state = runtime.build_initial_state(_last_user_text(history), self.loop.mode)

        queue: "asyncio.Queue[tuple[str, Dict[str, Any]]]" = asyncio.Queue()
        holder: Dict[str, Any] = {}

        def emit(kind: str, data: Dict[str, Any]) -> None:
            # emit 会被同步调用（含工具线程里的回调），因此必须非阻塞
            queue.put_nowait((kind, data))

        async def _drive() -> None:
            try:
                holder["state"] = await runtime.run_loop(self.loop, state, emit)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 主循环异常也要变成事件，不能让 SSE 断在半路
                holder["error"] = e
                emit("error", {"message": f"主循环异常终止: {type(e).__name__}: {e}"})

        driver = asyncio.create_task(_drive())
        try:
            async for event in _drain(driver, queue):
                yield event
        except asyncio.CancelledError:
            # 客户端断开：取消驱动任务，但**不回滚磁盘**（用户可能已经看到一半的改动）
            driver.cancel()
            try:
                await driver
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            raise

        final: LoopState = holder.get("state") or machine.force_halt(
            state, HaltReason.blocked,
            f"主循环未产生终态: {holder.get('error') or '未知原因'}")

        async for event in self._final_answer(final):
            yield event
        yield ev.make_event(ev.DONE, {
            "finish_reason": str(final.halt_reason or HaltReason.completed),
            "phase": str(final.phase),
            "steps": final.step_count,
            "model_calls": final.model_calls,
        })

    # ---------------- 收尾 ----------------
    async def _final_answer(self, state: LoopState) -> AsyncIterator[Dict[str, Any]]:
        """
        生成面向用户的收尾文本。

        只允许散文，**不允许再发工具调用**——终态之后禁止任何副作用。
        模型这次调用失败时用机器摘要兜底，不回退到工具循环。
        """
        text = ""
        if self.loop.can_call_model():
            try:
                message = await self.loop.call_model(
                    context.final_messages(state, self.loop))
                from app.reasoning import emit_llm_call_side_events
                for side in emit_llm_call_side_events(
                        getattr(self.loop.llm, "last_call_meta", {}), ev.make_event):
                    yield side
                text = str((message or {}).get("content") or "").strip()
            except Exception as e:  # noqa: BLE001 - 收尾失败不该影响已经完成的交付
                text = ""
                state.note(f"收尾模型调用失败，改用机器摘要：{type(e).__name__}")
        if not text:
            text = runtime.fallback_summary(state)

        for index in range(0, len(text), _CHUNK):
            yield ev.make_event(ev.TOKEN, {"content": text[index:index + _CHUNK]})
            await asyncio.sleep(0.01)


# ======================================================================
# 辅助
# ======================================================================
async def _drain(driver: "asyncio.Task[None]", queue: "asyncio.Queue[tuple[str, Dict[str, Any]]]"
                 ) -> AsyncIterator[Dict[str, Any]]:
    """
    一边等驱动任务结束，一边把队列里的事件流出去。

    事件顺序由队列保证（先入先出）；驱动任务结束后把残余事件排空，
    避免最后几条 `tool_result` / `done` 丢在队列里没发出去。
    """
    while True:
        getter = asyncio.ensure_future(queue.get())
        done, _ = await asyncio.wait({driver, getter},
                                     return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            kind, data = getter.result()
            yield ev.make_event(kind, data)
            continue
        # 驱动已结束且此刻没有新事件：先取消挂起的 get，再排空残余
        if not getter.done():
            getter.cancel()
        break

    while not queue.empty():
        kind, data = queue.get_nowait()
        yield ev.make_event(kind, data)


def _last_user_text(history: Optional[List[Dict[str, Any]]]) -> str:
    for message in reversed(history or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""
