# -*- coding: utf-8 -*-
"""
切片 4 验收：外部根写入的快照记账与回滚。

- fs_*（L4）写前记账：写入发生前，Journal 记下外部根文件的原始字节
- 回滚还原：repair 回滚时对外部根同样生效，且重新经 broker 解析
- 新建文件：before=None → 回滚时删除
- 根消失/不可写：对应条目跳过，不阻断其余还原
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from tools.base import ToolRegistry
from tools.fs_access import AccessBroker, AccessRoot
from tools.fs_tools import register_fs_read_tools, register_fs_write_tools
from tools.workspace import WorkspaceSecurity

from agents.state_loop.journal import FileJournal
from agents.state_loop.outcome import run_tool
from agents.state_loop.state import Effect, TaskNode, ToolCall


def make_broker(root, writable=True):
    return AccessBroker({"r": AccessRoot(name="r", path=root, read=True, write=writable)})


class JournalExternalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "r"
        self.root.mkdir()
        (self.root / "f.txt").write_text("original", encoding="utf-8")
        self.broker = make_broker(self.root, writable=True)
        self.workspace = WorkspaceSecurity(self.base / "ws")

    def tearDown(self):
        self._tmp.cleanup()

    def test_external_snapshot_restores_existing_file(self):
        journal = FileJournal()
        journal.checkpoint("t1", [], self.workspace)
        journal.remember_write_external("t1", "r", "f.txt", b"original",
                                        str(self.root / "f.txt"))
        (self.root / "f.txt").write_text("CHANGED", encoding="utf-8")
        restored = journal.restore("t1", self.workspace, broker=self.broker)
        self.assertIn("r:f.txt", restored)
        self.assertEqual((self.root / "f.txt").read_text(encoding="utf-8"), "original")

    def test_external_new_file_deleted_on_restore(self):
        journal = FileJournal()
        journal.checkpoint("t1", [], self.workspace)
        journal.remember_write_external("t1", "r", "new.txt", None,
                                        str(self.root / "new.txt"))
        (self.root / "new.txt").write_text("tmp", encoding="utf-8")
        restored = journal.restore("t1", self.workspace, broker=self.broker)
        self.assertIn("r:new.txt", restored)
        self.assertFalse((self.root / "new.txt").exists())

    def test_root_gone_skipped_without_raising(self):
        journal = FileJournal()
        journal.checkpoint("t1", [], self.workspace)
        journal.remember_write_external("t1", "r", "f.txt", b"original",
                                        str(self.root / "f.txt"))
        missing_broker = AccessBroker({})      # 根已不存在
        restored = journal.restore("t1", self.workspace, broker=missing_broker)
        self.assertEqual(restored, [])

    def test_readonly_root_restore_skipped(self):
        journal = FileJournal()
        journal.checkpoint("t1", [], self.workspace)
        journal.remember_write_external("t1", "r", "f.txt", b"original",
                                        str(self.root / "f.txt"))
        (self.root / "f.txt").write_text("CHANGED", encoding="utf-8")
        readonly = make_broker(self.root, writable=False)
        restored = journal.restore("t1", self.workspace, broker=readonly)
        self.assertEqual(restored, [], "只读根不允许回滚写回")


class OutcomeIntegrationTests(unittest.TestCase):
    """run_tool 对 L4 fs_* 调用的写前记账是一条完整链路，必须整条可证。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "r"
        self.root.mkdir()
        (self.root / "f.txt").write_text("original", encoding="utf-8")
        self.broker = make_broker(self.root, writable=True)
        self.registry = ToolRegistry()
        register_fs_read_tools(self.registry, self.broker, {})
        register_fs_write_tools(self.registry, self.broker, {})
        self.workspace = WorkspaceSecurity(self.base / "ws")
        self.task = TaskNode(id="t1", title="t", scope=())
        self.journal = FileJournal()
        self.journal.checkpoint("t1", [], self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    async def _run_write(self):
        call = ToolCall(id="c1", name="fs_write",
                        arguments={"root": "r", "path": "f.txt", "content": "CHANGED"},
                        effect=Effect.L4_MCP_MUTATE)
        return await run_tool(self.registry, call, journal=self.journal, task=self.task,
                              workspace=self.workspace, broker=self.broker)

    def test_l4_write_is_journaled_and_restorable(self):
        outcome = asyncio.run(self._run_write())
        self.assertTrue(outcome.ok, outcome.text)
        self.assertEqual((self.root / "f.txt").read_text(encoding="utf-8"), "CHANGED")
        restored = self.journal.restore("t1", self.workspace, broker=self.broker)
        self.assertIn("r:f.txt", restored)
        self.assertEqual((self.root / "f.txt").read_text(encoding="utf-8"), "original")

    def test_l0_read_is_not_journaled(self):
        call = ToolCall(id="c2", name="fs_read",
                        arguments={"root": "r", "path": "f.txt"},
                        effect=Effect.L0_READ)
        outcome = asyncio.run(run_tool(self.registry, call, journal=self.journal,
                                       task=self.task, workspace=self.workspace,
                                       broker=self.broker))
        self.assertTrue(outcome.ok, outcome.text)
        self.assertEqual(self.journal.external_changed_paths("t1"), [],
                         "只读调用不得记账")


if __name__ == "__main__":
    unittest.main()