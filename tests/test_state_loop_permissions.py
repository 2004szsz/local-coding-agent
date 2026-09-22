# -*- coding: utf-8 -*-
"""权限分级与批次授权的单测。**安全判定必须逐条可证，不能靠「看起来没问题」。**"""
import tempfile
import unittest

from tools.base import Tool, ToolRegistry
from tools.workspace import WorkspaceSecurity

from agents.state_loop import permissions
from agents.state_loop.permissions import (
    Policy,
    authorize,
    classify,
    path_in_scope,
    write_targets,
)
from agents.state_loop.state import Effect, ExecMode, FailureKind, ToolCall


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("read_file", "list_dir", "file_search", "rag_search", "calculator",
                 "write_file", "edit_file", "run_python_code", "run_command",
                 "mcp_docs_lookup", "mcp_docs_create",
                 "fs_roots", "fs_read", "fs_write", "fs_edit", "fs_delete"):
        registry.register(Tool(
            name=name, description=name, parameters={"type": "object", "properties": {}},
            handler=lambda kwargs: "ok",
            read_only=name in ("mcp_docs_lookup",),
        ))
    return registry


class ScopeTests(unittest.TestCase):
    def test_exact_and_prefix_match(self):
        self.assertTrue(path_in_scope("app/main.py", ("app/main.py",)))
        self.assertTrue(path_in_scope("app/main.py", ("app",)))
        self.assertTrue(path_in_scope("./app/main.py", ("app/",)))

    def test_prefix_does_not_match_sibling_names(self):
        """`app` 不该命中 `application.py`——这是目录前缀最容易写错的地方。"""
        self.assertFalse(path_in_scope("application.py", ("app",)))
        self.assertFalse(path_in_scope("app_old/main.py", ("app",)))

    def test_empty_scope_matches_nothing(self):
        self.assertFalse(path_in_scope("app/main.py", ()))

    def test_external_root_scope(self):
        self.assertTrue(path_in_scope("desktop:notes.md", ("desktop:notes.md",)))
        self.assertTrue(path_in_scope("desktop:a/b.txt", ("desktop:.",)))
        self.assertTrue(path_in_scope("desktop:a/b.txt", ("desktop:a",)))
        self.assertFalse(path_in_scope("desktop:a/b.txt", ("docs:a",)))
        self.assertFalse(path_in_scope("app/main.py", ("desktop:.",)))


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = WorkspaceSecurity(self._tmp.name)
        self.registry = make_registry()

    def tearDown(self):
        self._tmp.cleanup()

    def classify(self, name, arguments):
        return classify(name, arguments, self.registry, self.workspace)

    def test_read_tools_are_l0(self):
        for name in ("read_file", "list_dir", "file_search", "rag_search", "calculator"):
            with self.subTest(tool=name):
                self.assertIs(self.classify(name, {}).effect, Effect.L0_READ)

    def test_write_tools_are_l1(self):
        for name in ("write_file", "edit_file"):
            with self.subTest(tool=name):
                self.assertIs(self.classify(name, {"path": "app/main.py"}).effect,
                              Effect.L1_WRITE)

    def test_sandbox_is_l2(self):
        self.assertIs(self.classify("run_python_code", {"code": "1+1"}).effect,
                      Effect.L2_SANDBOX)

    def test_write_outside_workspace_is_denied(self):
        result = self.classify("write_file", {"path": "../escape.txt"})
        self.assertFalse(result.accepted)
        self.assertIs(result.effect, Effect.L5_ESCAPE)
        self.assertEqual(result.failure.kind, str(FailureKind.PathDenied))
        self.assertFalse(result.failure.retryable)

    def test_unknown_tool_is_correctable_not_fatal(self):
        """编造的工具名要变成「可纠正的失败」，而不是一路撞墙。"""
        result = self.classify("file_reader", {})
        self.assertFalse(result.accepted)
        self.assertEqual(result.failure.kind, str(FailureKind.NotFound))
        self.assertTrue(result.failure.retryable)

    def test_allowed_command_is_l3(self):
        result = self.classify("run_command", {"argv": ["pytest", "-q"]})
        self.assertTrue(result.accepted)
        self.assertIs(result.effect, Effect.L3_COMMAND)

    def test_python_m_pytest_is_allowed(self):
        """
        `python -m pytest` 必须放行。

        一刀切禁掉 `-m` 会顺手禁掉最常用的跑测试写法——安全规则切错方向
        比不够严格更糟：它会让人绕过去，或者干脆不用自测验证。
        """
        result = self.classify("run_command", {"argv": ["python", "-m", "pytest", "-q"]})
        self.assertTrue(result.accepted)
        self.assertIs(result.effect, Effect.L3_COMMAND)

    def test_python_c_is_denied(self):
        result = self.classify("run_command", {"argv": ["python", "-c", "import os"]})
        self.assertFalse(result.accepted)
        self.assertEqual(result.failure.kind, str(FailureKind.PermissionDenied))

    def test_python_m_unknown_module_is_denied(self):
        result = self.classify("run_command", {"argv": ["python", "-m", "http.server"]})
        self.assertFalse(result.accepted)
        self.assertEqual(result.failure.kind, str(FailureKind.PermissionDenied))

    def test_unknown_argv0_is_denied(self):
        for argv0 in ("rm", "bash", "curl", "git"):
            with self.subTest(argv0=argv0):
                result = self.classify("run_command", {"argv": [argv0, "-x"]})
                self.assertFalse(result.accepted)
                self.assertEqual(result.failure.kind, str(FailureKind.PermissionDenied))

    def test_pip_install_is_denied_but_show_is_allowed(self):
        self.assertFalse(self.classify("run_command", {"argv": ["pip", "install", "x"]}).accepted)
        self.assertTrue(self.classify("run_command", {"argv": ["pip", "show", "x"]}).accepted)

    def test_windows_exe_suffix_is_normalized(self):
        result = self.classify("run_command", {"argv": ["python.exe", "-m", "pytest"]})
        self.assertTrue(result.accepted)

    def test_cwd_outside_workspace_is_denied(self):
        result = self.classify("run_command", {"argv": ["pytest"], "cwd": "../.."})
        self.assertFalse(result.accepted)
        self.assertEqual(result.failure.kind, str(FailureKind.PathDenied))

    def test_malformed_argv_is_a_command_problem_not_a_permission_one(self):
        """
        参数形态错误交给工具处理器抛 ArgError，而不是在这里判权限。

        差别很实际：模型收到「参数要写成数组」才会改，收到「你没权限」只会放弃。
        """
        result = self.classify("run_command", {"argv": "pytest -q"})
        self.assertTrue(result.accepted)
        self.assertIs(result.effect, Effect.L3_COMMAND)

    def test_mcp_read_only_from_annotations(self):
        self.assertIs(self.classify("mcp_docs_lookup", {}).effect, Effect.L0_READ)

    def test_mcp_mutating_tool_is_l4(self):
        self.assertIs(self.classify("mcp_docs_create", {}).effect, Effect.L4_MCP_MUTATE)

    def test_write_targets_extraction(self):
        self.assertEqual(write_targets("edit_file", {"path": "a.py"}), ["a.py"])
        self.assertEqual(write_targets("read_file", {"path": "a.py"}), [])

    def test_fs_write_is_l4(self):
        self.assertIs(self.classify("fs_read", {"root": "d", "path": "a.txt"}).effect,
                      Effect.L0_READ)
        self.assertIs(self.classify("fs_write", {"root": "d", "path": "a.txt"}).effect,
                      Effect.L4_MCP_MUTATE)


class AuthorizeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = WorkspaceSecurity(self._tmp.name)
        self.registry = make_registry()

    def tearDown(self):
        self._tmp.cleanup()

    def authorize(self, calls, mode=ExecMode.auto_workspace, scope=("app",)):
        return authorize(calls, mode, self.registry, self.workspace, scope=scope)

    def test_write_in_scope_is_granted(self):
        result = self.authorize([ToolCall("c1", "edit_file", {"path": "app/main.py"})])
        self.assertTrue(result.granted)
        self.assertIs(result.calls[0].effect, Effect.L1_WRITE)

    def test_write_out_of_scope_is_denied_with_scope_violation(self):
        """scope 守卫在 authorize，不在提示词里——提示词里的叮嘱拦不住任何东西。"""
        result = self.authorize([ToolCall("c1", "edit_file", {"path": "other/x.py"})])
        self.assertTrue(result.denied)
        self.assertEqual(result.failure.kind, str(FailureKind.ScopeViolation))
        self.assertFalse(result.failure.retryable)

    def test_no_scope_means_no_writes(self):
        result = self.authorize([ToolCall("c1", "write_file", {"path": "app/new.py"})],
                                scope=())
        self.assertTrue(result.denied)

    def test_mixed_batch_takes_the_strictest_policy(self):
        """
        批次策略取**最严格**的一条，而不是取数值最大的副作用等级。

        反例：confirm_writes 档下 L2 自动、L1 需确认。若按等级取 max 会得到 L2（自动），
        于是一次 edit_file + 一次 run_python_code 就把写盘悄悄放过了。
        """
        calls = [
            ToolCall("c1", "edit_file", {"path": "app/main.py"}),
            ToolCall("c2", "run_python_code", {"code": "1+1"}),
        ]
        result = self.authorize(calls, mode=ExecMode.confirm_writes)
        self.assertTrue(result.needs_confirm)
        self.assertFalse(result.granted)

    def test_confirm_writes_pauses_on_l1_and_l3(self):
        self.assertTrue(self.authorize(
            [ToolCall("c1", "edit_file", {"path": "app/main.py"})],
            mode=ExecMode.confirm_writes).needs_confirm)
        self.assertTrue(self.authorize(
            [ToolCall("c1", "run_command", {"argv": ["pytest"]})],
            mode=ExecMode.confirm_writes).needs_confirm)

    def test_confirm_writes_allows_reads_and_sandbox(self):
        self.assertTrue(self.authorize(
            [ToolCall("c1", "read_file", {"path": "app/main.py"})],
            mode=ExecMode.confirm_writes).granted)
        self.assertTrue(self.authorize(
            [ToolCall("c1", "run_python_code", {"code": "1+1"})],
            mode=ExecMode.confirm_writes).granted)

    def test_auto_workspace_allows_commands_but_pauses_on_mcp_mutation(self):
        self.assertTrue(self.authorize(
            [ToolCall("c1", "run_command", {"argv": ["pytest"]})]).granted)
        self.assertTrue(self.authorize(
            [ToolCall("c1", "mcp_docs_create", {})]).needs_confirm)

    def test_escape_is_denied_in_every_mode(self):
        for mode in ExecMode:
            with self.subTest(mode=mode):
                result = self.authorize(
                    [ToolCall("c1", "write_file", {"path": "../x.txt"})], mode=mode)
                self.assertTrue(result.denied)

    def test_model_reported_effect_is_overwritten(self):
        """模型自报的 effect 一律丢弃——否则它可以给自己提权。"""
        forged = ToolCall("c1", "write_file", {"path": "app/x.py"}, effect=Effect.L0_READ)
        result = self.authorize([forged])
        self.assertIs(result.calls[0].effect, Effect.L1_WRITE)

    def test_policy_summary_is_readable(self):
        text = permissions.describe_policy(ExecMode.auto_workspace)
        self.assertIn("自动", text)
        self.assertIn("拒绝", text)

    def test_external_write_out_of_scope_is_denied(self):
        result = self.authorize(
            [ToolCall("c1", "fs_write", {"root": "r", "path": "a.txt"})],
            scope=("desktop:.",))
        self.assertTrue(result.denied)
        self.assertEqual(result.failure.kind, str(FailureKind.ScopeViolation))

    def test_external_write_in_scope_needs_confirm(self):
        result = self.authorize(
            [ToolCall("c1", "fs_write", {"root": "r", "path": "a.txt"})],
            scope=("r:a.txt",))
        self.assertTrue(result.needs_confirm)


if __name__ == "__main__":
    unittest.main()
