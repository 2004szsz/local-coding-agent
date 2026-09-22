# -*- coding: utf-8 -*-
"""
Agent 框架注册工厂（配置驱动）。

通过 agents/agent.yaml -> framework 选择实现（环境变量 AGENT_FRAMEWORK 可临时覆盖）：
    state_loop    -> StateLoopAgent（自研状态驱动主循环，需装配 LoopDeps）
    native_react  -> NativeReActAgent（零额外依赖的手写 ReAct）
    autogen       -> AutoGenTeamAgent（pip install autogen-agentchat autogen-ext[openai]）
    llamaindex    -> LlamaIndexAgent（pip install llama-index llama-index-llms-openai-like）
    crewai        -> CrewAICodingCrew（pip install crewai）

langgraph 已于 2026-09-21 移除，控制面改由自研的 state_loop 运行时接管，
设计见 docs/agent-runtime-architecture.md。

所有实现共享同一套 ToolRegistry，切换框架无需改动任何工具代码。
"""
from __future__ import annotations

from typing import Dict

from .base import AgentDependencies, BaseAgent
from .native_react import NativeReActAgent
from .state_loop import StateLoopAgent

#: 默认框架。
#: 刻意保守地取 native_react：它零额外依赖，且在「模型不服从强制函数调用」时
#: 仍有一套文本动作兜底，是任何环境下都必然可用的实现。
#: 把默认切到 state_loop 是一次**有意的运维决定**，前提是确认所用模型能稳定产出
#: submit_decision（见 docs/agent-runtime-architecture.md 第 13.5 节）。
DEFAULT_FRAMEWORK = "native_react"

#: 可选框架 -> 缺失依赖时的安装提示
INSTALL_HINTS: Dict[str, str] = {
    "autogen": "pip install \"autogen-agentchat>=0.4.0\" \"autogen-ext[openai]>=0.4.0\"",
    "llamaindex": "pip install llama-index llama-index-llms-openai-like",
    "crewai": "pip install crewai",
}

#: 已从本项目移除、但配置里可能残留的框架名 -> 提示
REMOVED_FRAMEWORKS: Dict[str, str] = {
    "langgraph": (
        "langgraph 已从本项目移除。控制面由自研 state_loop 运行时接管"
        "（见 docs/agent-runtime-architecture.md）；"
        f"请改用 '{DEFAULT_FRAMEWORK}' 或 'state_loop'。"
    ),
}

#: 可选框架标识 -> (模块名, 类名)
_OPTIONAL = {
    "autogen": ("agents.autogen_agent", "AutoGenTeamAgent"),
    "llamaindex": ("agents.llamaindex_agent", "LlamaIndexAgent"),
    "crewai": ("agents.crewai_agent", "CrewAICodingCrew"),
}

_CORE = {
    "state_loop": StateLoopAgent,
    "native_react": NativeReActAgent,
}


class UnknownFrameworkError(ValueError):
    """配置了不支持的框架名。"""


class OptionalFrameworkMissingError(RuntimeError):
    """选择了可选框架但其依赖尚未安装。"""


def supported_frameworks() -> list[str]:
    return list(_CORE.keys()) + list(_OPTIONAL.keys())


def _explain(framework: str) -> str:
    """为已移除的框架名补一句可执行的说明。"""
    hint = REMOVED_FRAMEWORKS.get(framework)
    return f"\n{hint}" if hint else ""


def build_agent(framework: str, deps: AgentDependencies) -> BaseAgent:
    """
    按名称构造 Agent 实例。

    :raises UnknownFrameworkError: 框架名无法识别（含已移除 / 未实现的提示）
    :raises OptionalFrameworkMissingError: 可选框架依赖缺失（异常消息含安装命令）
    """
    framework = (framework or DEFAULT_FRAMEWORK).strip().lower()

    if framework in _CORE:
        return _CORE[framework](deps)

    if framework not in _OPTIONAL:
        raise UnknownFrameworkError(
            f"未知 Agent 框架: '{framework}'，可选值: {', '.join(supported_frameworks())}"
            + _explain(framework)
        )

    import importlib
    module_name, class_name = _OPTIONAL[framework]
    try:
        module = importlib.import_module(module_name)
        agent_cls = getattr(module, class_name)
    except ImportError as e:
        raise OptionalFrameworkMissingError(
            f"框架 '{framework}' 的可选依赖未安装或导入失败: {e}\n"
            f"请先执行安装命令: {INSTALL_HINTS[framework]}"
        ) from e
    return agent_cls(deps)
