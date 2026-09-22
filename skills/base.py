# -*- coding: utf-8 -*-
"""
技能：一组工具加上一段任务说明。

工具是原子动作（读文件、搜索、计算）。技能不再包一层执行器，
只声明“做这类任务时该用哪些工具、按什么顺序”，由 Agent 在系统提示词里读到。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    summary: str
    tools: tuple[str, ...]
    guidance: str

    def prompt_section(self) -> str:
        tools = ", ".join(self.tools)
        return f"### {self.title}（{self.name}）\n可用工具: {tools}\n{self.guidance.strip()}\n"
