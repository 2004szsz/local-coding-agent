# -*- coding: utf-8 -*-
"""
AutoGen（微软多智能体框架）适配器 —— 可选依赖。

启用方式：
    pip install "autogen-agentchat>=0.4.0" "autogen-ext[openai]>=0.4.0"
    config.yaml: agent.framework: "autogen"

实现「架构师 -> 工程师（持工具）-> 评审员」的多角色顺序协作团队，
模型接入使用 OpenAI 兼容客户端（Ollama / GLM / OpenAI 均可），
工具由统一 ToolRegistry 动态适配为 AutoGen FunctionTool。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List

from . import events as ev
from .base import AgentDependencies, BaseAgent
from .registry import OptionalFrameworkMissingError


def _pydantic_schema(parameters: Dict[str, Any], model_name: str):
    """把工具的 JSON Schema 转为 Pydantic 模型（AutoGen 参数声明用）。"""
    from pydantic import create_model
    props = parameters.get("properties", {}) or {}
    required = set(parameters.get("required", []) or [])
    fields = {
        key: (Any, ... if key in required else None)
        for key in props
    }
    return create_model(model_name, **fields)


class AutoGenTeamAgent(BaseAgent):
    framework_name = "autogen"

    def __init__(self, deps: AgentDependencies):
        super().__init__(deps)
        # 实例化时即校验依赖，缺失时给出明确安装命令
        try:
            from autogen_agentchat.agents import AssistantAgent  # noqa: F401
            from autogen_agentchat.teams import RoundRobinGroupChat  # noqa: F401
        except ImportError as e:  # pragma: no cover - 取决于用户环境
            raise OptionalFrameworkMissingError(
                'AutoGen 依赖未安装，请执行: '
                'pip install "autogen-agentchat>=0.4.0" "autogen-ext[openai]>=0.4.0"'
            ) from e

    def _adapt_tools(self) -> List[Any]:
        """把 ToolRegistry 中的工具适配为 AutoGen FunctionTool 列表。"""
        from autogen_core.tools import FunctionTool

        result: List[Any] = []
        for name in self.deps.tools.names():
            tool = self.deps.tools.get(name)

            def _make_handler(tool_name=tool.name, deps=self.deps):
                def _handler(**kwargs) -> str:
                    from .gate import execute_gated_sync
                    return execute_gated_sync(deps, tool_name, kwargs)
                return _handler

            result.append(FunctionTool(
                _make_handler(),
                name=tool.name,
                description=tool.description,
                parameters_model=_pydantic_schema(
                    tool.parameters, f"{tool.name}_params"),
            ))
        return result

    async def astream_run(self, history: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        try:
            from autogen_agentchat.agents import AssistantAgent
            from autogen_agentchat.conditions import MaxMessageTermination
            from autogen_agentchat.teams import RoundRobinGroupChat
            from autogen_ext.models.openai import OpenAIChatCompletionClient
        except ImportError as e:
            yield ev.make_event(ev.ERROR, {"message": f"AutoGen 导入失败: {e}"})
            return

        llm_cfg = self.deps.llm
        create_args = llm_cfg.reasoning_extra() if hasattr(llm_cfg, "reasoning_extra") else {}
        client = OpenAIChatCompletionClient(
            model=llm_cfg.model,
            base_url=llm_cfg.base_url,
            api_key=llm_cfg.api_key,
            temperature=llm_cfg.temperature,
            create_args=create_args,
        )
        coding_tools = self._adapt_tools()
        max_turns = max(3, self.deps.max_iterations)

        # 多角色团队（角色配置来自 config.yaml -> frameworks.autogen.roles）
        architect = AssistantAgent(
            name="Architect",
            model_client=client,
            system_message="你是架构师：分析需求、给出简洁实现方案与任务拆解，用中文回复。",
        )
        engineer = AssistantAgent(
            name="Engineer",
            model_client=client,
            tools=coding_tools,
            system_message=self.deps.system_prompt
            + " 你负责实际编码，必须使用提供的工具操作文件与验证代码。",
        )
        reviewer = AssistantAgent(
            name="Reviewer",
            model_client=client,
            system_message="你是代码评审员：检查正确性与安全性，给出最终结论，用中文回复。",
        )

        team = RoundRobinGroupChat(
            participants=[architect, engineer, reviewer],
            termination_condition=MaxMessageTermination(max_messages=max_turns),
        )

        # 只把最后一条用户诉求作为团队任务（历史由调用方保证相关性）
        task = next((m["content"] for m in reversed(history)
                     if m["role"] == "user"), "")
        final_text = ""
        try:
            async for event in team.run_stream(task=task):
                for msg in getattr(event, "messages", []) or []:
                    async for out in self._emit_message(msg):
                        yield out
                    # 记录最后一条文本消息作为最终答复
                    txt = getattr(msg, "content", None)
                    if isinstance(txt, str) and txt.strip():
                        final_text = txt
        except Exception as e:  # noqa: BLE001
            yield ev.make_event(ev.ERROR, {"message": f"AutoGen 执行失败: {type(e).__name__}: {e}"})
            return
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

        # 团队内部讨论已作为文本事件下发，这里补发收尾
        if final_text:
            yield ev.make_event(ev.TOKEN, {"content": "\n\n" + final_text})
        yield ev.make_event(ev.DONE, {"finish_reason": "stop"})

    async def _emit_message(self, msg: Any):
        """将 AutoGen 消息块转译为统一事件（按类名防御式识别，兼容小版本差异）。"""
        content = getattr(msg, "content", None)
        source = getattr(msg, "source", "agent")
        blocks = content if isinstance(content, list) else [content]

        for block in blocks:
            cls = type(block).__name__
            if cls == "FunctionCallContent":
                raw_args = getattr(block, "arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {"raw": str(raw_args)}
                yield ev.make_event(ev.TOOL_CALL, {
                    "id": getattr(block, "id", "call"),
                    "name": getattr(block, "name", "unknown"),
                    "arguments": args,
                })
            elif cls == "FunctionExecutionResultContent":
                result = getattr(block, "content", "")
                yield ev.make_event(ev.TOOL_RESULT, {
                    "id": getattr(block, "call_id", "call"),
                    "name": source,
                    "output": str(result),
                    "is_error": bool(getattr(block, "is_error", False)),
                })
            elif isinstance(block, str) and block.strip():
                # 角色发言以 thought 流形式展示团队协作过程
                yield ev.make_event(ev.THOUGHT, {
                    "content": f"[{source}] {block[:1000]}"})
