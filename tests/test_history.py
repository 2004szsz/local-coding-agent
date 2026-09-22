# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from memory.history import HistoryStore, SessionNotFoundError


class HistoryTests(unittest.TestCase):
    def test_session_roundtrip_and_legacy_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "sessions"
            legacy.mkdir()
            (legacy / "abc123.json").write_text(json.dumps({
                "id": "abc123",
                "title": "旧会话",
                "created_at": 1,
                "updated_at": 2,
                "messages": [{
                    "id": "m1",
                    "role": "user",
                    "content": "hello",
                    "tool_events": [],
                    "created_at": 1.5,
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with HistoryStore(str(root / "history.db"), legacy_dir=str(legacy)) as store:
                imported = store.get_session("abc123")
                self.assertEqual(imported["title"], "旧会话")
                self.assertEqual(imported["messages"][0]["content"], "hello")
                store.append_message("abc123", "assistant", "world")

            with HistoryStore(str(root / "history.db"), legacy_dir=str(legacy)) as again:
                self.assertEqual(len(again.get_session("abc123")["messages"]), 2)

            with HistoryStore(str(root / "history.db")) as store:
                created = store.create_session()
                self.assertEqual(created["title"], "新会话")
                store.append_message(created["id"], "user", "给快速排序写测试")
                renamed = store.get_session(created["id"])
                self.assertTrue(renamed["title"].startswith("给快速排序"))

                history = store.get_llm_messages(created["id"])
                self.assertEqual(history[0]["role"], "user")

                store.delete_session(created["id"])
                with self.assertRaises(SessionNotFoundError):
                    store.get_session(created["id"])

    def test_stats_and_clear_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(str(Path(tmp) / "history.db"))
            try:
                a = store.create_session("a")
                b = store.create_session("b")
                store.append_message(a["id"], "user", "hi")
                store.append_message(b["id"], "user", "yo")
                stats = store.stats()
                self.assertEqual(stats["session_count"], 2)
                self.assertEqual(stats["message_count"], 2)
                self.assertGreater(stats["db_bytes"], 0)
                self.assertEqual(stats["db_name"], "history.db")
                deleted = store.clear_all()
                self.assertEqual(deleted, 2)
                empty = store.stats()
                self.assertEqual(empty["session_count"], 0)
                self.assertEqual(empty["message_count"], 0)
            finally:
                store.close()
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(str(Path(tmp) / "history.db"))
            try:
                with self.assertRaises(ValueError):
                    store.get_session("../etc/passwd")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
