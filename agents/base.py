# -*- coding: utf-8 -*-
"""
Agent 统一抽象。

无论底层使用哪个框架（原生 ReAct / 自研 state_loop / AutoGen / ...），
对外都只暴露同一个异步流式接口 astream_run()，并产出统一的 SSE 事件
（事件定义见 agents.events）。工具集通过 ToolRegistry 注入，框架间完全复用。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List

from tools.api_client import LLMClient
from tools.base import ToolRegistry


@dataclass
class AgentDependencies:
    """
    所有 Agent 运行所需的统一依赖容器。

    前四个字段是所有框架共用的最小集。`loop_deps` 是自研 state_loop 运行时的扩展依赖
    （见 agents/state_loop/runtime.LoopDeps）：显式可选，其他框架不看它。
    用一个槽位承载整包扩展依赖，而不是每加一个能力就往这里堆字段，
    这样新增模块不必改动本文件，也不会破坏既有框架的构造调用。
    """

    llm: LLMClient
    tools: ToolRegistry
    system_prompt: str
    max_iterations: int = 12
    loop_deps: Any = None


class BaseAgent(ABC):
    """Agent 协议基类。"""

    #: 框架标识（子类覆盖）
    framework_name: str = "base"

    def __init__(self, deps: AgentDependencies):
        self.deps = deps

    def build_messages(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """在对话历史前注入 system 消息。"""
        return [{"role": "system", "content": self.deps.system_prompt}] + history

    @abstractmethod
    def astream_run(self, history: List[Dict[str, Any]]) -> AsyncIterator[Dict[str, Any]]:
        """
        执行一轮 Agent 循环（含可能的多轮工具调用）。

        :param history: OpenAI 格式对话历史（不含 system，由本方法注入）
        :yield: agents.events 中定义的统一事件 dict
        """
        raise NotImplementedError
