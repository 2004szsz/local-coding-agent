示例 1：局部修改

用户：把 ReAct 最大轮数从 12 改成 8。
动作：file_search pattern=max_iterations → read_file path=agents/agent.yaml → edit_file 只改这一处数字。
回复：已将 agents/agent.yaml 的 max_iterations 改为 8。搜索确认没有第二处仍写着 12。

示例 2：先定位再决定不改

用户：路径越界是在哪里拦住的？
动作：file_search pattern=PathTraversalError → read_file path=tools/workspace.py。
回复：拦截在 tools/workspace.py 的 WorkspaceSecurity.resolve，越界会抛出 PathTraversalError。这次没有改文件。

示例 3：算术不要进沙箱

用户：120、80、40 的平均数是多少？
动作：calculator expression=(120+80+40)/3。
回复：平均 80。没有调用 run_python_code。

示例 4：读本机已授权目录

用户：桌面上有哪些 markdown？
动作：fs_roots → fs_search root=desktop pattern=.md filename=*.md。
回复：列出命中文件。没有用 list_dir 去猜桌面绝对路径。

示例 5：查本机状态

用户：现在内存和磁盘还剩多少？
动作：sys_overview、sys_disks。
回复：给出占用与剩余。没有调用 run_command。
