# -*- coding: utf-8 -*-
"""
四档执行模式：功能矩阵 + 边界（不越权、不假放开）。

验证：
1. MODE_POLICY 对 L0–L5 的确认/放行/拒绝与档位一致
2. full_access 不关掉 L5 / 路径门闩
3. fs_* 写入工具描述随 ExecMode 切换
4. 「本机已授权」与「完全访问」执行档语义分离
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.base import Tool, ToolRegistry
from tools.fs_access import AccessBroker, AccessRoot
from tools.fs_tools import apply_fs_write_descriptions, register_fs_write_tools
from tools.workspace import WorkspaceSecurity

from agents.gate import authorize_tool
from agents.state_loop.permissions import authorize
from agents.state_loop.state import Effect, ExecMode, ToolCall


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for name in (
        "read_file", "write_file", "edit_file", "run_python_code", "run_command",
        "fs_write", "mcp_docs_create",
    ):
        registry.register(Tool(
            name=name, description=name,
            parameters={"type": "object", "properties": {}},
            handler=lambda kwargs: "ok",
            read_only=False,
        ))
    return registry


class ExecModePolicyMatrixTests(unittest.TestCase):
    """四档 × 副作用等级：功能边界一张表证完。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = WorkspaceSecurity(self._tmp.name)
        self.registry = _registry()

    def tearDown(self):
        self._tmp.cleanup()

    def _auth(self, name, arguments, mode, scope=("app",)):
        return authorize(
            [ToolCall("c1", name, arguments)],
            mode, self.registry, self.workspace, scope=scope)

    def test_plan_confirm_auto_full_matrix(self):
        cases = [
            # (tool, args, mode, expect_granted, expect_confirm, expect_denied)
            ("read_file", {"path": "app/a.py"}, ExecMode.plan, True, False, False),
            ("read_file", {"path": "app/a.py"}, ExecMode.confirm_writes, True, False, False),
            ("read_file", {"path": "app/a.py"}, ExecMode.auto_workspace, True, False, False),
            ("read_file", {"path": "app/a.py"}, ExecMode.full_access, True, False, False),

            ("edit_file", {"path": "app/a.py"}, ExecMode.confirm_writes, False, True, False),
            ("edit_file", {"path": "app/a.py"}, ExecMode.auto_workspace, True, False, False),
            ("edit_file", {"path": "app/a.py"}, ExecMode.full_access, True, False, False),
            ("edit_file", {"path": "app/a.py"}, ExecMode.plan, True, False, False),

            ("run_command", {"argv": ["pytest"]}, ExecMode.confirm_writes, False, True, False),
            ("run_command", {"argv": ["pytest"]}, ExecMode.auto_workspace, True, False, False),
            ("run_command", {"argv": ["pytest"]}, ExecMode.full_access, True, False, False),

            ("mcp_docs_create", {}, ExecMode.auto_workspace, False, True, False),
            ("mcp_docs_create", {}, ExecMode.confirm_writes, False, True, False),
            ("mcp_docs_create", {}, ExecMode.plan, False, True, False),
            ("mcp_docs_create", {}, ExecMode.full_access, True, False, False),

            ("fs_write", {"root": "r", "path": "a.txt"}, ExecMode.auto_workspace, False, True, False),
            ("fs_write", {"root": "r", "path": "a.txt"}, ExecMode.full_access, True, False, False),
        ]
        for name, args, mode, granted, confirm, denied in cases:
            with self.subTest(tool=name, mode=mode):
                scope = ("app",) if name != "fs_write" else ("r:a.txt",)
                result = self._auth(name, args, mode, scope=scope)
                self.assertEqual(result.granted, granted)
                self.assertEqual(result.needs_confirm, confirm)
                self.assertEqual(result.denied, denied)

    def test_escape_denied_in_every_mode(self):
        for mode in ExecMode:
            with self.subTest(mode=mode):
                result = self._auth(
                    "write_file", {"path": "../escape.txt"}, mode)
                self.assertTrue(result.denied)
                self.assertFalse(result.granted)

    def test_full_access_still_respects_task_scope(self):
        result = self._auth(
            "edit_file", {"path": "other/x.py"},
            ExecMode.full_access, scope=("app",))
        self.assertTrue(result.denied)

    def test_full_access_still_respects_external_scope(self):
        result = self._auth(
            "fs_write", {"root": "r", "path": "a.txt"},
            ExecMode.full_access, scope=("desktop:.",))
        self.assertTrue(result.denied)


class FsWriteDescriptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "r"
        root.mkdir()
        self.broker = AccessBroker(
            {"r": AccessRoot(name="r", path=root, read=True, write=True)},
            trash_dir=Path(self._tmp.name) / "trash",
            audit_log=Path(self._tmp.name) / "audit.jsonl",
        )
        self.registry = ToolRegistry()
        register_fs_write_tools(self.registry, self.broker)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_mentions_confirm(self):
        text = self.registry.get("fs_write").description
        self.assertIn("需要用户确认", text)
        self.assertNotIn("完全访问", text)

    def test_full_access_rewrites_description(self):
        n = apply_fs_write_descriptions(self.registry, ExecMode.full_access)
        self.assertGreaterEqual(n, 6)
        text = self.registry.get("fs_write").description
        self.assertIn("完全访问", text)
        self.assertIn("可自动执行", text)
        self.assertNotIn("需要用户确认", text)
        # 切回 confirm 档应恢复
        apply_fs_write_descriptions(self.registry, ExecMode.confirm_writes)
        self.assertIn("需要用户确认", self.registry.get("fs_write").description)


class GateFullAccessBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = WorkspaceSecurity(self._tmp.name)
        self.registry = _registry()

    def tearDown(self):
        self._tmp.cleanup()

    async def test_full_access_allows_fs_write_without_handler(self):
        decision = await authorize_tool(
            "fs_write", {"root": "r", "path": "a.txt"},
            registry=self.registry, workspace=self.workspace,
            confirm_handler=None, mode=ExecMode.full_access)
        self.assertTrue(decision.allowed)

    async def test_auto_workspace_still_blocks_fs_write_without_handler(self):
        decision = await authorize_tool(
            "fs_write", {"root": "r", "path": "a.txt"},
            registry=self.registry, workspace=self.workspace,
            confirm_handler=None, mode=ExecMode.auto_workspace)
        self.assertFalse(decision.allowed)
        self.assertIn("未获确认", decision.output)


class AccessLabelSeparationTests(unittest.TestCase):
    def test_access_status_not_named_full_access(self):
        from app.modules.coordinator import ModuleCoordinator

        class Runtime:
            broker = object()
            tools = type("T", (), {"names": lambda self: ["fs_read"]})()

        class FakeState:
            cfg = {"local_access": {"enabled": False}}
            runtime = Runtime()
            broker = Runtime.broker

        status = ModuleCoordinator()._access_status(FakeState())
        self.assertEqual(status["value"], "本地电脑")
        self.assertNotEqual(status["value"], "完全访问")


if __name__ == "__main__":
    unittest.main()
