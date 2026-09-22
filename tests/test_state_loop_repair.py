# -*- coding: utf-8 -*-
"""失败分类、签名归一化与修复策略的单测。

这一组测试守的是「迭代修复」能不能真的收敛：
分类错了下一步动作就错，签名归一化错了停滞检测就永远不触发。
"""
import unittest

from tools.errors import (
    CommandFailedError,
    CommandTimeoutError,
    PatchConflictError,
    ScopeViolationError,
)

from agents.state_loop import machine, repair
from agents.state_loop.machine import EV, DEFAULT_LIMITS
from agents.state_loop.state import (
    Acceptance,
    FailureKind,
    LoopState,
    Phase,
    TaskGraph,
    TaskNode,
    TaskStatus,
)


def make_state(task: TaskNode | None = None, phase: Phase = Phase.repair) -> LoopState:
    task = task or TaskNode(id="t1", title="任务", scope=("app",),
                            acceptance=Acceptance(commands=(("python", "-m", "pytest"),)),
                            status=TaskStatus.ready)
    return LoopState(phase=phase, graph=TaskGraph(tasks={task.id: task}, order=[task.id]),
                     current_task_id=task.id)


class ClassifyTests(unittest.TestCase):
    def test_known_exception_types(self):
        cases = {
            "FileNotFoundError": FailureKind.NotFound,
            "PathTraversalError": FailureKind.PathDenied,
            "PermissionError": FailureKind.PermissionDenied,
            "PatchConflictError": FailureKind.PatchConflict,
            "CommandFailedError": FailureKind.CommandFailed,
            "TimeoutExpired": FailureKind.Timeout,
            "CommandTimeoutError": FailureKind.Timeout,
            "SandboxViolationError": FailureKind.SandboxViolation,
            "McpToolError": FailureKind.McpError,
            "ScopeViolationError": FailureKind.ScopeViolation,
        }
        for error_type, expected in cases.items():
            with self.subTest(error_type=error_type):
                failure = repair.classify_error(error_type, "出错了", "tool")
                self.assertEqual(failure.kind, str(expected))

    def test_tool_reported_kind_is_used_directly(self):
        """工具可以自报 FailureKind 值（如 shell 的 exit_code 判定），不该被改判。"""
        failure = repair.classify_error("CommandFailed", "退出码 1", "run_command")
        self.assertEqual(failure.kind, str(FailureKind.CommandFailed))

    def test_keyword_fallback_for_plain_value_error(self):
        """旧工具只能抛 ValueError 时，靠具体关键词补漏。"""
        failure = repair.classify_error("ValueError", "未在文件中找到 old_string", "edit_file")
        self.assertEqual(failure.kind, str(FailureKind.PatchConflict))

    def test_unknown_type_falls_back_to_tool_error(self):
        """兜底保证「分类永不失败」：宁可标为待修，也不要漏判。"""
        failure = repair.classify_error("WeirdError", "说不清的错误", "x")
        self.assertEqual(failure.kind, str(FailureKind.ToolError))
        self.assertTrue(failure.retryable)

    def test_non_retryable_kinds(self):
        for error_type in ("PathTraversalError", "PermissionError", "ScopeViolationError"):
            with self.subTest(error_type=error_type):
                self.assertFalse(repair.classify_error(error_type, "x", "t").retryable)

    def test_exception_classes_map_by_name(self):
        """异常类名即失败类别，因此 tools/errors.py 里的类名不能改。"""
        for exc in (PatchConflictError, CommandFailedError, CommandTimeoutError,
                    ScopeViolationError):
            with self.subTest(exc=exc.__name__):
                self.assertIn(exc.__name__, repair._TYPE_TO_KIND)


class SignatureTests(unittest.TestCase):
    def test_line_numbers_are_normalized_away(self):
        """同一个错误换了个行号，必须算同一个签名——否则停滞检测永远不触发。"""
        left = repair.signature("CommandFailed", "pytest",
                                "FAILED tests/test_x.py line 42 assertion failed")
        right = repair.signature("CommandFailed", "pytest",
                                 "FAILED tests/test_x.py line 57 assertion failed")
        self.assertEqual(left, right)

    def test_colon_line_column_is_normalized(self):
        left = repair.signature("DiagnosticError", "ruff", "app/main.py:12:5 undefined name")
        right = repair.signature("DiagnosticError", "ruff", "app/main.py:88:9 undefined name")
        self.assertEqual(left, right)

    def test_durations_and_temp_paths_are_normalized(self):
        left = repair.signature("Timeout", "run_command", "耗时 1234ms，临时目录 /tmp/pytest-12/a")
        right = repair.signature("Timeout", "run_command", "耗时 9999ms，临时目录 /tmp/pytest-99/b")
        self.assertEqual(left, right)

    def test_ansi_colors_are_stripped(self):
        left = repair.signature("CommandFailed", "pytest", "\x1b[31mFAILED\x1b[0m a")
        right = repair.signature("CommandFailed", "pytest", "FAILED a")
        self.assertEqual(left, right)

    def test_different_errors_keep_different_signatures(self):
        left = repair.signature("CommandFailed", "pytest", "assert 1 == 2")
        right = repair.signature("CommandFailed", "pytest", "import error: no module named x")
        self.assertNotEqual(left, right)

    def test_kind_and_tool_participate(self):
        self.assertNotEqual(repair.signature("ArgError", "a", "x"),
                            repair.signature("ArgError", "b", "x"))


