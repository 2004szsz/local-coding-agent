# -*- coding: utf-8 -*-
"""
CrewAI 适配器 —— 可选依赖，多角色协作 + 任务自动拆解。

启用方式：
    pip install crewai
    config.yaml: agent.framework: "crewai"

实现：依据 config.yaml -> frameworks.crewai.tasks 将编码任务拆为顺序流水线，
由「分析师 -> 工程师（持工具，可多个实现环节）-> 评审员」协作完成。
CrewAI 为同步框架，这里放到线程池中执行，并通过队列实时桥接工具调用事件。
"""
from __future__ import annotations

import asyncio
import itertools
import queue
from typing import Any, AsyncIterator, Dict, List

from . import events as ev
from .base import AgentDependencies, BaseAgent
from .registry import OptionalFrameworkMissingError


class CrewAICodingCrew(BaseAgent):
    framework_name = "crewai"

    def __init__(self, deps: AgentDependencies):
        super().__init__(deps)
        try:
            import crewai  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise OptionalFrameworkMissingError(
                "CrewAI 依赖未安装，请执行: pip install crewai"
            ) from e

    # ---------------- 工具适配（带跨线程事件队列） ----------------
    def _adapt_tools(self, event_q: "queue.Queue",
                     counter: itertools.count) -> List[Any]:
        from pydantic import BaseModel, Field, create_model
        from crewai.tools import BaseTool

        crew_tools: List[Any] = []
        for name in self.deps.tools.names():
            tool = self.deps.tools.get(name)

            # 1) 动态生成参数 Pydantic 模型
            props = tool.parameters.get("properties", {}) or {}
            required = set(tool.parameters.get("required", []) or [])
            fields = {}
            for key, schema in props.items():
                desc = str(schema.get("description", "")) if isinstance(schema, dict) else ""
                fields[key] = (Any, Field(default=... if key in required else None,
                                         description=desc))
            args_model = create_model(f"{tool.name}_args", **fields)

            # 2) 动态生成 CrewAI 工具类，执行时把事件推入队列
            registry = self.deps.tools

            def make_run(tool_name=tool.name, registry=registry, deps=self.deps):
                def _run(self, **kwargs):
                    from .gate import execute_gated_sync
                    call_id = f"call_{next(counter)}"
                    event_q.put(("call", call_id, tool_name, kwargs))
                    output = execute_gated_sync(deps, tool_name, kwargs)
                    event_q.put(("result", call_id, tool_name, output))
                    return output
                return _run

            tool_cls = type(
                f"CrewTool_{tool.name}",
                (BaseTool,),
                {
                    "name": tool.name,
                    "description": tool.description[:1024],
                    "args_schema": args_model,
                    "_run": make_run(),
                },
            )
            crew_tools.append(tool_cls())
        return crew_tools

    # ---------------- 主流程 ----------------
    async def astream_run(self, history: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        try:
            from crewai import LLM, Agent, Crew, Process, Task
        except ImportError as e:
            yield ev.make_event(ev.ERROR, {"message": f"CrewAI 导入失败: {e}"})
            return

        event_q: queue.Queue = queue.Queue()
        counter = itertools.count(1)
        crew_tools = self._adapt_tools(event_q, counter)

        cfg = self.deps.llm
        extra = cfg.reasoning_extra() if hasattr(cfg, "reasoning_extra") else {}
        llm_kwargs: Dict[str, Any] = dict(
            model=f"openai/{cfg.model}",
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            temperature=cfg.temperature,
        )
        if extra:
            llm_kwargs["extra_body"] = extra
        llm = LLM(**llm_kwargs)

        user_text = next((m["content"] for m in reversed(history)
                          if m["role"] == "user"), "")
        task_labels: List[str] = ["需求分析", "编码实现", "测试验证"]

        analyst = Agent(
            role="需求分析师", goal="理解并拆解编码需求，形成清晰实施步骤",
            backstory="你是资深技术负责人，善于把模糊需求拆解为可执行步骤。",
            llm=llm, allow_delegation=False, max_iter=3,
        )
        engineer = Agent(
            role="工程师", goal="使用工具完成编码并验证结果",
            backstory=self.deps.system_prompt,
            llm=llm, tools=crew_tools, allow_delegation=False,
            max_iter=self.deps.max_iterations,
        )
        reviewer = Agent(
            role="评审员", goal="审查实现的正确性、安全性并给出结论",
            backstory="你是严格的代码评审专家，用中文给出最终结论。",
            llm=llm, allow_delegation=False, max_iter=3,
        )

        # 按 task_labels 动态编排：首环节分析、末环节评审、中间环节全部交给工程师
        tasks: List[Any] = []
        for i, label in enumerate(task_labels):
            if i == 0:
                owner = analyst
            elif i == len(task_labels) - 1:
                owner = reviewer
            else:
                owner = engineer
            tasks.append(Task(
                description=f"【{label}】针对以下编码需求完成本环节工作: {user_text}",
                expected_output="中文输出，包含本环节的关键过程与结论。",
                agent=owner,
                context=tasks[:] if tasks else None,
            ))

        crew = Crew(
            agents=[analyst, engineer, reviewer],
            tasks=tasks,
            process=Process.sequential,
            verbose=False,
        )

        # 同步 kickoff 放入线程；主线程轮询队列实时转发工具事件
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, crew.kickoff)
        while not future.done():
            try:
                kind, cid, tname, payload = event_q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1)
                continue
            if kind == "call":
                yield ev.make_event(ev.TOOL_CALL, {
                    "id": cid, "name": tname, "arguments": payload})
            else:
                yield ev.make_event(ev.TOOL_RESULT, {
                    "id": cid, "name": tname, "output": str(payload),
                    "is_error": str(payload).startswith("[工具错误]")})

        try:
            result = await future
        except Exception as e:  # noqa: BLE001
            yield ev.make_event(ev.ERROR, {
                "message": f"CrewAI 执行失败: {type(e).__name__}: {e}"})
            return

        # 排空残余事件
        while not event_q.empty():
            kind, cid, tname, payload = event_q.get_nowait()
            etype = ev.TOOL_CALL if kind == "call" else ev.TOOL_RESULT
            yield ev.make_event(etype, {
                "id": cid, "name": tname,
                "arguments": payload if kind == "call" else None,
                "output": str(payload) if kind == "result" else None,
                "is_error": kind == "result" and str(payload).startswith("[工具错误]"),
            })

        final_text = getattr(result, "raw", None) or str(result)
        yield ev.make_event(ev.TOKEN, {"content": str(final_text)})
        yield ev.make_event(ev.DONE, {"finish_reason": "stop"})
