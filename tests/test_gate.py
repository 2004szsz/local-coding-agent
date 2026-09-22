# -*- coding: utf-8 -*-
"""
框架共用审批闸：native_react 没有 authorize 状态，L4 必须在执行前被拦住。

- L0 自动放行并真正执行
- L4 无处理器 → 失败关闭，工具不执行
- L4 用户允许 → 执行；用户拒绝 → 不执行
"""
from __future__ import annotations

import tempfile
import unittest

from tools.base import Tool, ToolRegistry
from tools.workspace import WorkspaceSecurity

from agents.gate import authorize_tool, execute_gated_sync
from agents.state_loop.state import ExecMode, PermissionKind
from agents.base import AgentDependencies
from agents.state_loop.runtime import LoopDeps


def _registry_with(name: str, *, read_only: bool = False, sink=None):
    executed = sink if sink is not None else []
    registry = ToolRegistry()
    registry.register(Tool(
        name=name, description=name,
        parameters={"type": "object", "properties": {}},
        handler=lambda kwargs: executed.append(dict(kwargs)) or "ran",
        read_only=read_only,
    ))
    return registry, executed


class AuthorizeToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = WorkspaceSecurity(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_l0_runs_without_confirm(self):
        registry, executed = _registry_with("fs_read")
        decision = await authorize_tool(
            "fs_read", {"root": "d", "path": "a.txt"},
            registry=registry, workspace=self.workspace)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.events, ())
        # 闸门只判定，真正执行由调用方完成
        self.assertEqual(executed, [])

    async def test_l4_without_handler_fails_closed(self):
        registry, executed = _registry_with("fs_write")
        decision = await authorize_tool(
            "fs_write", {"root": "d", "path": "a.txt", "content": "x"},
            registry=registry, workspace=self.workspace, confirm_handler=None)
        self.assertFalse(decision.allowed)
        self.assertIn("未获确认", decision.output)
        kinds = [e["type"] for e in decision.events]
        self.assertIn("permission_request", kinds)
        self.assertEqual(executed, [])

    async def test_l4_allow_then_caller_may_execute(self):
        registry, executed = _registry_with("fs_write")

        async def allow(kind, calls, request_id):
            self.assertIs(kind, PermissionKind.tools)
            return True

        decision = await authorize_tool(
            "fs_write", {"root": "d", "path": "a.txt"},
            registry=registry, workspace=self.workspace, confirm_handler=allow)
        self.assertTrue(decision.allowed)
        output = registry.execute("fs_write", {"root": "d", "path": "a.txt"})
        self.assertEqual(output, "ran")
        self.assertEqual(len(executed), 1)

    async def test_l4_deny_does_not_execute(self):
        registry, executed = _registry_with("fs_write")

        async def deny(kind, calls, request_id):
            return False

        decision = await authorize_tool(
            "fs_write", {"root": "d", "path": "a.txt"},
            registry=registry, workspace=self.workspace, confirm_handler=deny)
        self.assertFalse(decision.allowed)
        self.assertEqual(executed, [])


class SyncGateTests(unittest.TestCase):
    def test_sync_gate_blocks_l4(self):
        registry, executed = _registry_with("fs_write")
        deps = AgentDependencies(
            llm=None, tools=registry, system_prompt="",
            loop_deps=LoopDeps(registry=registry, mode=ExecMode.auto_workspace))
        text = execute_gated_sync(deps, "fs_write", {"root": "d", "path": "a.txt"})
        self.assertIn("工具错误", text)
        self.assertIn("人工确认", text)
        self.assertEqual(executed, [])

    def test_sync_gate_allows_l0(self):
        registry, executed = _registry_with("fs_read")
        deps = AgentDependencies(
            llm=None, tools=registry, system_prompt="",
            loop_deps=LoopDeps(registry=registry, mode=ExecMode.auto_workspace))
        text = execute_gated_sync(deps, "fs_read", {"root": "d", "path": "a.txt"})
        self.assertEqual(text, "ran")
        self.assertEqual(len(executed), 1)


if __name__ == "__main__":
    unittest.main()
