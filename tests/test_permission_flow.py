# -*- coding: utf-8 -*-
"""
主循环 × 审批通道的端到端（切片 5 验收清单）：

L4 外部根写入（fs_write）必须停在 authorize 等人：
    - confirm allow  → 工具才真正执行，文件落盘
    - confirm deny   → 工具不执行，任务 blocked，文件不存在
    - 无审批处理器   → 失败关闭（拒绝），文件不存在
    - 60s 超时       → 视为拒绝（用短超时 hub 模拟）
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from tools.base import ToolRegistry
from tools.fs_access import AccessBroker, AccessRoot
from tools.fs_tools import register_fs_read_tools, register_fs_write_tools
from tools.workspace import WorkspaceSecurity

from agents.state_loop.machine import EV, LoopLimits
from agents.state_loop.runtime import LoopDeps, build_initial_state, run_loop
from agents.state_loop.state import ExecMode, HaltReason, Phase, TaskStatus

from app.api.routes_permissions import PermissionHub, make_confirm_handler


def plan_payload() -> Dict[str, Any]:
    return {
        "summary": "在授权根内写一个外部文件",
        "tasks": [{
            "title": "向根 r 写入 external.txt",
            "detail": "用 fs_write 在根 r 创建 external.txt",
            "scope": ["r:external.txt"],  # 外部根写入必须声明 root:rel，authorize 按此校验
            "commands": [["python", "check_ok.py"]],
            "must_read": [],
        }],
    }


def fs_write_action(content: str = "approved") -> Dict[str, Any]:
    return {"kind": "tool_batch", "calls": [{"name": "fs_write", "arguments": {
        "root": "r", "path": "external.txt", "content": content}}]}


class ConfirmFlowHarness:
    def __init__(self, root: Path, external_root: Path,
                 confirm_handler=None):
        self.root = root
        self.external_root = external_root
        self.workspace = WorkspaceSecurity(root)
        (root / "check_ok.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")

        self.broker = AccessBroker(
            {"r": AccessRoot(name="r", path=external_root, read=True, write=True)})
        self.registry = ToolRegistry()
        register_fs_read_tools(self.registry, self.broker, {})
        register_fs_write_tools(self.registry, self.broker, {})

        self.events: List[tuple] = []
        self.decide_calls = 0
        self.deps = LoopDeps(
            registry=self.registry,
            workspace=self.workspace,
            config=LoopLimits(max_steps=80, max_model_calls=20, max_attempts=3,
                              max_replan=2),
            mode=ExecMode.auto_workspace,
            system_prompt="测试",
            allowed_tools=tuple(self.registry.names()),
            decision_hook=self._hook,
            broker=self.broker,
            confirm_handler=confirm_handler,
        )

    async def _hook(self, state, purpose: str):
        if purpose == "decompose":
            return plan_payload()
        self.decide_calls += 1
        return fs_write_action()

    def emit(self, kind: str, data: Dict[str, Any]) -> None:
        self.events.append((kind, data))

    async def run(self) -> Any:
        state = build_initial_state("写外部文件", self.deps.mode)
        return await run_loop(self.deps, state, self.emit)

    def kinds(self) -> List[str]:
        return [kind for kind, _ in self.events]


class BaseConfirmCase(unittest.IsolatedAsyncioTestCase):
    confirm_handler = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.workspace_root = base / "ws"
        self.external_root = base / "ext"
        self.workspace_root.mkdir()
        self.external_root.mkdir()
        self.harness = ConfirmFlowHarness(
            self.workspace_root, self.external_root,
            confirm_handler=self.confirm_handler)

    def tearDown(self):
        self._tmp.cleanup()

    def external_file_exists(self) -> bool:
        return (self.external_root / "external.txt").exists()


class AllowedFlowTests(BaseConfirmCase):
    async def test_allow_executes_tool_and_writes_file(self):
        hub = PermissionHub(timeout_seconds=5)

        async def _approve_after(pause=0.05):
            await asyncio.sleep(pause)
            for rid in list(hub._waiters.keys()):
                hub.decide(rid, True)

        self.harness.deps.confirm_handler = make_confirm_handler(hub)
        driver = asyncio.ensure_future(self.harness.run())
        approver = asyncio.ensure_future(_approve_after())
        final = await driver
        await approver

        self.assertIs(final.phase, Phase.halt)
        self.assertIn("permission_request", self.harness.kinds())
        self.assertTrue(self.external_file_exists(), "允许后文件必须落盘")
        self.assertIn("approved", (self.external_root / "external.txt")
                      .read_text(encoding="utf-8"))


class DeniedFlowTests(BaseConfirmCase):
    async def test_deny_blocks_task_without_writing(self):
        hub = PermissionHub(timeout_seconds=5)

        async def _deny_after(pause=0.05):
            await asyncio.sleep(pause)
            for rid in list(hub._waiters.keys()):
                hub.decide(rid, False)

        self.harness.deps.confirm_handler = make_confirm_handler(hub)
        driver = asyncio.ensure_future(self.harness.run())
        denier = asyncio.ensure_future(_deny_after())
        final = await driver
        await denier

        self.assertFalse(self.external_file_exists(), "拒绝后文件不得存在")
        task = final.graph.tasks["t1"] if final.graph else None
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TaskStatus.blocked)


class NoHandlerTests(BaseConfirmCase):
    async def test_missing_handler_fails_closed(self):
        self.harness.deps.confirm_handler = None
        final = await self.harness.run()
        self.assertFalse(self.external_file_exists(), "无审批处理器时不得执行")
        self.assertIn("permission_request", self.harness.kinds())


class TimeoutTests(BaseConfirmCase):
    async def test_timeout_treated_as_deny(self):
        hub = PermissionHub(timeout_seconds=0.1)
        self.harness.deps.confirm_handler = make_confirm_handler(hub)
        final = await self.harness.run()
        self.assertFalse(self.external_file_exists(), "超时必须按拒绝处理")


if __name__ == "__main__":
    unittest.main()