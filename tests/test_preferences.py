# -*- coding: utf-8 -*-
"""用户偏好记忆：存储、提示词注入、API CRUD。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agents.agent import compose_system_prompt, load_agent_spec
from agents.state_loop.permissions import TOOL_EFFECTS, classify
from agents.state_loop.state import Effect
from app.config import DEFAULT_CONFIG
from memory.preferences import PreferenceError, PreferenceStore
from tools.base import ToolRegistry
from tools.preference_tools import register_preference_tools


class PreferenceStoreTests(unittest.TestCase):
    def test_add_list_update_delete_and_prompt(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = PreferenceStore(Path(tmp) / "preferences.json", max_items=10)
            item = store.add("回复时先给结论再给细节", source="user")
            self.assertTrue(item["enabled"])
            self.assertEqual(item["source"], "user")

            section = store.prompt_section()
            self.assertIn("用户偏好记忆", section)
            self.assertIn("先给结论", section)

            store.update(item["id"], enabled=False)
            self.assertEqual(store.prompt_section(), "")

            store.update(item["id"], enabled=True, content="项目用 React")
            self.assertIn("React", store.prompt_section())

            self.assertTrue(store.delete(item["id"]))
            self.assertEqual(store.list_items(), [])
            self.assertEqual(store.clear(), 0)

    def test_capacity_max_items(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = PreferenceStore(Path(tmp) / "p.json", max_items=2)
            store.add("a")
            store.add("b")
            with self.assertRaises(PreferenceError):
                store.add("c")

    def test_persist_reload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "p.json"
            PreferenceStore(path).add("跨会话约定", source="conversation_summary")
            again = PreferenceStore(path)
            items = again.list_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"], "conversation_summary")


class PreferencePromptAndGateTests(unittest.TestCase):
    def test_compose_injects_preferences(self):
        spec = load_agent_spec()
        prompt = compose_system_prompt(
            spec, preferences_section="## 用户偏好记忆\n- 项目用 React"
        )
        self.assertIn("用户偏好记忆", prompt)
        self.assertIn("项目用 React", prompt)
        self.assertIn("user_preferences", prompt)

    def test_tool_effects_are_l2_for_writes(self):
        self.assertEqual(TOOL_EFFECTS["preference_list"], Effect.L0_READ)
        self.assertEqual(TOOL_EFFECTS["preference_add"], Effect.L2_SANDBOX)
        self.assertEqual(TOOL_EFFECTS["preference_update"], Effect.L2_SANDBOX)
        self.assertEqual(TOOL_EFFECTS["preference_delete"], Effect.L2_SANDBOX)
        registry = ToolRegistry()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            store = PreferenceStore(Path(tmp) / "p.json")
            register_preference_tools(registry, store)
            self.assertEqual(
                classify("preference_add", {"content": "x"}, registry, None).effect,
                Effect.L2_SANDBOX,
            )
            self.assertEqual(
                classify("preference_list", {}, registry, None).effect,
                Effect.L0_READ,
            )


class PreferenceApiTests(unittest.TestCase):
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
        cfg["rag"]["collection"] = "test_pref_api"
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

    def test_crud_and_clear(self):
        created = self.client.post(
            "/api/memory/preferences",
            json={"content": "回复时先给结论再给细节", "source": "user"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        item = created.json()
        self.assertTrue(item["id"])

        listed = self.client.get("/api/memory/preferences")
        self.assertEqual(listed.status_code, 200)
        self.assertGreaterEqual(len(listed.json()["items"]), 1)

        patched = self.client.patch(
            f"/api/memory/preferences/{item['id']}",
            json={"enabled": False},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertFalse(patched.json()["enabled"])

        status = self.client.get("/api/memory/status")
        self.assertEqual(status.status_code, 200)
        self.assertIn("preferences", status.json())
        self.assertIn("count", status.json()["preferences"])

        health = self.client.get("/api/health")
        self.assertIn("preferences", health.json())
        tools = health.json().get("tools") or []
        self.assertIn("preference_list", tools)
        self.assertIn("preference_add", tools)

        cleared = self.client.post("/api/memory/clear")
        self.assertEqual(cleared.status_code, 200)
        self.assertTrue(cleared.json()["ok"])
        after = self.client.get("/api/memory/preferences").json()["items"]
        self.assertEqual(after, [])


if __name__ == "__main__":
    unittest.main()
