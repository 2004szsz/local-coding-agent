# -*- coding: utf-8 -*-
"""技能注册表。新增技能时在这里登记，并在 agents/agent.yaml 里启用。"""
from __future__ import annotations

from .base import Skill
from .code_interpreter import SKILL as code_interpreter
from .data_analysis import SKILL as data_analysis
from .report_generation import SKILL as report_generation
from .local_system import SKILL as local_system
from .local_system_write import SKILL as local_system_write
from .user_preferences import SKILL as user_preferences

SKILLS: dict[str, Skill] = {
    data_analysis.name: data_analysis,
    code_interpreter.name: code_interpreter,
    report_generation.name: report_generation,
    user_preferences.name: user_preferences,
    local_system.name: local_system,
    local_system_write.name: local_system_write,
}


def get_skill(name: str) -> Skill:
    try:
        return SKILLS[name]
    except KeyError as e:
        known = ", ".join(SKILLS)
        raise KeyError(f"未知技能: {name}（可选: {known}）") from e


def tools_for_skills(names: list[str]) -> list[str]:
    """启用技能的工具并集，按技能顺序去重。这是模型可见工具的唯一来源。"""
    ordered: list[str] = []
    for name in names:
        for tool_name in get_skill(name).tools:
            if tool_name not in ordered:
                ordered.append(tool_name)
    return ordered


def render_skill_prompt(names: list[str]) -> str:
    """把启用的技能说明拼成系统提示词的一节。"""
    if not names:
        return ""
    parts = ["## 已启用技能", "按任务选用下列技能，不要调用未列出的工具。", ""]
    for name in names:
        parts.append(get_skill(name).prompt_section())
    return "\n".join(parts).strip() + "\n"
