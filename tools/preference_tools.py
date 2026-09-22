# -*- coding: utf-8 -*-
"""
用户偏好工具：经 ToolRegistry + 权限闸门读写 PreferenceStore。

Agent 不得直接写 preferences.json；写入类工具登记为 L2。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

from .base import Tool, ToolRegistry

if TYPE_CHECKING:
    from memory.preferences import PreferenceStore

PromptRefresh = Optional[Callable[[], None]]


def _store_or_raise(store: Optional["PreferenceStore"]) -> "PreferenceStore":
    if store is None:
        raise RuntimeError("偏好记忆未初始化")
    return store


def build_preference_list_tool(store: Optional["PreferenceStore"]) -> Tool:
    def preference_list(kwargs: Dict[str, Any]) -> str:
        prefs = _store_or_raise(store)
        enabled_only = bool(kwargs.get("enabled_only", False))
        items = prefs.list_items(enabled_only=enabled_only)
        if not items:
            return "当前没有用户偏好条目。"
        lines = []
        for item in items:
            flag = "开" if item.get("enabled") else "关"
            lines.append(
                f"- [{flag}] id={item['id']} source={item['source']}: {item['content']}"
            )
        return f"共 {len(items)} 条偏好：\n" + "\n".join(lines)

    return Tool(
        name="preference_list",
        description="列出跨会话用户偏好（内容、来源、启用状态）。改偏好前先看现有条目，避免重复。",
        parameters={
            "type": "object",
            "properties": {
                "enabled_only": {
                    "type": "boolean",
                    "description": "仅返回已启用条目，默认 false",
                },
            },
        },
        handler=preference_list,
        read_only=True,
    )


def build_preference_add_tool(
    store: Optional["PreferenceStore"],
    on_change: PromptRefresh = None,
) -> Tool:
    def preference_add(kwargs: Dict[str, Any]) -> str:
        from memory.preferences import PreferenceError

        prefs = _store_or_raise(store)
        content = str(kwargs.get("content") or "").strip()
        source = str(kwargs.get("source") or "user").strip() or "user"
        try:
            item = prefs.add(content, source=source, enabled=True)
        except PreferenceError as e:
            return f"添加失败: {e}"
        if on_change:
            on_change()
        return f"已添加偏好 id={item['id']}: {item['content']}"

    return Tool(
        name="preference_add",
        description=(
            "新增一条跨会话用户偏好（如「回复时先给结论再给细节」「项目用 React」）。"
            "仅在用户明确要求记住偏好时调用；不要把临时任务说明写成偏好。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "偏好正文，简洁可执行"},
                "source": {
                    "type": "string",
                    "description": "来源：user（默认）或 conversation_summary",
                    "enum": ["user", "conversation_summary"],
                },
            },
            "required": ["content"],
        },
        handler=preference_add,
    )


def build_preference_update_tool(
    store: Optional["PreferenceStore"],
    on_change: PromptRefresh = None,
) -> Tool:
    def preference_update(kwargs: Dict[str, Any]) -> str:
        from memory.preferences import PreferenceError

        prefs = _store_or_raise(store)
        item_id = str(kwargs.get("id") or "").strip()
        if not item_id:
            return "缺少必填参数: id"
        content = kwargs.get("content")
        enabled = kwargs.get("enabled")
        if content is None and enabled is None:
            return "至少提供 content 或 enabled 之一"
        try:
            item = prefs.update(
                item_id,
                content=None if content is None else str(content),
                enabled=None if enabled is None else bool(enabled),
            )
        except KeyError:
            return f"未找到偏好 id={item_id}"
        except PreferenceError as e:
            return f"更新失败: {e}"
        if on_change:
            on_change()
        flag = "启用" if item.get("enabled") else "停用"
        return f"已更新偏好 id={item['id']}（{flag}）: {item['content']}"

    return Tool(
        name="preference_update",
        description="更新已有偏好的正文或启用状态（enabled=false 即停用，不再注入系统提示词）。",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "偏好 id"},
                "content": {"type": "string", "description": "新正文（可选）"},
                "enabled": {"type": "boolean", "description": "是否启用（可选）"},
            },
            "required": ["id"],
        },
        handler=preference_update,
    )


def build_preference_delete_tool(
    store: Optional["PreferenceStore"],
    on_change: PromptRefresh = None,
) -> Tool:
    def preference_delete(kwargs: Dict[str, Any]) -> str:
        prefs = _store_or_raise(store)
        item_id = str(kwargs.get("id") or "").strip()
        if not item_id:
            return "缺少必填参数: id"
        if not prefs.delete(item_id):
            return f"未找到偏好 id={item_id}"
        if on_change:
            on_change()
        return f"已删除偏好 id={item_id}"

    return Tool(
        name="preference_delete",
        description="永久删除一条用户偏好。不确定时先 preference_list，或用 preference_update 停用。",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "偏好 id"},
            },
            "required": ["id"],
        },
        handler=preference_delete,
    )


def register_preference_tools(
    registry: ToolRegistry,
    store: Optional["PreferenceStore"],
    on_change: PromptRefresh = None,
) -> None:
    registry.register(build_preference_list_tool(store))
    registry.register(build_preference_add_tool(store, on_change))
    registry.register(build_preference_update_tool(store, on_change))
    registry.register(build_preference_delete_tool(store, on_change))
