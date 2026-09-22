# -*- coding: utf-8 -*-
"""
MCP 客户端配置。

配置解析的策略是**逐条降级**：某一条 server 配置不合法，只跳过这一条并记一条警告，
不影响其他 server，更不阻止对话。这与现有 RAG 初始化的策略一致——
可选增强不得成为主链路的单点故障。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["McpServerConfig", "parse_server", "load_servers", "TRANSPORTS"]

#: server 名会进工具名（`mcp_<server>_<tool>`），因此必须严格受限
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TRANSPORTS = ("stdio", "streamable_http", "sse")
#: 已实现 vs 未实现要分清：写清楚比「静默失败」好得多
IMPLEMENTED_TRANSPORTS = ("stdio", "streamable_http")


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: Tuple[str, ...] = ()
    url: str = ""
    env: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    #: True 表示该 server 的全部工具按只读（L0）处理，不需要 annotations 佐证
    read_only: bool = False
    enabled: bool = True
    timeout_seconds: int = 30

    @property
    def is_local(self) -> bool:
        return self.transport == "stdio"

    def describe(self) -> str:
        target = " ".join(self.command) if self.is_local else self.url
        return f"{self.name}({self.transport}: {target})"


def _as_str_dict(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


def parse_server(raw: Any) -> Tuple[Optional[McpServerConfig], str]:
    """
    解析单条 server 配置。

    :return: `(config, "")` 或 `(None, 原因)`
    """
    if not isinstance(raw, dict):
        return None, f"配置项必须是对象，收到 {type(raw).__name__}"

    # 刻意**不做大小写归一化**：server 名会出现在工具名、日志与状态里，
    # 静默把 "Docs" 变成 "docs" 会让配置与运行时出现两个名字。
    # 严格拒绝 + 明确提示，比静默改写更可预测。
    name = str(raw.get("name") or "").strip()
    if not _NAME_PATTERN.match(name):
        return None, (f"name 非法: {name or '(空)'}；"
                      f"要求小写字母开头，仅含小写字母/数字/下划线（不要用大写或连字符），长度 ≤32")

    if raw.get("enabled") is False:
        return None, f"{name}: 已禁用"

    transport = str(raw.get("transport") or "stdio").strip().lower()
    if transport not in TRANSPORTS:
        return None, f"{name}: 不支持的 transport '{transport}'（可选 {', '.join(TRANSPORTS)}）"
    if transport not in IMPLEMENTED_TRANSPORTS:
        return None, (f"{name}: transport '{transport}' 尚未实现"
                      f"（已实现 {', '.join(IMPLEMENTED_TRANSPORTS)}）")

    command = raw.get("command") or ()
    url = str(raw.get("url") or "").strip()

    if transport == "stdio":
        if isinstance(command, str):
            return None, f"{name}: command 必须是数组，例如 [\"npx\", \"-y\", \"some-server\"]"
        command = tuple(str(part) for part in command if str(part).strip())
        if not command:
            return None, f"{name}: stdio 传输必须提供非空 command 数组"
    else:
        if not url:
            return None, f"{name}: {transport} 传输必须提供 url"

    try:
        timeout = int(raw.get("timeout_seconds", 30))
    except (TypeError, ValueError):
        timeout = 30

    return McpServerConfig(
        name=name,
        transport=transport,
        command=command,
        url=url,
        env=_as_str_dict(raw.get("env")),
        headers=_as_str_dict(raw.get("headers")),
        read_only=bool(raw.get("read_only", False)),
        enabled=True,
        timeout_seconds=max(5, min(timeout, 120)),
    ), ""


def load_servers(cfg: Dict[str, Any]) -> Tuple[List[McpServerConfig], List[str]]:
    """
    从整份配置里读出 MCP server 清单。

    :return: `(servers, warnings)`。warnings 会被打印出来——**跳过一条配置必须出声**，
             否则用户会以为「配了但没生效」是玄学。
    """
    section = (cfg or {}).get("mcp") or {}
    raw_list = section.get("servers") if isinstance(section, dict) else None
    if not raw_list:
        return [], []
    if not isinstance(raw_list, (list, tuple)):
        return [], ["mcp.servers 必须是数组，已忽略"]

    servers: List[McpServerConfig] = []
    warnings: List[str] = []
    seen: set[str] = set()

    for index, raw in enumerate(raw_list):
        config, reason = parse_server(raw)
        if config is None:
            warnings.append(f"mcp.servers[{index}] 已跳过：{reason}")
            continue
        if config.name in seen:
            warnings.append(f"mcp.servers[{index}] 已跳过：name 重复（{config.name}）")
            continue
        seen.add(config.name)
        servers.append(config)
    return servers, warnings
