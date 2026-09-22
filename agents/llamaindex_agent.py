# -*- coding: utf-8 -*-
"""
LlamaIndex 适配器 —— 可选依赖，主打「基于代码结构的 RAG 智能检索 + Function Agent」。

启用方式：
    pip install llama-index llama-index-llms-openai-like
    config.yaml: agent.framework: "llamaindex"

说明：
- LLM 使用 OpenAILike（任意 OpenAI 兼容端点，含 Ollama / GLM）；
- 统一工具集适配为 LlamaIndex FunctionTool（从 JSON Schema 生成参数模型）；
- 优先使用新版 workflow 的 FunctionAgent（带工具事件流），
  旧版本自动回退到 ReActAgent。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List

from . import events as ev
from .base import AgentDependencies, BaseAgent
from .registry import OptionalFrameworkMissingError


def _pydantic_schema(parameters: Dict[str, Any], model_name: str):
    """JSON Schema -> Pydantic 参数模型。"""
    from pydantic import create_model
    props = parameters.get("properties", {}) or {}
    required = set(parameters.get("required", []) or [])
    fields = {key: (Any, ... if key in required else None) for key in props}
    return create_model(model_name, **fields)


class LlamaIndexAgent(BaseAgent):
    framework_name = "llamaindex"

    def __init__(self, deps: AgentDependencies):
        super().__init__(deps)
        try:
            import llama_index  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise OptionalFrameworkMissingError(
                "LlamaIndex 依赖未安装，请执行: "
                "pip install llama-index llama-index-llms-openai-like"
            ) from e

    # ---------------- 工具适配 ----------------
    def _adapt_tools(self) -> List[Any]:
        from llama_index.core.tools import FunctionTool

        tools: List[Any] = []
        for name in self.deps.tools.names():
            tool = self.deps.tools.get(name)

            def _make_fn(tool_name=tool.name, deps=self.deps):
                def _fn(**kwargs) -> str:
                    from .gate import execute_gated_sync
                    return execute_gated_sync(deps, tool_name, kwargs)
                return _fn

            tools.append(FunctionTool.from_defaults(
                fn=_make_fn(),
                name=tool.name,
                description=tool.description,
                fn_schema=_pydantic_schema(tool.parameters, f"{tool.name}_params"),
            ))
        return tools

    def _build_llm(self) -> Any:
        from llama_index.llms.openai_like import OpenAILike
        cfg = self.deps.llm
        kwargs: Dict[str, Any] = dict(
            model=cfg.model,
            api_base=cfg.base_url,
            api_key=cfg.api_key,
            is_chat_model=True,
            is_function_calling_model=True,
            context_window=32000,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
            timeout=cfg.timeout,
        )
        extra = cfg.reasoning_extra() if hasattr(cfg, "reasoning_extra") else {}
        if extra:
            kwargs["additional_kwargs"] = extra
        return OpenAILike(**kwargs)

    # ---------------- 主流程 ----------------
    async def astream_run(self, history: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        try:
            from llama_index.core.llms import ChatMessage
        except ImportError as e:
            yield ev.make_event(ev.ERROR, {"message": f"LlamaIndex 导入失败: {e}"})
            return

        last_user = next((m["content"] for m in reversed(history)
                          if m["role"] == "user"), "")
        # 历史消息转换（tool 角色折叠为 assistant 文本，避免协议不兼容）
        chat_history: List[Any] = []
        for m in history[:-1]:
            if m["role"] in ("user", "assistant") and m.get("content"):
                chat_history.append(ChatMessage(role=m["role"], content=m["content"]))
            elif m["role"] == "tool":
                chat_history.append(ChatMessage(
                    role="assistant",
                    content=f"[工具 {m.get('name', '')} 返回] {m.get('content', '')[:500]}"))

        try:
            async for out in self._run_workflow_agent(last_user, chat_history):
                yield out
        except ImportError:
            # 旧版回退：ReActAgent
            async for out in self._run_react_agent(last_user):
                yield out
        except Exception as e:  # noqa: BLE001
            yield ev.make_event(ev.ERROR, {
                "message": f"LlamaIndex 执行失败: {type(e).__name__}: {e}"})

    async def _run_workflow_agent(self, user_text: str, chat_history: List[Any]):
        """新版 FunctionAgent（workflow）路径，支持工具事件实时流。"""
        from llama_index.core.agent.workflow import FunctionAgent

        llm = self._build_llm()
        agent = FunctionAgent(
            tools=self._adapt_tools(),
            llm=llm,
            system_prompt=self.deps.system_prompt,
        )
        handler = agent.run(user_text, chat_history=chat_history)
        streamed_chars = 0  # 记录是否已流式产出过文本，避免最终结果重复下发

        async for event in handler.stream_events():
            cls = type(event).__name__
            if cls == "ToolCall":
                yield ev.make_event(ev.TOOL_CALL, {
                    "id": getattr(event, "tool_id", "call"),
                    "name": getattr(event, "tool_name", "unknown"),
                    "arguments": getattr(event, "tool_kwargs", {}),
                })
            elif cls == "ToolResult":
                output = (getattr(event, "tool_output", None)
                          or getattr(event, "return_value", ""))
                text_out = output if isinstance(output, str) else json.dumps(
                    output, ensure_ascii=False, default=str)
                yield ev.make_event(ev.TOOL_RESULT, {
                    "id": getattr(event, "tool_id", "call"),
                    "name": getattr(event, "tool_name", "unknown"),
                    "output": text_out,
                    "is_error": False,
                })
            elif cls == "AgentStream":
                delta = getattr(event, "delta", "")
                if delta:
                    streamed_chars += len(delta)
                    yield ev.make_event(ev.TOKEN, {"content": delta})

        response = await handler
        text = getattr(response, "response", None) or str(response)
        # 若该版本不产生 AgentStream 增量事件，则补发一次完整文本
        if text and streamed_chars == 0:
            yield ev.make_event(ev.TOKEN, {"content": text})
        yield ev.make_event(ev.DONE, {"finish_reason": "stop"})

    async def _run_react_agent(self, user_text: str):
        """旧版 ReActAgent 回退路径（工具过程不细粒度展示）。"""
        from llama_index.core.agent import ReActAgent

        agent = ReActAgent.from_tools(
            self._adapt_tools(),
            llm=self._build_llm(),
            verbose=False,
            system_prompt=self.deps.system_prompt,
        )
        response = await agent.achat(user_text)
        text = getattr(response, "response", None) or str(response)
        yield ev.make_event(ev.TOKEN, {"content": text})
        yield ev.make_event(ev.DONE, {"finish_reason": "stop"})
