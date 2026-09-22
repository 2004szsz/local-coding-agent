# -*- coding: utf-8 -*-
"""
MCP 原生扩展。

四个文件，职责互不重叠：

    config.py  把 config.yaml 的 mcp.servers 解析成配置对象（逐条降级）
    client.py  JSON-RPC 协议与两种传输（stdio / streamable_http）
    bridge.py  把 MCP 工具桥接成 tools.base.Tool（「原生」体现在这里）
    manager.py 连接生命周期与按需刷新
"""
from __future__ import annotations

from .bridge import register_mcp_tools, tool_name
from .client import McpClient
from .config import McpServerConfig, load_servers
from .manager import McpManager

__all__ = [
    "McpClient", "McpManager", "McpServerConfig", "load_servers",
    "register_mcp_tools", "tool_name",
]
