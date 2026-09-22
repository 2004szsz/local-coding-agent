# -*- coding: utf-8 -*-
"""
源码卫生检查。

存在的理由非常具体：本项目在一次工具写入后，出现过**整个文件被写成 NUL 字节**的情况。
而 Python 报的错指向「import 它的那一行」，完全看不出真正损坏的是哪个文件
（报错点是 `from . import runtime`，坏的是 `runtime.py`），排查成本很高。

这个测试把那类问题变成一条一眼可读、定位到文件的失败。
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: 参与检查的源码目录
FOLDERS = ("agents", "tools", "tests", "app", "skills", "memory")
#: 出现在 .py 里就一定异常、且会让解析失败的字符
FORBIDDEN = {"\x00": "NUL 空字节", "\u200b": "零宽空格", "\u200c": "零宽非连接符",
             "\u200d": "零宽连接符"}


def _sources():
    for folder in FOLDERS:
        base = ROOT / folder
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


class SourceHygieneTests(unittest.TestCase):
    def test_no_forbidden_characters(self):
        problems = []
        for path in _sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            for char, name in FORBIDDEN.items():
                if char in text:
                    problems.append(
                        f"{path.relative_to(ROOT)} 含 {name} × {text.count(char)}")
        self.assertEqual(problems, [],
                         "源码含控制字符（通常意味着写入时被损坏）: " + "；".join(problems))

    def test_every_source_file_parses(self):
        """文件非空且可解析。空文件同样是损坏的一种形态（整文件被清空）。"""
        problems = []
        for path in _sources():
            text = path.read_text(encoding="utf-8", errors="replace")
            relative = path.relative_to(ROOT)
            if not text.strip():
                problems.append(f"{relative} 是空文件")
                continue
            try:
                ast.parse(text, filename=str(path))
            except SyntaxError as e:
                problems.append(f"{relative} 语法错误: {e.msg}（行 {e.lineno}）")
        self.assertEqual(problems, [], "；".join(problems))

    def test_critical_modules_are_importable(self):
        """
        关键模块必须能导入。

        这比单测更早地拦住「文件损坏但测试恰好没覆盖到」的情况。
        """
        import importlib

        modules = [
            "agents.state_loop.machine", "agents.state_loop.state",
            "agents.state_loop.runtime", "agents.state_loop.agent",
            "agents.state_loop.planner", "agents.state_loop.permissions",
            "agents.state_loop.journal", "agents.state_loop.outcome",
            "agents.state_loop.decisions", "agents.state_loop.context",
            "agents.state_loop.verify", "agents.state_loop.repair",
            "agents.state_loop.scheduler", "agents.state_loop.delegate",
            "tools.base", "tools.shell", "tools.errors", "tools.workspace",
            "tools.mcp.config", "tools.mcp.client", "tools.mcp.bridge",
            "tools.mcp.manager", "agents.registry", "agents.agent",
            "agents.gate", "tools.fs_access", "tools.fs_tools", "tools.system_tools",
        ]
        failures = []
        for name in modules:
            try:
                importlib.import_module(name)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{name}: {type(e).__name__}: {e}")
        self.assertEqual(failures, [], "；".join(failures))


if __name__ == "__main__":
    unittest.main()
