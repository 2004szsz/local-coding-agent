# -*- coding: utf-8 -*-
"""
MCP 连接管理：连接、注册、刷新、关闭。

生命周期的失败策略与现有 RAG 初始化**完全一致**：某一个 server 连不上，
把它标成 `down`、不注册它的工具、打一行警告，对话照常继续。

    「可选增强不得成为主链路的单点故障」——这条在 RAG 上已经成立，
    在 MCP 上没有理由不成立。

刷新走轮询而不是事件回调：stdio 的读线程已经把 `notifications/*` 排进队列了，
这里在每次「准备模型请求」前顺手取一次即可，不需要额外线程。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .bridge import register_mcp_tools
from .client import McpClient
from .config import McpServerConfig

__all__ = ["McpManager"]

#: 触发重新拉工具表的通知
_TOOLS_CHANGED = "notifications/tools/list_changed"


class McpManager:
    """所有 MCP 连接的持有者。进程级单例，绑在 Runtime 生命周期上。"""

    def __init__(self, configs: Sequence[McpServerConfig], warn=print):
        self._configs = list(configs)
        self._clients: Dict[str, McpClient] = {}
        self._registered: Dict[str, List[str]] = {}
        self._warn = warn

    # ---------------- 生命周期 ----------------
    def connect_all(self) -> List[str]:
        """
        逐个连接。**单个失败不影响其他 server，也不抛出。**

        :return: 成功连上的 server 名
        """
        ready: List[str] = []
        for config in self._configs:
            client = McpClient(config)
            try:
                client.start()
            except Exception as e:  # noqa: BLE001 - 连接失败是预期情况，不是异常情况
                client.error = f"{type(e).__name__}: {e}"
                client.close()
                self._warn(f"[MCP] {config.name} 连接失败，已跳过: {client.error}")
                continue
            self._clients[config.name] = client
            ready.append(config.name)
            info = client.server_info.get("name") or "unknown"
            self._warn(f"[MCP] {config.name} 已连接（server: {info}，"
                       f"协议 {client.protocol_version or '未知'}）")
        return ready

    def register_all(self, registry) -> List[str]:
        """把所有已连接 server 的工具注册进注册表，返回工具名列表。"""
        names: List[str] = []
        for name, client in self._clients.items():
            names.extend(self._register_one(registry, client))
        return names

    def refresh(self, registry) -> List[str]:
        """
        按需刷新：只有收到 `tools/list_changed` 的 server 才重新拉工具表。

        没收到通知就不动——无条件重拉会让每个用户回合都多出 N 次网络/进程往返。
        """
        touched: List[str] = []
        for name, client in self._clients.items():
            try:
                notifications = client.poll_notifications()
            except Exception as e:  # noqa: BLE001
                self._warn(f"[MCP] {name} 读取通知失败: {e}")
                continue
            if not any(str(item.get("method")) == _TOOLS_CHANGED for item in notifications):
                continue
            touched.extend(self._register_one(registry, client))
        return touched

    def close(self) -> None:
        """进程退出时关闭全部连接。关闭错误一律忽略。"""
        for client in self._clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        self._registered.clear()

    # ---------------- 查询 ----------------
    @property
    def ready_names(self) -> List[str]:
        return [name for name, client in self._clients.items() if client.ready]

    def status(self) -> List[Dict[str, Any]]:
        """给 /api/health 用的状态摘要。"""
        items: List[Dict[str, Any]] = []
        for name, client in self._clients.items():
            items.append({
                "name": name,
                "transport": client.config.transport,
                "status": client.status,
                "tools": len(self._registered.get(name, [])),
                "server": client.server_info.get("name", ""),
                "protocol": client.protocol_version,
            })
        for config in self._configs:
            if config.name not in self._clients:
                items.append({"name": config.name, "transport": config.transport,
                              "status": "down", "tools": 0, "server": "", "protocol": ""})
        return items

    def tool_names(self) -> List[str]:
        return [name for names in self._registered.values() for name in names]

    # ---------------- 内部 ----------------
    def _register_one(self, registry, client: McpClient) -> List[str]:
        previous = self._registered.get(client.name, [])
        try:
            names = register_mcp_tools(registry, client, previously=previous)
        except Exception as e:  # noqa: BLE001 - 工具表拉取失败要标 down 而不是崩掉
            self._warn(f"[MCP] {client.name} 拉取工具表失败: {type(e).__name__}: {e}")
            client.ready = False
            client.error = str(e)
            return previous
        self._registered[client.name] = names
        return names
