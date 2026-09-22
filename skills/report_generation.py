# -*- coding: utf-8 -*-
"""收尾报告：改动、验证、风险，短而具体。"""
from .base import Skill

SKILL = Skill(
    name="report_generation",
    title="报告生成",
    summary="把本轮修改和验证收成一段可复核的中文说明。",
    tools=("read_file",),
    guidance="""
结束前用中文给出报告，包含：
1. 做了什么（一句话）。
2. 碰到的文件路径，以及关键改动，不要贴无关全文。
3. 如何验证（读文件、搜索或沙箱结果）。没有验证就写明未验证。
4. 还没收口的风险。
""",
)
