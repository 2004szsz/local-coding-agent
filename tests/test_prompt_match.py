# -*- coding: utf-8 -*-
"""提示词模板后台匹配单元测试。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG
from app.prompt_match.matcher import build_match_status, match_model


class PromptMatchMatcherTests(unittest.TestCase):
    def test_match_deepseek(self):
        self.assertEqual(match_model("deepseek-chat"), "deepseek")

    def test_match_gpt(self):
        self.assertEqual(match_model("gpt-4o-mini"), "gpt")

    def test_match_general_fallback(self):
        self.assertEqual(match_model("unknown-model-x"), "general")
        self.assertEqual(match_model(""), "general")

    def test_build_status_matched(self):
        st = build_match_status("deepseek-chat", provider="custom")
        self.assertEqual(st["status"], "matched")
        self.assertEqual(st["family"], "deepseek")
        self.assertIn("deepseek-chat", st["matched_text"])

    def test_build_status_pending(self):
        st = build_match_status("")
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["family"], "general")


class PromptMatchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.workspace)
        cfg["llm"]["model"] = "deepseek-chat"
        cfg["llm"]["provider"] = "custom"
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

    def test_status_ready(self):
        r = self.client.get("/api/prompt-match/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        status = body.get("status") or {}
        self.assertEqual(status.get("model"), "deepseek-chat")
        self.assertEqual(status.get("family"), "deepseek")
        self.assertEqual(status.get("status"), "matched")

    def test_health_and_match_consistent(self):
        health = self.client.get("/api/health").json()
        match = self.client.get("/api/prompt-match/status").json()["status"]
        self.assertEqual(match.get("model"), health.get("model"))


if __name__ == "__main__":
    unittest.main()
