# -*- coding: utf-8 -*-
"""
审批通道测试（切片 5 验收清单）：

- 无答复 60s（可配短超时）→ 视为拒绝（失败关闭）
- allow / deny 决议正确送达等待者
- 决议迟到（已超时/不存在）→ 404，不崩
- pending 视图正确
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.api.routes_permissions import PermissionHub, make_confirm_handler


class HubUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_allow_decision_reaches_waiter(self):
        hub = PermissionHub(timeout_seconds=5)
        async def waiter():
            return await hub.ask("tools", (), "perm_1")
        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.01)
        self.assertEqual(hub.pending, 1)
        self.assertTrue(hub.decide("perm_1", True))
        self.assertTrue(await task)
        self.assertEqual(hub.pending, 0)

    async def test_deny_decision(self):
        hub = PermissionHub(timeout_seconds=5)
        async def waiter():
            return await hub.ask("tools", (), "perm_2")
        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.01)
        hub.decide("perm_2", False)
        self.assertFalse(await task)

    async def test_timeout_is_deny(self):
        hub = PermissionHub(timeout_seconds=0.05)
        self.assertFalse(await hub.ask("tools", (), "perm_3"))
        self.assertEqual(hub.pending, 0, "超时后必须清理登记")

    async def test_decide_after_timeout_returns_false(self):
        hub = PermissionHub(timeout_seconds=0.05)
        await hub.ask("tools", (), "perm_4")
        self.assertFalse(hub.decide("perm_4", True), "已超时的请求不应再被决议")

    async def test_pending_view(self):
        hub = PermissionHub(timeout_seconds=5)
        task = asyncio.ensure_future(hub.ask("tools", ("call-a",), "perm_5"))
        await asyncio.sleep(0.01)
        view = hub.pending_view()
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]["request_id"], "perm_5")
        hub.decide("perm_5", True)
        await task


class ConfirmHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_returns_ask_result(self):
        hub = PermissionHub(timeout_seconds=5)
        handler = make_confirm_handler(hub)
        task = asyncio.ensure_future(handler("tools", (), "perm_9"))
        await asyncio.sleep(0.01)
        self.assertTrue(hub.decide("perm_9", True))
        self.assertTrue(await task)


class AuditTests(unittest.IsolatedAsyncioTestCase):
    """切片 5 验收：审计日志条数正确——每次决议（允许/拒绝/超时）各一行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.audit = Path(self._tmp.name) / "perm.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _lines(self):
        return [json.loads(l) for l in self.audit.read_text(encoding="utf-8").splitlines()]

    async def test_allow_is_audited(self):
        hub = PermissionHub(timeout_seconds=5, audit_log=self.audit)
        task = asyncio.ensure_future(hub.ask("tools", ("call-x",), "perm_a"))
        await asyncio.sleep(0.01)
        hub.decide("perm_a", True)
        self.assertTrue(await task)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["decision"], "allow")
        self.assertEqual(lines[0]["request_id"], "perm_a")

    async def test_deny_is_audited(self):
        hub = PermissionHub(timeout_seconds=5, audit_log=self.audit)
        task = asyncio.ensure_future(hub.ask("tools", ("call-y",), "perm_b"))
        await asyncio.sleep(0.01)
        hub.decide("perm_b", False)
        self.assertFalse(await task)
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["decision"], "deny")

    async def test_timeout_is_audited(self):
        hub = PermissionHub(timeout_seconds=0.05, audit_log=self.audit)
        self.assertFalse(await hub.ask("tools", ("call-z",), "perm_c"))
        lines = self._lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["decision"], "timeout")

    async def test_rows_carry_kind_and_calls(self):
        hub = PermissionHub(timeout_seconds=5, audit_log=self.audit)
        task = asyncio.ensure_future(hub.ask("tools", ("fs_write(root=r)",), "perm_d"))
        await asyncio.sleep(0.01)
        hub.decide("perm_d", True)
        await task
        lines = self._lines()
        self.assertEqual(lines[0]["kind"], "tools")
        self.assertIn("fs_write", lines[0]["calls"][0])


if __name__ == "__main__":
    unittest.main()