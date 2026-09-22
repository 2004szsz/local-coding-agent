# -*- coding: utf-8 -*-
"""IDE 上下文后台 API 冒烟测试。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG


class ContextApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.workspace)
        cfg["llm"]["embedding"]["enabled"] = False
        cfg["rag"]["enabled"] = False
        cfg["storage"]["history_db"] = str(cls.root / "history.db")
        cfg["storage"]["sessions_dir"] = str(cls.root / "sessions")
        cfg["storage"]["preferences_path"] = str(cls.root / "preferences.json")
        cfg["local_access"]["enabled"] = False
        cfg["system"]["enabled"] = False

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

    def test_snapshot_ready(self):
        r = self.client.get("/api/context/snapshot")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        snap = body.get("snapshot") or {}
        self.assertIn("present", snap)
        self.assertIn("seq", snap)

    def test_editor_and_edit_flow(self):
        r = self.client.post("/api/context/editor", json={
            "path": "src/demo.js",
            "content": "export function hello() {}\n",
            "line": 1,
            "col": 1,
            "selection": "hello",
        })
        self.assertEqual(r.status_code, 200)
        snap = self.client.get("/api/context/snapshot").json()["snapshot"]
        self.assertEqual(snap["openFile"]["path"], "src/demo.js")
        self.assertEqual(snap["selection"], "hello")

        self.client.post("/api/context/edit", json={
            "path": "src/demo.js",
            "summary": "测试编辑",
        })
        snap2 = self.client.get("/api/context/snapshot").json()["snapshot"]
        self.assertTrue(snap2["present"]["recentEdits"])

    def test_terminal_append(self):
        self.client.post("/api/context/terminal", json={"line": "$ npm test"})
        snap = self.client.get("/api/context/snapshot").json()["snapshot"]
        self.assertIn("npm test", "\n".join(snap.get("terminalLines") or []))


if __name__ == "__main__":
    unittest.main()
