# -*- coding: utf-8 -*-
"""本地访问写入技能：授权可写根上的写/删/移动，与受控系统动作（全部需用户确认）。"""
from .base import Skill

SKILL = Skill(
    name="local_system_write",
    title="本地文件写入与系统动作",
    summary="在用户显式授权的可写根内修改、移动、删除文件（删除走隔离目录可还原），并执行受控系统动作。",
    tools=("fs_write", "fs_edit", "fs_copy", "fs_move", "fs_delete", "fs_restore",
           "sys_open_path", "sys_notify", "sys_launch", "sys_screenshot"),
    guidance="""
对工作区外文件的写入与系统动作：
1. 这些工具全部需要用户逐次确认后才能执行，请把意图说清楚。
2. 修改已有文件优先用 fs_edit（精确局部替换），不要整体重写。
3. fs_delete 永不真删——内容进隔离目录，误删可用 fs_restore(ts) 还原。
4. 启动应用只用 sys_launch 且仅限白名单；不接受任意命令行。
""",
)