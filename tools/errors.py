# -*- coding: utf-8 -*-
"""
工具层的领域异常类型。

**为什么需要这一层**：`ToolRegistry.call` 会把异常**类名**放进 `ToolResult.error_type`，
上层（`agents/state_loop/repair.py`）据此做失败分类——「补丁冲突要回滚」「命令失败要改代码」
「沙箱拒绝要换工具」是三种完全不同的修复动作。

如果工具只用 `ValueError` / `Exception` 表达失败，分类就只能去猜消息里的关键词，
那正是「失败不可分类」的根源：错误一旦不可分类，就不可修复。

约定（改代码时请遵守）：
1. 每个类都**继承对应的内置异常**，这样旧框架里 `except ValueError` 之类的分支依然成立；
2. **类名即失败类别，不要重命名**——分类表按类名匹配；
3. 抛异常时消息要能被人读懂（它会原样进模型上下文），但**分类不依赖消息内容**。
"""
from __future__ import annotations

__all__ = [
    "ToolError",
    "PatchConflictError",
    "CommandFailedError",
    "CommandTimeoutError",
    "SandboxViolationError",
    "McpToolError",
    "ScopeViolationError",
]


class ToolError(Exception):
    """工具层异常基类。仅用于「未归类的工具失败」的兜底类型。"""


class PatchConflictError(ToolError, ValueError):
    """
    `edit_file` 的定位串匹配失败：0 次（文件已变）或多次（不够唯一）。

    修复动作是**回滚到检查点再重新读文件**，而不是让模型继续对着旧内容打补丁。
    """


class CommandFailedError(ToolError):
    """工作区命令退出码非 0。"""


class CommandTimeoutError(ToolError, TimeoutError):
    """工作区命令超时并已被强杀。"""


class SandboxViolationError(ToolError, RuntimeError):
    """受限沙箱在编译期或运行期拒绝。修复动作是改用 `run_command`。"""


class McpToolError(ToolError, RuntimeError):
    """MCP 传输断开或 `tools/call` 报错。"""


class ScopeViolationError(ToolError, PermissionError):
    """写入路径不在任务声明的范围内。这不是「参数不对」，是「越界」，不可自动重试。"""
