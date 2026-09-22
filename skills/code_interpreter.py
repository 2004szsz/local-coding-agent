# -*- coding: utf-8 -*-
"""隔离执行与落盘：改工作空间里的文件，并用沙箱核对纯计算。"""
from .base import Skill

SKILL = Skill(
    name="code_interpreter",
    title="代码解释执行",
    summary="修改工作空间中的文件，在受限沙箱里核对纯计算，并在工作区内跑编译与测试。",
    tools=("edit_file", "write_file", "run_python_code", "run_command"),
    guidance="""
改代码：
- 已有文件用 edit_file，新文件才用 write_file。调用前必须先 read_file。
验证：
- 纯计算用 run_python_code。沙箱禁止 import，**看不到工作空间文件**，
  不能用它读盘或访问网络；超时或报错要写进最终回复，不要假装已经通过。
- 需要跑测试、编译或静态检查时用 run_command：它只接受 argv 数组
  （如 ["python","-m","pytest","tests/test_x.py","-q"]），不支持管道与 cd，
  可执行文件限于 python / pytest / ruff / pyright / pip show|list，
  且 `python -c` 形态被禁止——要执行的逻辑请先写成工作区内的脚本文件。
""",
)
