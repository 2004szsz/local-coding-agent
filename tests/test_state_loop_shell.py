# -*- coding: utf-8 -*-
"""沙箱终端（L3）的单测。**真实起子进程**——命令执行的正确性没法靠 mock 证明。"""
import sys
import tempfile
import unittest
from pathlib import Path

from tools.shell import WorkspaceCommandRunner, build_shell_tool, normalize_argv0
from tools.workspace import WorkspaceSecurity


class ArgvNormalizationTests(unittest.TestCase):
    def test_windows_exe_suffix_stripped(self):
        self.assertEqual(normalize_argv0("python.exe"), "python")
        self.assertEqual(normalize_argv0(r"C:\Python313\python.EXE"), "python")

    def test_bare_names_unchanged(self):
        self.assertEqual(normalize_argv0("pytest"), "pytest")


class ShellRunnerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = WorkspaceSecurity(self.root)
        self.runner = WorkspaceCommandRunner(self.workspace, default_seconds=20,
                                            ceiling_seconds=30, tail_lines=80)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    # ---------------- 参数与权限 ----------------
    def test_rejects_missing_argv(self):
        result = self.runner.run(None)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ArgError")

    def test_rejects_shell_string(self):
        """只接受 argv 数组。字符串形式意味着 shell 语义，那是注入的入口。"""
        result = self.runner.run("python -m pytest")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ArgError")
        self.assertIn("argv", result.text)

    def test_rejects_path_as_argv0(self):
        """
        argv[0] 必须是命令名而不是路径。

        若允许路径，白名单约束的就不是「运行哪个程序」，而只是「文件名长什么样」——
        在工作区里放个同名可执行文件即可绕过。
        """
        result = self.runner.run([str(self.root / "python.exe"), "-V"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PermissionDenied")
        self.assertIn("命令名而不是路径", result.text)

    def test_rejects_unknown_command(self):
        result = self.runner.run(["curl", "http://x"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PermissionDenied")

    def test_rejects_python_c(self):
        result = self.runner.run(["python", "-c", "print(1)"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PermissionDenied")

    def test_rejects_python_m_unknown_module(self):
        result = self.runner.run(["python", "-m", "http.server"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PermissionDenied")

    def test_rejects_pip_install(self):
        result = self.runner.run(["pip", "install", "requests"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PermissionDenied")

    def test_cwd_outside_workspace_denied(self):
        result = self.runner.run(["pytest"], cwd="../..")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PathDenied")

    def test_cwd_must_be_directory(self):
        self.write("app/main.py", "x = 1\n")
        result = self.runner.run(["pytest"], cwd="app/main.py")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "ArgError")

    # ---------------- 真实执行 ----------------
    def test_successful_command(self):
        self.write("app/ok.py", "print('hello')\n")
        result = self.runner.run(["python", "app/ok.py"])
        self.assertTrue(result.ok, msg=result.text)
        self.assertEqual(result.artifacts["exit_code"], 0)
        self.assertIn("hello", result.text)
        self.assertIn("退出码: 0", result.text)

    def test_python_m_py_compile_is_allowed_and_runs(self):
        """`python -m <白名单模块>` 必须能真的跑起来，而不是只在判定层放行。"""
        self.write("app/ok.py", "x = 1\n")
        result = self.runner.run(["python", "-m", "py_compile", "app/ok.py"])
        self.assertTrue(result.ok, msg=result.text)
        self.assertEqual(result.artifacts["exit_code"], 0)

    def test_failure_reports_exit_code_and_tail(self):
        self.write("app/bad.py", "import sys\nsys.stderr.write('炸了')\nsys.exit(2)\n")
        result = self.runner.run(["python", "app/bad.py"])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "CommandFailed")
        self.assertEqual(result.artifacts["exit_code"], 2)
        self.assertIn("炸了", result.artifacts["tail"])

    def test_utf8_output_is_readable(self):
        """中文输出必须可读：乱码会让修复阶段读不懂错误栈。"""
        self.write("app/cn.py", "print('中文输出正常')\n")
        result = self.runner.run(["python", "app/cn.py"])
        self.assertTrue(result.ok, msg=result.text)
        self.assertIn("中文输出正常", result.text)

    def test_timeout_kills_process(self):
        self.write("app/sleepy.py", "import time\ntime.sleep(30)\n")
        result = self.runner.run(["python", "app/sleepy.py"], timeout_seconds=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "Timeout")
        self.assertTrue(result.artifacts["timed_out"])
        self.assertEqual(result.artifacts["exit_code"], -1)

    # ---------------- 输出窗口 ----------------
    def test_window_keeps_head_and_tail(self):
        """
        长输出取「前 20 行 + 后 60 行」。

        对半截断是错的：pytest 的失败列表在末尾，但开头的汇总行也有价值。
        """
        lines = "\n".join(f"line{i}" for i in range(1, 201))
        self.write("app/loud.py", f"print('''{lines}''')\n")
        result = self.runner.run(["python", "app/loud.py"])
        self.assertIn("省略中间", result.text)
        self.assertIn("line1", result.text)          # 头部保留
        self.assertIn("line200", result.text)        # 尾部保留
        self.assertNotIn("line100", result.text)     # 中段被省略

    def test_tail_artifact_holds_last_lines(self):
        lines = "\n".join(f"line{i}" for i in range(1, 201))
        self.write("app/loud.py", f"print('''{lines}''')\n")
        result = self.runner.run(["python", "app/loud.py"])
        self.assertIn("line200", result.artifacts["tail"])
        self.assertNotIn("line1\n", result.artifacts["tail"].replace("line100", ""))

    def test_timeout_is_clamped_to_ceiling(self):
        self.write("app/quick.py", "print('ok')\n")
        result = self.runner.run(["python", "app/quick.py"], timeout_seconds=99999)
        self.assertTrue(result.ok)   # 被夹紧到 30s，实际 0.1s 就返回

    def test_no_output_is_reported_explicitly(self):
        self.write("app/silent.py", "pass\n")
        result = self.runner.run(["python", "app/silent.py"])
        self.assertTrue(result.ok)
        self.assertIn("命令无输出", result.text)


class ShellToolTests(unittest.TestCase):
    def test_tool_schema_and_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = WorkspaceSecurity(tmp)
            Path(tmp, "ok.py").write_text("print('hi')\n", encoding="utf-8")
            runner = WorkspaceCommandRunner(workspace)
            tool = build_shell_tool(runner)
            self.assertEqual(tool.name, "run_command")
            self.assertEqual(tool.parameters["required"], ["argv"])
            # handler 直接调用也应返回结构化结果
            result = tool.handler({"argv": ["python", "ok.py"]})
            self.assertTrue(getattr(result, "ok", False))
            self.assertEqual(result.artifacts["exit_code"], 0)

    def test_tool_is_registered_as_l3(self):
        from agents.state_loop.permissions import TOOL_EFFECTS
        from agents.state_loop.state import Effect

        self.assertIs(TOOL_EFFECTS["run_command"], Effect.L3_COMMAND)


if __name__ == "__main__":
    unittest.main()
