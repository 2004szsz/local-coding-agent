# -*- coding: utf-8 -*-
"""源码与数据摸底：先定位，再读上下文，简单数值用计算器。"""
from .base import Skill

SKILL = Skill(
    name="data_analysis",
    title="数据分析",
    summary="检索并阅读工作空间中的代码与文本，归纳结构、调用关系和关键数据。",
    tools=("rag_search", "file_search", "list_dir", "read_file", "calculator"),
    guidance="""
理解既有实现时：
1. 不知道位置就用 rag_search；已经知道符号名或字符串就用 file_search。
2. 命中后用 read_file 读相关行，不要凭检索片段直接改代码。
3. 比例、差值、数量用 calculator。不要为一步算术去跑代码沙箱。
""",
)
