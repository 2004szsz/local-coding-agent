# -*- coding: utf-8 -*-
"""
决策协议：模型与状态机之间的**唯一**接口。

模型不产出「自由文本动作」。它只能产出一个名为 `submit_decision` 的函数调用，
参数形状由本模块定义。这不是风格选择，而是正确性要求：

- 若允许自由文本，协议被违反时上层只有两条路——**正则抠 JSON**（把「模型没按协议输出」
  这个重要信号静默成「解析成功」），或**当作最终答复**（循环从此失去目标）。
- 固定为函数调用后，协议违反是一个**一等事件**：记 `ModelProtocolError` → 重试一次 →
  仍失败则 `halt(blocked)`，全程可见、可统计。

本模块只负责**协议形状**（字段类型、枚举、必填项）。业务规则（任务图能不能执行）
在 `planner.py`，权限在 `permissions.py`——三者互不越界。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .state import Decision, DecisionKind, Failure, FailureKind, ToolCall

__all__ = [
    "SUBMIT_DECISION", "force_choice", "plan_spec", "act_spec",
    "parse_plan", "parse_decision", "extract_tool_arguments",
]

#: 决策工具的函数名。模型必须调用它，没有第二个入口。
SUBMIT_DECISION = "submit_decision"

_KINDS = tuple(str(k) for k in DecisionKind)


def force_choice(name: str = SUBMIT_DECISION) -> Dict[str, Any]:
    """
    构造强制调用某个函数的 `tool_choice`。

    注意这是**强制**而非 `auto`：`auto` 意味着模型可以选择「不用工具，直接说点什么」，
    那正是我们要杜绝的自由文本出口。
    """
    return {"type": "function", "function": {"name": name}}


def _protocol_error(message: str, raw: Any = None) -> Failure:
    failure = Failure(
        kind=str(FailureKind.ModelProtocolError),
        message=message,
        tool=SUBMIT_DECISION,
        retryable=True,
        artifacts={"raw": _clip(raw)},
    )
    failure.signature = ""
    return failure


def _clip(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


# ======================================================================
# 协议 schema
# ======================================================================
def plan_spec() -> Dict[str, Any]:
    """`decompose` 状态的 submit_decision schema：提交一张任务图。"""
    return {
        "type": "function",
        "function": {
            "name": SUBMIT_DECISION,
            "description": "提交需求拆解结果（任务图）。必须调用本函数，不要输出散文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "一句话目标"},
                    "tasks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "任务标题，一句话"},
                                "detail": {"type": "string", "description": "具体要做什么"},
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": ["string", "integer"]},
                                    "description": "前置任务的序号（从 1 开始）或标题原文",
                                },
                                "scope": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "允许写入的范围，必须非空。工作区用相对路径（app/main.py）；已授权本机根用 root:rel（desktop:notes.md）",
                                },
                                "commands": {
                                    "type": "array",
                                    "items": {"type": "array", "items": {"type": "string"}},
                                    "description": "验收命令的 argv 数组，如 [[\"python\",\"-m\",\"pytest\",\"-q\"]]",
                                },
                                "must_read": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "动手前必须先读的文件。工作区相对路径或授权根 root:rel",
                                },
                            },
                            "required": ["title", "scope"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    }


def act_spec(allowed_tools: Sequence[str]) -> Dict[str, Any]:
    """
    `decide` 状态的 submit_decision schema：提交下一步动作。

    `calls[].name` 用 `enum` 锁死在当前可见工具内——**模型编造工具名在协议层就被拦下**，
    而不是等到执行时才发现工具不存在。
    """
    name_schema: Dict[str, Any] = {"type": "string"}
    tools = [t for t in allowed_tools if t]
    if tools:
        name_schema["enum"] = tools

    return {
        "type": "function",
        "function": {
            "name": SUBMIT_DECISION,
            "description": "提交下一步动作。必须调用本函数，不要输出散文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(_KINDS),
                        "description": ("tool_batch=执行一批工具；mark_done=本任务已完成；"
                                        "replan=任务划分需要调整；ask_user=需要用户澄清"),
                    },
                    "calls": {
                        "type": "array",
                        "description": "kind=tool_batch 时必填",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": name_schema,
                                "arguments": {
                                    "type": "object",
                                    "description": "工具入参；各工具的参数定义见上文的工具目录",
                                },
                            },
                            "required": ["name", "arguments"],
                        },
                    },
                    "note": {"type": "string", "description": "给用户看的一句话说明"},
                    "replan_reason": {"type": "string", "description": "kind=replan 时必填"},
                    "question": {"type": "string", "description": "kind=ask_user 时必填"},
                },
                "required": ["kind"],
            },
        },
    }


# ======================================================================
# 解析
# ======================================================================
def _as_mapping(raw: Any, what: str) -> Tuple[Optional[Dict[str, Any]], Optional[Failure]]:
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, _protocol_error(f"{what} 为空")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return None, _protocol_error(f"{what} 不是合法 JSON: {e}", raw)
        if not isinstance(parsed, dict):
            return None, _protocol_error(f"{what} 必须是对象，收到 {type(parsed).__name__}", raw)
        return parsed, None
    return None, _protocol_error(f"{what} 类型不支持: {type(raw).__name__}", raw)


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None and str(item).strip()]
    return []


def parse_plan(raw: Any) -> Tuple[Optional[Dict[str, Any]], Optional[Failure]]:
    """
    校验计划提交的**结构**（不含业务规则，业务规则在 planner.accept）。

    返回归一化后的 dict：`{"summary": str, "tasks": [ {title, detail, depends_on, scope, commands, must_read} ]}`
    """
    data, failure = _as_mapping(raw, "计划")
    if failure is not None:
        return None, failure

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, (list, tuple)) or not raw_tasks:
        return None, _protocol_error("计划缺少非空的 tasks 数组", data)

    tasks: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            return None, _protocol_error(f"tasks[{index}] 必须是对象", item)

        title = str(item.get("title") or "").strip()
        if not title:
            return None, _protocol_error(f"tasks[{index}] 缺少 title", item)

        commands: List[List[str]] = []
        raw_commands = item.get("commands") or []
        if isinstance(raw_commands, (list, tuple)):
            for cmd in raw_commands:
                if isinstance(cmd, str):
                    return None, _protocol_error(
                        f"tasks[{index}] 的 commands 必须是 argv 数组（收到字符串），"
                        f"例如 [[\"python\",\"-m\",\"pytest\",\"-q\"]]", cmd)
                if not isinstance(cmd, (list, tuple)) or not cmd:
                    return None, _protocol_error(f"tasks[{index}] 的 commands 含空项", cmd)
                if not all(isinstance(part, str) and part for part in cmd):
                    return None, _protocol_error(f"tasks[{index}] 的命令元素必须是非空字符串", cmd)
                commands.append([str(part) for part in cmd])
        elif raw_commands:
            return None, _protocol_error(f"tasks[{index}] 的 commands 必须是数组", raw_commands)

        tasks.append({
            "title": title,
            "detail": str(item.get("detail") or "").strip(),
            "depends_on": _as_str_list(item.get("depends_on")),
            "scope": _as_str_list(item.get("scope")),
            "commands": commands,
            "must_read": _as_str_list(item.get("must_read")),
        })

    return {"summary": str(data.get("summary") or "").strip(), "tasks": tasks}, None


def parse_decision(raw: Any, allowed_tools: Iterable[str]) -> Tuple[Optional[Decision], Optional[Failure]]:
    """校验 `decide` 提交的结构，返回 `Decision` 或协议失败。"""
    data, failure = _as_mapping(raw, "决策")
    if failure is not None:
        return None, failure

    kind_text = str(data.get("kind") or "").strip()
    if kind_text not in _KINDS:
        return None, _protocol_error(
            f"kind 非法: {kind_text or '(空)'}，可选 {', '.join(_KINDS)}", data)

    kind = DecisionKind(kind_text)
    note = str(data.get("note") or "").strip()
    allowed = {str(name) for name in allowed_tools}

    if kind is DecisionKind.tool_batch:
        raw_calls = data.get("calls")
        if not isinstance(raw_calls, (list, tuple)) or not raw_calls:
            return None, _protocol_error("kind=tool_batch 时必须提供非空的 calls 数组", data)

        calls: List[ToolCall] = []
        for index, item in enumerate(raw_calls):
            if not isinstance(item, dict):
                return None, _protocol_error(f"calls[{index}] 必须是对象", item)
            name = str(item.get("name") or "").strip()
            if not name:
                return None, _protocol_error(f"calls[{index}] 缺少 name", item)
            if allowed and name not in allowed:
                return None, _protocol_error(
                    f"calls[{index}] 使用了不可用工具 {name}；"
                    f"当前可用: {', '.join(sorted(allowed))}", item)
            arguments = item.get("arguments")
            if arguments is None:
                arguments = {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except json.JSONDecodeError:
                    return None, _protocol_error(
                        f"calls[{index}]（{name}）的 arguments 不是合法 JSON", arguments)
            if not isinstance(arguments, dict):
                return None, _protocol_error(
                    f"calls[{index}]（{name}）的 arguments 必须是对象", arguments)
            calls.append(ToolCall(id=_call_id(index), name=name, arguments=arguments))

        return Decision(kind=kind, calls=tuple(calls), note=note), None

    if kind is DecisionKind.replan:
        reason = str(data.get("replan_reason") or "").strip()
        if not reason:
            return None, _protocol_error("kind=replan 时必须说明 replan_reason", data)
        return Decision(kind=kind, note=note, replan_reason=reason), None

    if kind is DecisionKind.ask_user:
        question = str(data.get("question") or "").strip()
        if not question:
            return None, _protocol_error("kind=ask_user 时必须给出 question", data)
        return Decision(kind=kind, note=note, question=question), None

    # mark_done
    return Decision(kind=kind, note=note), None


def extract_tool_arguments(message: Dict[str, Any], tool_name: str = SUBMIT_DECISION
                           ) -> Tuple[Any, Optional[Failure]]:
    """
    从 LLM 返回的 message 里取出指定函数的参数。

    这一步是「协议违反」的判定点：模型只回了散文、或调了别的函数，
    都在这里变成明确的 `ModelProtocolError`，而不是被静默忽略。
    """
    calls = message.get("tool_calls") or []
    if not calls:
        content = str(message.get("content") or "").strip()
        hint = f"模型返回了自然语言而不是函数调用（前 200 字：{_clip(content, 200)}）" if content \
            else "模型既没有返回内容也没有调用任何函数"
        return None, _protocol_error(f"没有收到 {tool_name} 调用：{hint}")

    for call in calls:
        function = call.get("function") or {}
        if str(function.get("name") or "") == tool_name:
            return function.get("arguments", "{}"), None

    names = ", ".join(str((c.get("function") or {}).get("name")) for c in calls)
    return None, _protocol_error(f"模型调用了 {names}，但没有调用 {tool_name}")


def _call_id(index: int) -> str:
    return f"c{index + 1}"
