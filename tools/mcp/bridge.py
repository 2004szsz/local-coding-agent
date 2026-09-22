# -*- coding: utf-8 -*-
"""
MCP 工具桥接：把 MCP server 的工具变成 `tools.base.Tool`。

**「原生支持 MCP」的定义就是这个文件。** 桥接之后，MCP 工具与内置工具**零差别**：

    同一个 ToolRegistry  →  同一套 authorize 权限管线  →  同一套 tool_call/tool_result 事件
                        →  同一个 output_limit 截断    →  同一个观察环折叠策略

反过来也成立：MCP **不构成第二套循环**。它没有自己的重试策略、没有自己的停止条件，
它产生的失败和内置工具一样走 `repair` 分类。

命名规则 `mcp_<server>_<tool>`：避免与内置工具撞名；`server` 与 `tool` 都做字符规范化，
因为 MCP 允许工具名里出现点号、连字符等函数名不允许的字符。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Sequence

from ..base import Tool, ToolRegistry, ToolResult
from .client import McpClient

__all__ = ["tool_name", "register_mcp_tools", "is_read_only"]

_NAME_SAFE = re.compile(r"[^a-z0-9_]+")
_MAX_NAME_LENGTH = 60
#: 结构化结果转成文本时的字符上限（Tool.output_limit 会再截一次）
_STRUCTURED_TEXT_LIMIT = 6000


def tool_name(server: str, tool: str) -> str:
    """构造注册用的工具名。超长时截断并保留尾部哈希，避免两个长名撞成一个。"""
    def clean(text: str) -> str:
        return _NAME_SAFE.sub("_", str(text).lower()).strip("_")

    name = f"mcp_{clean(server)}_{clean(tool)}" or "mcp_tool"
    if len(name) > _MAX_NAME_LENGTH:
        name = f"{name[:_MAX_NAME_LENGTH - 9]}_{abs(hash(name)) % 0xFFFFFF:06x}"
    return name


def is_read_only(spec: Dict[str, Any], server_read_only: bool) -> bool:
    """
    判定一个 MCP 工具是否只读。

    只认两个来源：server 级配置，或 MCP 官方 annotations 里的 `readOnlyHint`。
    **绝不根据 description 的文字猜**——「查询」「获取」这类词出现在一个实际会
    创建资源的工具描述里是完全可能的，猜错的代价是外部状态被改。
    """
    if server_read_only:
        return True
    annotations = spec.get("annotations")
    if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True:
        return True
    return False


def register_mcp_tools(registry: ToolRegistry, client: McpClient,
                       previously: Iterable[str] = ()) -> List[str]:
    """
    把某个 client 的工具注册进注册表。

    :param previously: 上一次注册的工具名。本次不再出现的会被**注销**——
                       server 撤下工具后，注册表里留着一个调不通的幽灵工具比没有更糟。
    :return: 本次注册的工具名（顺序与 server 返回一致）
    """
    specs = client.list_tools()
    registered: List[str] = []
    collided: List[str] = []

    for spec in specs:
        if not isinstance(spec, dict):
            continue
        raw_name = str(spec.get("name") or "").strip()
        if not raw_name:
            continue
        name = tool_name(client.name, raw_name)
        if name in registered:
            collided.append(name)
            continue
        registry.replace(build_mcp_tool(client, spec, name, raw_name))
        registered.append(name)

    for stale in set(previously) - set(registered):
        registry.unregister(stale)

    if collided:
        print(f"[MCP] 警告: {client.name} 有工具名规范化后冲突，已跳过: {', '.join(collided)}")
    return registered


def build_mcp_tool(client: McpClient, spec: Dict[str, Any],
                   name: str, remote_name: str) -> Tool:
    """把一个 MCP 工具描述转成 `Tool`。"""

    def handler(kwargs: Dict[str, Any]) -> ToolResult:
        result = client.call_tool(remote_name, kwargs)
        text = _content_text(result)
        structured = result.get("structuredContent")
        artifacts: Dict[str, Any] = {}
        if structured is not None:
            artifacts["structured"] = structured
            if not text:
                text = json.dumps(structured, ensure_ascii=False, default=str)
        if len(text) > _STRUCTURED_TEXT_LIMIT:
            text = text[:_STRUCTURED_TEXT_LIMIT] + "\n...[MCP 结果过长已截断]"
        return ToolResult(
            text=text or f"(MCP 工具 {remote_name} 没有返回文本内容)",
            artifacts=artifacts,
        )

    description = str(spec.get("description") or "").strip()
    read_only = is_read_only(spec, client.config.read_only)
    header = (f"[MCP:{client.name}] " + (description or f"远程工具 {remote_name}"))
    if read_only:
        header += "（该工具声明为只读）"

    schema = spec.get("inputSchema")
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    # 只读声明同时写进 Tool.read_only：permissions.classify 对非内置工具看的就是它
    return Tool(
        name=name,
        description=header,
        parameters=schema,
        handler=handler,
        read_only=read_only,
    )


def _content_text(result: Dict[str, Any]) -> str:
    """把 MCP 的 content 数组拼成给模型看的文本。非 text 类型（图片等）只留一行占位。"""
    chunks: List[str] = []
    for item in (result.get("content") or []):
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            chunks.append(str(item.get("text") or ""))
        elif kind:
            chunks.append(f"[{kind} 内容，本地运行时暂不支持渲染]")
    return "\n".join(chunk for chunk in chunks if chunk).strip()
