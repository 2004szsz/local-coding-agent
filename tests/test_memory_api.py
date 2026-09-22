# -*- coding: utf-8 -*-
"""记忆状态 API：会话历史 + RAG，供设置页展示。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG
from memory.base import RagMemory
from memory.vector_store import CodeVectorStore, HashEmbedder


class RagMemoryAdapterTests(unittest.TestCase):
    def test_remember_recall_via_vector_store(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = CodeVectorStore(str(Path(tmp) / "chroma"), "rag_mem", HashEmbedder())
            try:
                mem = RagMemory(store)
                self.assertEqual(mem.kind, "rag")
                mem.remember("demo.py", {
                    "ids": ["demo#0"],
                    "documents": ["def hello():\n    return 1\n"],
                    "metadatas": [{"path": "demo.py", "name": "hello", "kind": "function"}],
                })
                hits = mem.recall("hello function")
                self.assertTrue(hits)
                self.assertEqual(hits[0]["path"], "demo.py")
            finally:
                closer = getattr(store, "close", None)
                if closer:
                    closer()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            empty = CodeVectorStore(str(Path(tmp) / "chroma"), "empty_rag", HashEmbedder())
            try:
                with self.assertRaises(KeyError):
                    RagMemory(empty).recall("anything")
            finally:
                closer = getattr(empty, "close", None)
                if closer:
                    closer()


class MemoryStatusApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()
        (cls.workspace / "demo.py").write_text("def hello():\n    return 1\n", encoding="utf-8")

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.workspace)
        cfg["llm"]["embedding"]["enabled"] = False
        cfg["rag"]["enabled"] = True
        cfg["rag"]["persist_dir"] = str(cls.root / "chroma")
        cfg["rag"]["collection"] = "test_memory_status"
        cfg["storage"]["history_db"] = str(cls.root / "history.db")
        cfg["storage"]["sessions_dir"] = str(cls.root / "sessions")
        cfg["storage"]["preferences_path"] = str(cls.root / "preferences.json")
        cfg["local_access"]["enabled"] = False
        cfg["system"]["enabled"] = False
        cfg.setdefault("mcp", {})["servers"] = []
        cfg["modules"] = {"auto_rag_index": False}

        import app.main as main_module

        cls._main = main_module
        cls._original_load_config = main_module.load_config
        main_module.load_config = lambda *args, **kwargs: copy.deepcopy(cfg)

        cls._client_cm = TestClient(main_module.app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._client_cm.__exit__(None, None, None)
        finally:
            cls._main.load_config = cls._original_load_config
            cls._tmp.cleanup()

    def test_memory_status_shape(self):
        resp = self.client.get("/api/memory/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("sessions", body)
        self.assertIn("rag", body)
        self.assertIn("preferences", body)
        sess = body["sessions"]
        for key in ("db_name", "db_bytes", "session_count", "message_count"):
            self.assertIn(key, sess)
        self.assertEqual(sess["db_name"], "history.db")
        rag = body["rag"]
        self.assertTrue(rag["enabled"])
        self.assertIn("chunks", rag)
        self.assertIn("embedder", rag)
        self.assertEqual(rag["embedder_mode"], "hash")
        pref = body["preferences"]
        self.assertIn("count", pref)
        self.assertIn("max_items", pref)

    def test_health_mcp_empty_not_default_memory(self):
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json().get("mcp"), [])

    def test_clear_all_sessions(self):
        created = self.client.post("/api/sessions", json={"title": "t"}).json()
        store = self._main.app.state.sessions
        store.append_message(created["id"], "user", "hi")

        before = self.client.get("/api/memory/status").json()["sessions"]
        self.assertGreaterEqual(before["session_count"], 1)
        self.assertGreaterEqual(before["message_count"], 1)

        cleared = self.client.delete("/api/sessions")
        self.assertEqual(cleared.status_code, 200)
        self.assertTrue(cleared.json()["ok"])
        self.assertGreaterEqual(cleared.json()["deleted"], 1)

        after = self.client.get("/api/memory/status").json()["sessions"]
        self.assertEqual(after["session_count"], 0)
        self.assertEqual(after["message_count"], 0)


if __name__ == "__main__":
    unittest.main()
