# -*- coding: utf-8 -*-
"""
fs_* 本地文件工具组的单测（切片 2 验收 + 切片 4 写入/删除行为预验收）：

切片 2：只读组行为、截断与二进制保护、注册期白名单过滤
切片 4：只读根写入被拒、删除进隔离目录可还原
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from tools.base import ToolRegistry
from tools.fs_access import AccessBroker, AccessRoot
from tools.fs_tools import build_fs_tools, register_fs_read_tools, register_fs_write_tools


def make_env(writable=True):
    hold = {}
    hold["tmp"] = tempfile.TemporaryDirectory()
    base = Path(hold["tmp"].name)
    root = base / "r"
    root.mkdir()
    (root / "a.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("needle in sub\n", encoding="utf-8")
    hold["root"] = root
    hold["base"] = base
    hold["broker"] = AccessBroker(
        {"r": AccessRoot(name="r", path=root, read=True, write=writable)},
        trash_dir=base / "trash",
        audit_log=base / "audit.jsonl")
    return hold


def cleanup(hold):
    hold["tmp"].cleanup()


class ReadToolTests(unittest.TestCase):
    def setUp(self):
        self.hold = make_env()
        self.registry = ToolRegistry()
        register_fs_read_tools(self.registry, self.hold["broker"], {})

    def tearDown(self):
        cleanup(self.hold)

    def call(self, name, **kwargs):
        return self.registry.call(name, kwargs)

    def test_read_toolset_registered(self):
        self.assertEqual(sorted(self.registry.names()),
                         ["fs_list", "fs_read", "fs_roots", "fs_search", "fs_stat"])

    def test_fs_roots_lists_roots(self):
        result = self.call("fs_roots")
        self.assertTrue(result.ok)
        self.assertIn("r", result.text)

    def test_fs_read_with_lines(self):
        result = self.call("fs_read", root="r", path="a.txt")
        self.assertTrue(result.ok, result.error_message)
        self.assertIn("line2", result.text)

    def test_fs_read_unknown_root_is_pathdenied(self):
        result = self.call("fs_read", root="nope", path="a.txt")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PathTraversalError")

    def test_fs_read_truncation_notice(self):
        reg = ToolRegistry()
        register_fs_read_tools(reg, self.hold["broker"], {"max_read_bytes": 8})
        result = reg.call("fs_read", {"root": "r", "path": "a.txt"})
        self.assertTrue(result.ok)
        self.assertIn("已截断", result.text)

    def test_fs_read_binary_returns_meta_only(self):
        (self.hold["root"] / "bin.dat").write_bytes(b"\x00\x01\x02" + b"x" * 64)
        result = self.call("fs_read", root="r", path="bin.dat")
        self.assertTrue(result.ok)
        self.assertIn("二进制", result.text)

    def test_fs_search_finds_content(self):
        result = self.call("fs_search", root="r", pattern="needle")
        self.assertTrue(result.ok)
        self.assertIn("b.txt", result.text)

    def test_fs_search_filename_filter(self):
        result = self.call("fs_search", root="r", pattern="needle", filename="*.txt")
        self.assertTrue(result.ok)
        self.assertIn("b.txt", result.text)

    def test_fs_list_truncates_by_limit(self):
        reg = ToolRegistry()
        register_fs_read_tools(reg, self.hold["broker"], {"max_list_entries": 1})
        result = reg.call("fs_list", {"root": "r", "path": "."})
        self.assertTrue(result.ok)
        self.assertIn("已截断", result.text)

    def test_fs_stat(self):
        result = self.call("fs_stat", root="r", path="a.txt")
        self.assertTrue(result.ok)
        self.assertIn("文件", result.text)


class WriteToolTests(unittest.TestCase):
    """切片 4 的写入组行为（代码随切片 2 一次落地，验收归属切片 4）。"""

    def setUp(self):
        self.hold = make_env(writable=True)
        self.registry = ToolRegistry()
        register_fs_read_tools(self.registry, self.hold["broker"], {})
        register_fs_write_tools(self.registry, self.hold["broker"], {})

    def tearDown(self):
        cleanup(self.hold)

    def call(self, name, **kwargs):
        return self.registry.call(name, kwargs)

    def test_write_toolset_registered(self):
        self.assertIn("fs_write", self.registry.names())
        self.assertIn("fs_delete", self.registry.names())
        self.assertIn("fs_restore", self.registry.names())

    def test_fs_write_creates_file(self):
        result = self.call("fs_write", root="r", path="new.txt", content="hello")
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual((self.hold["root"] / "new.txt").read_text(encoding="utf-8"), "hello")

    def test_fs_edit_patch_conflict(self):
        result = self.call("fs_edit", root="r", path="a.txt",
                           old_string="not-there", new_string="x")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "PatchConflictError")

    def test_readonly_root_write_rejected(self):
        readonly = AccessBroker({"ro": AccessRoot("ro", path=self.hold["root"],
                                                  read=True, write=False)})
        reg = ToolRegistry()
        register_fs_write_tools(reg, readonly, {})
        result = reg.call("fs_write", {"root": "ro", "path": "n.txt", "content": "x"})
        self.assertFalse(result.ok)
        self.assertIn(result.error_type, ("AccessDeniedError", "PermissionError"))

    def test_fs_copy_between_roots(self):
        base = self.hold["base"]
        other = base / "o"
        other.mkdir()
        broker = AccessBroker({
            "r": AccessRoot("r", path=self.hold["root"], read=True, write=True),
            "o": AccessRoot("o", path=other, read=True, write=True),
        }, trash_dir=base / "trash")
        reg = ToolRegistry()
        register_fs_write_tools(reg, broker, {})
        res = reg.call("fs_copy", {"src_root": "r", "src_path": "a.txt",
                                   "dst_root": "o", "dst_path": "copied.txt"})
        self.assertTrue(res.ok, res.error_message)
        self.assertTrue((other / "copied.txt").exists())

    def test_fs_delete_goes_to_trash_and_restores(self):
        original = self.hold["root"] / "a.txt"
        result = self.call("fs_delete", root="r", path="a.txt")
        self.assertTrue(result.ok, result.error_message)
        self.assertFalse(original.exists(), "删除后原文件必须不存在（隔离而非真删）")
        ts = re.search(r"ts='([^']+)'", result.text).group(1)
        restored = self.call("fs_restore", ts=ts)
        self.assertTrue(restored.ok, restored.error_message)
        self.assertTrue(original.exists(), "还原后原文件必须回来")
        self.assertEqual(original.read_text(encoding="utf-8"), "line1\nline2\nline3\n")

    def test_write_is_audited(self):
        audit = self.hold["base"] / "audit.jsonl"
        self.call("fs_write", root="r", path="aud.txt", content="z")
        records = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(r["decision"] == "allow" and r["action"] == "write"
                            for r in records))


class RegistrationGateTests(unittest.TestCase):
    """N3 默认关闭：enabled=false 时不注册任何 fs_*/sys_* 工具，技能名单不变。"""

    def test_disabled_config_registers_nothing(self):
        import agents.agent as agent_mod
        registry = ToolRegistry()
        skill_names = []
        cfg = {"local_access": {"enabled": False}, "system": {"enabled": False}}
        broker = agent_mod._build_local_access(cfg, registry, skill_names)
        self.assertIsNone(broker)
        self.assertEqual(registry.names(), [])
        self.assertEqual(skill_names, [])

    def test_enabled_readonly_config_registers_only_readset(self):
        import agents.agent as agent_mod
        hold = make_env(writable=False)
        self.addCleanup(cleanup, hold)
        registry = ToolRegistry()
        skill_names = []
        cfg = {
            "local_access": {
                "enabled": True,
                "roots": [{"name": "r", "path": str(hold["root"]),
                           "read": True, "write": False}],
                "trash_dir": str(hold["base"] / "trash"),
                "audit_log": "",
            },
            "system": {"enabled": False},
        }
        broker = agent_mod._build_local_access(cfg, registry, skill_names)
        self.assertIsNotNone(broker)
        self.assertIn("fs_read", registry.names())
        self.assertNotIn("fs_write", registry.names(), "只读根不得注册写入工具")
        self.assertIn("local_system", skill_names)
        self.assertNotIn("local_system_write", skill_names)

    def test_build_fs_tools_respects_writable_gate(self):
        hold = make_env(writable=False)
        self.addCleanup(cleanup, hold)
        tools = build_fs_tools(hold["broker"])
        names = [t.name for t in tools]
        self.assertIn("fs_read", names)
        self.assertNotIn("fs_write", names)


if __name__ == "__main__":
    unittest.main()