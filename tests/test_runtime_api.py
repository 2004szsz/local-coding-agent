# -*- coding: utf-8 -*-
"""运行时项目 API：添加/切换项目热重载 fs_* / sys_*，无需重启。"""
import copy
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG
from app.local_runtime import (
    LocalRuntimeState,
    ProjectEntry,
    apply_runtime_to_config,
    load_runtime_state,
    save_runtime_state,
)


class RuntimeConfigMergeTests(unittest.TestCase):
    def test_apply_runtime_enables_local_access_and_system(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "myapp"
            project.mkdir()
            state = LocalRuntimeState(
                projects=[ProjectEntry(id="p1", name="myapp", path=str(project), write=True)],
                active_project_id="p1",
            )
            cfg = copy.deepcopy(DEFAULT_CONFIG)
            apply_runtime_to_config(cfg, state)
            self.assertEqual(cfg["server"]["workspace_root"], str(project.resolve()))
            self.assertTrue(cfg["local_access"]["enabled"])
            self.assertEqual(len(cfg["local_access"]["roots"]), 1)
            self.assertTrue(cfg["system"]["enabled"])
            self.assertIn("notify", cfg["system"]["allow_actions"])


class VanishedProjectTests(unittest.TestCase):
    def test_from_dict_drops_missing_project_dirs(self):
        state = LocalRuntimeState.from_dict({
            "projects": [
                {"id": "gone", "name": "gone", "path": r"C:\definitely-not-a-real-project-xyz"},
            ],
            "active_project_id": "gone",
        })
        self.assertEqual(state.projects, [])
        self.assertIsNone(state.active_project_id)


class RuntimeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.project = cls.root / "proj"
        cls.project.mkdir()
        cls.runtime_file = cls.root / "local_runtime.json"

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.root / "workspaces" / "demo")
        cfg["llm"]["embedding"]["enabled"] = False
        cfg["rag"]["enabled"] = False
        cfg["storage"]["history_db"] = str(cls.root / "history.db")
        cfg["storage"]["sessions_dir"] = str(cls.root / "sessions")
        cfg["storage"]["preferences_path"] = str(cls.root / "preferences.json")
        cfg["local_access"]["enabled"] = False
        cfg["system"]["enabled"] = False
        cls.cfg = cfg

        import app.local_runtime as lr_mod
        import app.api.routes_runtime as rt_mod
        import app.main as main_module

        cls._lr_mod = lr_mod
        cls._rt_mod = rt_mod
        cls._main = main_module
        cls._orig_runtime_file = lr_mod.RUNTIME_FILE
        lr_mod.RUNTIME_FILE = cls.runtime_file

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
            cls._lr_mod.RUNTIME_FILE = cls._orig_runtime_file
            cls._tmp.cleanup()

    def setUp(self):
        if self.runtime_file.exists():
            self.runtime_file.unlink()

    def test_add_project_registers_fs_tools(self):
        resp = self.client.post("/api/runtime/projects", json={
            "path": str(self.project),
            "name": "proj",
            "write": True,
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["local_access"]["enabled"])
        self.assertGreater(len(body["tools"]["fs"]), 0)
        self.assertEqual(body["workspace_root"], str(self.project.resolve()))

        health = self.client.get("/api/health")
        self.assertIn("fs_roots", health.json()["tools"])

    def test_capabilities_toggle_system(self):
        self.client.post("/api/runtime/projects", json={"path": str(self.project)})
        resp = self.client.put("/api/runtime/capabilities", json={"system": True})
        self.assertEqual(resp.status_code, 200)
        tools = resp.json()["tools"]["sys"]
        try:
            import psutil  # noqa: F401
            self.assertGreater(len(tools), 0)
        except ImportError:
            self.assertEqual(tools, [])


if __name__ == "__main__":
    unittest.main()
