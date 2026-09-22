# -*- coding: utf-8 -*-
"""FileJournal 的单测：写前快照与回滚。**回滚错了比不回滚更危险**，所以逐条验证。"""
import tempfile
import unittest
from pathlib import Path

from tools.workspace import WorkspaceSecurity

from agents.state_loop.journal import FileJournal


class JournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = WorkspaceSecurity(self.root)
        self.journal = FileJournal()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    # ---------------- 检查点 ----------------
    def test_checkpoint_records_existing_files_only(self):
        self.write("app/main.py", "原始内容")
        self.journal.checkpoint("t1", ["app/main.py", "app/missing.py"], self.workspace)
        info = self.journal.info("t1")
        self.assertIsNotNone(info)
        self.assertEqual(info.paths, ("app/main.py",))

    def test_checkpoint_is_idempotent(self):
        """
        重复建立检查点必须返回同一个点。

        否则第二次会把「已经被改过的内容」当成原始内容，回滚就回不到任务开始前。
        """
        self.write("a.py", "v1")
        first = self.journal.checkpoint("t1", ["a.py"], self.workspace)
        self.write("a.py", "v2")
        second = self.journal.checkpoint("t1", ["a.py"], self.workspace)
        self.assertEqual(first, second)

    def test_out_of_workspace_path_is_not_snapshotted(self):
        self.journal.checkpoint("t1", ["../secret.txt"], self.workspace)
        self.assertEqual(self.journal.changed_paths("t1"), [])

    # ---------------- 记账 ----------------
    def test_first_write_wins(self):
        """保留第一次的字节，那才是「任务开始前」的真实内容。"""
        self.write("a.py", "原始")
        self.journal.checkpoint("t1", [], self.workspace)
        self.journal.remember_write("t1", "a.py", b"\xe5\x8e\x9f\xe5\xa7\x8b")
        self.journal.remember_write("t1", "a.py", b"middle")
        self.write("a.py", "被改坏了")
        self.journal.restore("t1", self.workspace)
        self.assertEqual(self.read("a.py"), "原始")

    def test_changed_paths_preserves_order(self):
        self.journal.checkpoint("t1", [], self.workspace)
        self.journal.remember_write("t1", "b.py", None)
        self.journal.remember_write("t1", "a.py", None)
        self.assertEqual(self.journal.changed_paths("t1"), ["b.py", "a.py"])

    def test_backslash_paths_are_normalized(self):
        self.journal.checkpoint("t1", [], self.workspace)
        self.journal.remember_write("t1", "app\\main.py", None)
        self.assertEqual(self.journal.changed_paths("t1"), ["app/main.py"])

    def test_has_writes_reflects_reality_not_claims(self):
        """
        `has_writes` 是模型声称「我已修改」时的唯一事实来源。
        没记过写入就是没写过——不看模型说了什么。
        """
        self.journal.checkpoint("t1", [], self.workspace)
        self.assertFalse(self.journal.has_writes("t1"))
        self.journal.remember_write("t1", "a.py", None)
        self.assertTrue(self.journal.has_writes("t1"))

    def test_remember_write_without_checkpoint_is_ignored(self):
        self.journal.remember_write("nobody", "a.py", None)
        self.assertEqual(self.journal.changed_paths("nobody"), [])

    # ---------------- 回滚 ----------------
    def test_restore_reverts_modified_file(self):
        self.write("a.py", "原始")
        self.journal.checkpoint("t1", ["a.py"], self.workspace)
        self.journal.remember_write("t1", "a.py", self.root.joinpath("a.py").read_bytes())
        self.write("a.py", "改坏了")
        restored = self.journal.restore("t1", self.workspace)
        self.assertEqual(restored, ["a.py"])
        self.assertEqual(self.read("a.py"), "原始")

    def test_restore_deletes_file_that_did_not_exist(self):
        """`before is None` 表示原先不存在 → 回滚应当删除，而不是写空内容。"""
        self.journal.checkpoint("t1", [], self.workspace)
        self.journal.remember_write("t1", "created.py", None)
        self.write("created.py", "新文件")
        restored = self.journal.restore("t1", self.workspace)
        self.assertEqual(restored, ["created.py"])
        self.assertFalse((self.root / "created.py").exists())

    def test_restore_tolerates_already_deleted_target(self):
        self.journal.checkpoint("t1", [], self.workspace)
        self.journal.remember_write("t1", "gone.py", None)
        self.assertEqual(self.journal.restore("t1", self.workspace), [])

    def test_restore_without_checkpoint_is_noop(self):
        self.assertEqual(self.journal.restore("unknown", self.workspace), [])

    def test_forget_releases_bytes(self):
        self.write("a.py", "内容")
        self.journal.checkpoint("t1", ["a.py"], self.workspace)
        self.journal.forget("t1")
        self.assertIsNone(self.journal.info("t1"))
        self.assertEqual(self.journal.changed_paths("t1"), [])

    def test_read_before_returns_none_for_missing_file(self):
        self.assertIsNone(FileJournal.read_before(self.workspace, "nope.py"))

    def test_read_before_returns_bytes_for_existing_file(self):
        self.write("a.py", "内容")
        self.assertEqual(FileJournal.read_before(self.workspace, "a.py"),
                         "内容".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