class RecordTests(unittest.TestCase):
    def test_record_counts_per_signature(self):
        state = make_state()
        failure = repair.classify_error("CommandFailed", "同样的错", "pytest")
        self.assertEqual(repair.record(state, failure), 1)
        self.assertEqual(repair.record(state, failure), 2)

    def test_record_fills_missing_signature(self):
        state = make_state()
        failure = repair.classify_error("ArgError", "缺参数", "edit_file")
        failure.signature = ""
        self.assertEqual(repair.record(state, failure), 1)
        self.assertTrue(failure.signature)


class ChooseTests(unittest.TestCase):
    def setUp(self):
        self.state = make_state()

    def choose(self, failure, count=1, limits=DEFAULT_LIMITS):
        return repair.choose(self.state, failure, limits, count)

    def test_stall_takes_priority(self):
        failure = repair.classify_error("CommandFailed", "错", "pytest")
        self.assertEqual(self.choose(failure, count=repair.STALL_THRESHOLD), EV.STALLED)

    def test_permission_denied_blocks_task(self):
        failure = repair.classify_error("PermissionError", "不在允许列表", "run_command")
        self.assertEqual(self.choose(failure), EV.REPAIR_BLOCKED)

    def test_must_read_not_found_replans(self):
        """必读文件不存在 → 文件名本身就错了，改参数没有意义。"""
        failure = repair.classify_error("FileNotFoundError", "文件不存在: app/x.py", "read_file")
        failure.artifacts["must_read"] = True
        self.assertEqual(self.choose(failure), EV.REPAIR_REPLAN)

    def test_plain_not_found_edits(self):
        failure = repair.classify_error("FileNotFoundError", "文件不存在: app/x.py", "read_file")
        self.assertEqual(self.choose(failure), EV.REPAIR_EDIT)

    def test_scope_violation_rolls_back_first_then_replans(self):
        failure = repair.classify_error("ScopeViolationError", "写到范围外", "edit_file")
        self.assertEqual(self.choose(failure), EV.REPAIR_ROLLBACK)

        task = self.state.graph.tasks["t1"]
        repair.bump_scope_violation(self.state, task)
        self.assertEqual(self.choose(failure), EV.REPAIR_REPLAN)

    def test_patch_conflict_rolls_back(self):
        failure = repair.classify_error("PatchConflictError", "old_string 出现 2 次", "edit_file")
        self.assertEqual(self.choose(failure), EV.REPAIR_ROLLBACK)

    def test_command_failure_edits_while_budget_remains(self):
        failure = repair.classify_error("CommandFailed", "退出码 1", "pytest")
        self.assertEqual(self.choose(failure), EV.REPAIR_EDIT)

    def test_command_failure_rolls_back_when_attempts_almost_exhausted(self):
        """盲改两次仍失败 → 回干净状态重读真实文件，比第三次盲改有效。"""
        self.state.graph.tasks["t1"].attempts = DEFAULT_LIMITS.max_attempts - 1
        failure = repair.classify_error("TestFailed", "断言失败", "verify")
        self.assertEqual(self.choose(failure), EV.REPAIR_ROLLBACK)

    def test_mcp_error_edits(self):
        failure = repair.classify_error("McpToolError", "传输断开", "mcp_docs_lookup")
        self.assertEqual(self.choose(failure), EV.REPAIR_EDIT)

    def test_every_branch_is_a_real_event(self):
        """选出来的事件名必须都在转移表里，否则运行时会被判非法转移。"""
        produced = {EV.STALLED, EV.REPAIR_EDIT, EV.REPAIR_ROLLBACK,
                    EV.REPAIR_REPLAN, EV.REPAIR_BLOCKED}
        for event in produced:
            with self.subTest(event=event):
                self.assertIn((Phase.repair, event), machine._TRANSITIONS)


if __name__ == "__main__":
    unittest.main()
