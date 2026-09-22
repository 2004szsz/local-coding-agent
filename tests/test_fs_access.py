# -*- coding: utf-8 -*-
"""
AccessBroker 门闩的单测（切片 1 验收清单）：

- 越界路径被拒（未知根 / 相对穿越 / 绝对逃逸）
- junction 指向外部被拒（Windows，mklink 失败则跳过）
- 8.3 短名绕过被拒（Windows，C:\\PROGRA~1 存在才测）
- 拒绝清单命中被拒（敏感目录 / 凭据文件 / 后缀 / 目录前缀）
- 只读根写入被拒；限流生效；审计落盘
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.fs_access import (
    AccessBroker,
    AccessDeniedError,
    AccessRoot,
)
from tools.workspace import PathTraversalError

IS_WINDOWS = sys.platform == "win32"


def make_broker(root_dirs, **kwargs):
    """构造一个根为临时目录（可写）的 broker，供多数用例复用。"""
    roots = {name: AccessRoot(name=name, path=Path(p), read=True, write=True)
             for name, p in root_dirs.items()}
    return AccessBroker(roots=roots, **kwargs)


class ResolveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root_a = self.base / "a"
        self.root_b = self.base / "b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        (self.root_a / "hello.txt").write_text("hi", encoding="utf-8")
        self.broker = make_broker({"a": self.root_a, "b": self.root_b})

    def tearDown(self):
        self._tmp.cleanup()

    def test_unknown_root_rejected(self):
        with self.assertRaises(PathTraversalError):
            self.broker.resolve("nope", "hello.txt")

    def test_empty_relpath_is_root_itself(self):
        self.assertEqual(self.broker.resolve("a", "").resolve(), self.root_a.resolve())

    def test_relative_traversal_rejected(self):
        with self.assertRaises(PathTraversalError):
            self.broker.resolve("a", "../b/x.txt")

    def test_absolute_escape_rejected(self):
        outside = self.base / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(PathTraversalError):
            self.broker.resolve("a", str(outside))

    def test_inside_is_allowed_and_readable(self):
        p = self.broker.resolve("a", "hello.txt")
        self.assertEqual(p.name, "hello.txt")
        self.assertEqual(self.broker.read_text("a", "hello.txt"), "hi")

    def test_readonly_root_write_rejected(self):
        readonly = AccessBroker({"_r": AccessRoot("_r", path=self.root_a,
                                                  read=True, write=False)})
        with self.assertRaises(AccessDeniedError):
            readonly.resolve("_r", "x.txt", need_write=True)

    def test_gbk_text_decoding(self):
        (self.root_a / "gbk.txt").write_bytes("中文内容".encode("gbk"))
        self.assertEqual(self.broker.read_text("a", "gbk.txt"), "中文内容")


class DenyListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "r"
        self.root.mkdir()
        self.broker = make_broker({"r": self.root})

    def tearDown(self):
        self._tmp.cleanup()

    def test_sensitive_dir_rejected(self):
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_rsa").write_text("k", encoding="utf-8")
        for target in (".ssh/id_rsa", ".aws/credentials"):
            with self.assertRaises(AccessDeniedError, msg=target):
                self.broker.resolve("r", target)

    def test_credential_file_names_rejected(self):
        for name in ("id_rsa", "id_ed25519", ".env", ".npmrc", "Cookies"):
            (self.root / name).write_text("s", encoding="utf-8")
            with self.assertRaises(AccessDeniedError, msg=name):
                self.broker.resolve("r", name)

    def test_credential_suffixes_rejected(self):
        for name in ("a.pem", "b.key", "c.pfx"):
            (self.root / name).write_text("s", encoding="utf-8")
            with self.assertRaises(AccessDeniedError, msg=name):
                self.broker.resolve("r", name)

    def test_custom_denied_prefix_cross_platform(self):
        """注入自定义前缀即可跨平台验证「目录前缀拒绝」语义。"""
        (self.base / "sys").mkdir()
        broker = AccessBroker(
            {"s": AccessRoot("s", path=self.base, read=True, write=True)},
            denied_prefixes=(str(self.base / "sys"),),
        )
        with self.assertRaises(AccessDeniedError):
            broker.resolve("s", "sys/config.ini")

    def test_from_config_skips_root_inside_deny_list(self):
        if not IS_WINDOWS:
            self.skipTest("拒绝清单目录为 Windows 系统目录")
        broker = AccessBroker.from_config(
            {"roots": [{"name": "sys", "path": r"C:\Windows"}]},
            project_root=self.base)
        self.assertEqual(broker.roots, {})
        self.assertTrue(any("拒绝清单" in w for w in broker.warnings))

    def test_from_config_skips_relative_and_bad_roots(self):
        broker = AccessBroker.from_config(
            {"roots": [
                {"name": "rel", "path": "relative/path"},
                {"path": str(self.root)},
            ]},
            project_root=self.base)
        self.assertEqual(broker.roots, {})
        self.assertEqual(len(broker.warnings), 2)


@unittest.skipUnless(IS_WINDOWS, "Windows 专属: junction 逃逸")
class JunctionTests(unittest.TestCase):
    def test_junction_to_outside_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        root = base / "r"
        outside = base / "o"
        root.mkdir()
        outside.mkdir()
        (outside / "x.txt").write_text("x", encoding="utf-8")

        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(root / "link"), str(outside)],
                check=True, capture_output=True, timeout=15)
        except (subprocess.CalledProcessError, OSError):
            self.skipTest("mklink /J 不可用（权限或环境限制）")

        broker = make_broker({"r": root})
        with self.assertRaises(PathTraversalError):
            broker.resolve("r", "link/x.txt")

    def test_short_name_bypass_rejected(self):
        if not Path(r"C:\PROGRA~1").exists():
            self.skipTest("本机无 C:\\PROGRA~1 短名")
        broker = AccessBroker({"_c": AccessRoot("_c", path=Path("C:\\"),
                                                read=True, write=False)})
        with self.assertRaises(AccessDeniedError):
            broker.resolve("_c", "PROGRA~1")


class AuditAndRateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "r"
        self.root.mkdir()
        (self.root / "f.txt").write_text("f", encoding="utf-8")
        self.audit = self.base / "audit.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_audit_records_allow_and_deny(self):
        broker = make_broker({"r": self.root}, audit_log=self.audit)
        broker.resolve("r", "f.txt")
        try:
            broker.resolve("r", "..")
        except PathTraversalError:
            pass
        lines = [json.loads(l) for l in self.audit.read_text(encoding="utf-8").splitlines()]
        decisions = [l["decision"] for l in lines]
        self.assertIn("allow", decisions)
        self.assertIn("deny", decisions)
        self.assertTrue(all({"ts", "root", "path", "action", "decision", "reason"} <= set(l)
                            for l in lines))

    def test_rate_limit(self):
        broker = make_broker({"r": self.root}, rate_per_minute=2)
        broker.resolve("r", "f.txt")
        broker.resolve("r", "f.txt")
        with self.assertRaises(AccessDeniedError):
            broker.resolve("r", "f.txt")


if __name__ == "__main__":
    unittest.main()