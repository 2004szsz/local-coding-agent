# -*- coding: utf-8 -*-
"""
工具统一抽象层。

设计目标：
- 工具定义与 Agent 框架完全无关，一次注册、多个框架共用；
- 同时提供 OpenAI Function Calling 所需的 JSON Schema；
- 执行异常被捕获并转成结构化结果返回，避免单次工具报错中断整个循环。

执行结果的两种视图（同一份数据，两种消费方式）：

    ToolRegistry.call(name, args) -> ToolResult   # 结构化：ok / error_type / artifacts / text
    ToolRegistry.execute(name, args) -> str       # 旧视图：只取正文，错误表示为 '[工具错误] ...'

`execute` 只是 `call` 的正文投影，两者共用同一套参数归一化、截断与日志逻辑。
新代码（如自研 state_loop 运行时）应使用 `call`，它才能区分「参数错误 / 越界 / 超时 / 命令退出码非 0」，
而 `execute` 的字符串前缀无法承载这些信息。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Union

from .debug_log import log_tool_call

# 返回给模型的单条结果最大长度（超出截断，防止撑爆上下文）
DEFAULT_OUTPUT_LIMIT = 12000

# 工具错误文本的统一前缀。旧框架靠它判断失败，务必保持不变。
ERROR_PREFIX = "[工具错误]"


@dataclass
class ToolResult:
    """
    工具执行的完整结果。

    :param text:      给模型阅读的正文（会被 tool.output_limit 截断）
    :param artifacts: 机器字段，**不进模型上下文**：exit_code / tail / changed_paths / diagnostic_count ...
    :param ok:        是否成功
    :param error_type: 失败时的异常类名或稳定错误标识（供上层做失败分类，不要放人类可读描述）
    :param error_message: 失败描述
    """

    text: str = ""
    artifacts: Dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    error_type: str = ""
    error_message: str = ""

    @classmethod
    def failure(cls, text: str, error_type: str,
                error_message: str = "", artifacts: Dict[str, Any] | None = None) -> "ToolResult":
        """构造失败结果。text 缺省时用 error_message 兜底，保证有内容可回给模型。"""
        return cls(
            text=text or error_message,
            artifacts=dict(artifacts or {}),
            ok=False,
            error_type=error_type or "Error",
            error_message=error_message or text,
        )


#: 工具处理函数：接收参数 dict，返回正文文本，或返回结构化 ToolResult
ToolHandler = Callable[[Dict[str, Any]], Union[str, ToolResult]]


def truncate_output(text: str, limit: int = DEFAULT_OUTPUT_LIMIT) -> str:
    """截断过长的工具输出并附加提示。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (
        text[:half]
        + f"\n\n...[结果过长，已截断，共 {len(text)} 字符]...\n\n"
        + text[-half:]
    )


@dataclass
class Tool:
    """一个可被 LLM 调用的工具。"""

    name: str                                       # 工具名（英文、蛇形命名）
    description: str                                # 给模型看的用途说明
    parameters: Dict[str, Any]                      # JSON Schema（入参定义）
    handler: ToolHandler                            # 实际执行函数
    output_limit: int = DEFAULT_OUTPUT_LIMIT
    #: 副作用声明。**不作为权限判定的权威来源**（权威在 agents/state_loop/permissions.py），
    #: 只用于「内置表里没有的第三方工具」（MCP 桥接进来的工具）：
    #: True 表示服务端声明只读，可归入 L0。
    read_only: bool = False

    def openai_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI / Ollama 兼容的 function tool 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolRegistry:
    """工具注册表：框架通过它拿到 schema 并执行调用。"""

    _tools: Dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """移除工具。供 MCP 工具热刷新使用；不存在时返回 False，不抛异常。"""
        return self._tools.pop(name, None) is not None

    def replace(self, tool: Tool) -> None:
        """注册或覆盖（MCP 刷新导致 schema 变化时使用）。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"工具不存在: {name}（可用工具: {', '.join(self.names())}）")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def openai_tools_specs(self) -> List[Dict[str, Any]]:
        """返回 chat.completions 接口 tools 参数所需的完整 schema 列表。"""
        return [t.openai_schema() for t in self._tools.values()]

    def openai_specs_for(self, names: List[str]) -> List[Dict[str, Any]]:
        """按名字子集取 schema（顺序按 names）。缺失的名字静默跳过，由调用方先行校验。"""
        specs: List[Dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is not None:
                specs.append(tool.openai_schema())
        return specs

    # ---------------- 参数归一化（call / execute 共用） ----------------
    @staticmethod
    def _normalize_arguments(name: str, arguments: Dict[str, Any] | str | None) -> tuple[Dict[str, Any], ToolResult | None]:
        """
        把模型给出的参数归一化成 dict。

        :return: (kwargs, failure)。failure 非 None 时 kwargs 无意义。
        """
        if isinstance(arguments, str) and arguments.strip():
            try:
                return json.loads(arguments), None
            except json.JSONDecodeError:
                return {}, ToolResult.failure(
                    f"{ERROR_PREFIX} {name} 的参数不是合法 JSON: {arguments[:200]}",
                    "ArgError", f"参数不是合法 JSON: {arguments[:200]}")
        if isinstance(arguments, dict):
            return arguments, None
        if arguments is None:
            return {}, None
        return {}, ToolResult.failure(
            f"{ERROR_PREFIX} {name} 的参数类型不支持: {type(arguments).__name__}",
            "ArgError", f"参数类型不支持: {type(arguments).__name__}")

    def call(self, name: str, arguments: Dict[str, Any] | str | None) -> ToolResult:
        """
        执行工具，返回结构化结果。**任何异常都不抛出**。

        失败时的 error_type 取值：
            NotFound       工具未注册
            ArgError       参数不是合法 JSON / 类型不支持
            <异常类名>      handler 抛出的异常类型（如 ValueError、FileNotFoundError、
                            PathTraversalError、TimeoutExpired）——供上层做失败分类
        """
        try:
            tool = self.get(name)
        except KeyError as e:
            message = str(e)
            result = ToolResult.failure(f"{ERROR_PREFIX} {message}", "NotFound", message)
            log_tool_call(name, arguments, result.text)
            return result

        kwargs, failure = self._normalize_arguments(name, arguments)
        if failure is not None:
            log_tool_call(name, arguments, failure.text)
            return failure

        try:
            raw = tool.handler(kwargs)
            if isinstance(raw, ToolResult):
                result = raw
            elif isinstance(raw, str):
                result = ToolResult(text=raw)
            else:
                # handler 返回普通对象（dict/list）时序列化，便于模型阅读
                result = ToolResult(text=json.dumps(raw, ensure_ascii=False, default=str))
            result.text = truncate_output(result.text, tool.output_limit)
        except Exception as e:  # noqa: BLE001 - 工具层必须兜底，返回错误供上层分类与自纠
            text = f"{ERROR_PREFIX} {type(e).__name__}: {e}"
            result = ToolResult.failure(text, type(e).__name__, str(e))

        log_tool_call(name, kwargs, result.text)
        return result

    def execute(self, name: str, arguments: Dict[str, Any] | str | None) -> str:
        """
        执行工具并只返回正文文本（旧框架兼容视图）。

        异常被转换为 '[工具错误] ...' 文本（不抛出）。
        """
        return self.call(name, arguments).text
