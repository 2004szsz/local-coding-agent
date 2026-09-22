# -*- coding: utf-8 -*-
"""跨会话用户偏好：记住风格与项目约定，经 preference_* 工具维护。"""
from .base import Skill

SKILL = Skill(
    name="user_preferences",
    title="用户偏好记忆",
    summary="读写跨会话偏好（回复风格、技术栈约定等），与 RAG / 会话历史分离。",
    tools=(
        "preference_list",
        "preference_add",
        "preference_update",
        "preference_delete",
    ),
    guidance="""
仅在用户明确要求「记住 / 以后都… / 别再…」时写入偏好：
1. 先 preference_list，避免重复条目。
2. 新增用 preference_add；改文案或停用用 preference_update；确认删除用 preference_delete。
3. 不要把单次任务说明、临时路径或密码写进偏好。
4. 已启用的偏好会自动出现在系统提示词「用户偏好记忆」一节，无需再口头复述整表。
""",
)
