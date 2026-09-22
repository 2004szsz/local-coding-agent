# -*- coding: utf-8 -*-
"""本地访问只读技能：授权根文件读取 + 本机系统状态（全部无副作用）。"""
from .base import Skill

SKILL = Skill(
    name="local_system",
    title="本地文件与系统信息",
    summary="在用户显式授权的目录（fs_roots 列出的根）内读取文件，并查询本机系统状态。",
    tools=("fs_roots", "fs_list", "fs_read", "fs_search", "fs_stat",
           "sys_overview", "sys_processes", "sys_disks", "sys_network", "sys_env",
           "sys_battery", "sys_hardware", "sys_installed_apps"),
    guidance="""
访问工作区外的本地文件时：
1. 第一步必须调 fs_roots 确认有哪些已授权根及其读写权限。
2. 每次 fs_* 调用都要显式给出 root 参数——作用域不能靠路径猜。
3. 只读根上的任何写入都会失败；需要写入时确认根标记为可写，且写入操作会请求用户确认。
4. 查询系统状态（进程/磁盘/网络/环境）用 sys_* 工具，均为只读、无副作用。
5. 二进制文件只返回元信息；超大文件会被截断并明确标注，不要当作已读全貌。
""",
)