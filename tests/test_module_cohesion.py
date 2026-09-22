# -*- coding: utf-8 -*-
"""模块协同：规则加载、状态 API、adapt 入口。"""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG
from app.modules.coordinator import ModuleCoordinator
from app.modules.rules import (
    context_snippet_for_modules,
    extract_rule_sections,
    load_module_rules,
    load_rules_markdown,
)


class ModuleRulesTests(unittest.TestCase):
    def test_load_module_rules(self):
        rules = load_module_rules(force=True)
        self.assertIn("modules", rules)
        self.assertIn("rag", rules["modules"])
        self.assertTrue(rules["modules"]["rag"].get("auto_adapt"))

    def test_load_rules_markdown(self):
        md = load_rules_markdown(force=True)
        self.assertTrue(md.get("exists"))
        self.assertIn("模块协同", md.get("content", ""))

    def test_extract_rag_sections(self):
        excerpt = extract_rule_sections("rag")
        self.assertIn("RAG", excerpt)

    def test_context_snippet(self):
        snippet = context_snippet_for_modules(["rag", "agent"])
        self.assertIn("知识库", snippet)
        self.assertIn("循环", snippet)


class ModuleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.workspace = cls.root / "workspace"
        cls.workspace.mkdir()
        (cls.workspace / "demo.py").write_text("print('hi')\n", encoding="utf-8")

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.workspace)
        cfg["llm"]["embedding"]["enabled"] = False
        cfg["rag"]["enabled"] = True
        cfg["storage"]["history_db"] = str(cls.root / "history.db")
        cfg["storage"]["sessions_dir"] = str(cls.root / "sessions")
        cfg["storage"]["preferences_path"] = str(cls.root / "preferences.json")
        cfg["rag"]["persist_dir"] = str(cls.root / "chroma")
        cfg["rag"]["collection"] = "test_module_cohesion"
        cfg["local_access"]["enabled"] = False
        cfg["system"]["enabled"] = False
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
            try:
                cls._tmp.cleanup()
            except OSError:
                pass

    def test_rules_endpoint(self):
        r = self.client.get("/api/modules/rules")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("rag", body.get("modules", {}))
        self.assertTrue(body.get("context_snippet"))

    def test_status_endpoint(self):
        r = self.client.get("/api/modules/status")
        self.assertEqual(r.status_code, 200)
        modules = r.json().get("modules") or []
        ids = {m["id"] for m in modules}
        self.assertEqual(ids, {"fs", "rag", "agent", "access"})

    def test_adapt_agent(self):
        r = self.client.post("/api/modules/adapt/agent")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("framework", body)
        self.assertEqual(body.get("module"), "agent")

    def test_adapt_access_is_registered_not_404(self):
        r = self.client.post("/api/modules/adapt/access")
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body.get("module"), "access")
        self.assertEqual(body.get("action"), "verify_access")
        self.assertIn("gate", body)
        self.assertEqual(body["gate"].get("fs_write"), "L4")
        self.assertIn("message", body)

    def test_adapt_unknown_module(self):
        r = self.client.post("/api/modules/adapt/unknown")
        self.assertEqual(r.status_code, 404)

    def test_adapt_rag_indexes_workspace(self):
        r = self.client.post("/api/modules/adapt/rag")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body.get("module"), "rag")
        self.assertTrue(body.get("ok"), msg=body)
        self.assertGreaterEqual(int((body.get("stats") or {}).get("scanned", 0)), 1)

    def test_modules_routes_are_registered(self):
        paths = set(self.client.get("/openapi.json").json().get("paths") or {})
        self.assertIn("/api/modules/status", paths)
        self.assertIn("/api/modules/adapt/{module_id}", paths)


class ModuleCoordinatorTests(unittest.TestCase):
    def test_module_status_shape(self):
        class FakeState:
            rag_enabled = False
            framework_name = "native_react"
            cfg = {"local_access": {"enabled": False}}
            runtime = None
            vector_store = None
            indexer = None
            rag_daemon = None

        coord = ModuleCoordinator()
        result = coord.module_status(FakeState())
        self.assertEqual(len(result["modules"]), 4)

    def test_access_ready_when_broker_mounted(self):
        class Tools:
            def names(self):
                return ["fs_read", "fs_write", "read_file"]

        class Runtime:
            broker = object()
            tools = Tools()

        class FakeState:
            cfg = {"local_access": {"enabled": False}}
            runtime = Runtime()
            broker = Runtime.broker

        status = ModuleCoordinator()._access_status(FakeState())
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["value"], "本机已授权")

    def test_adapt_access_reports_gate_even_when_restricted(self):
        class FakeState:
            cfg = {"local_access": {"enabled": False}}
            runtime = None
            broker = None

        result = ModuleCoordinator().adapt("access", FakeState())
        self.assertEqual(result["module"], "access")
        self.assertFalse(result["ok"])
        self.assertEqual(result["gate"]["fs_write"], "L4")
        self.assertEqual(result["gate"]["fs_read"], "L0")
        self.assertIn("本机", result["message"])

    def test_adapt_rag_propagates_daemon_failure(self):
        class Daemon:
            def run_index_now(self):
                return {"ok": False, "message": "embed boom"}

        class FakeState:
            rag_enabled = True
            indexer = object()
            rag_daemon = Daemon()

        result = ModuleCoordinator().adapt("rag", FakeState())
        self.assertFalse(result["ok"])
        self.assertIn("embed boom", result["message"])

    def test_rag_ready_when_chunks_exist(self):
        class Store:
            def count(self):
                return 12

        class FakeState:
            rag_enabled = True
            vector_store = Store()
            indexer = object()
            rag_daemon = None

        status = ModuleCoordinator()._rag_status(FakeState())
        self.assertEqual(status["status"], "ready")
        self.assertIn("12", status["value"])

    def test_rag_empty_workspace_is_warn_not_pending(self):
        class Store:
            def count(self):
                return 0

        class Indexer:
            def _scan_files(self):
                return []

        class FakeState:
            rag_enabled = True
            vector_store = Store()
            indexer = Indexer()
            rag_daemon = None

        status = ModuleCoordinator()._rag_status(FakeState())
        self.assertEqual(status["status"], "warn")
        self.assertEqual(status["value"], "空工作区")


class RagDaemonNeedsIndexTests(unittest.TestCase):
    def test_needs_index_when_collection_already_populated(self):
        from app.modules.rag_daemon import RagAutoIndexDaemon

        class Store:
            def count(self):
                return 1712

        class FakeApp:
            rag_enabled = True
            indexer = object()
            vector_store = Store()

        daemon = RagAutoIndexDaemon(FakeApp())
        self.assertTrue(
            daemon.needs_index(),
            "已有切片时仍应做增量索引，否则工作区变更后知识库与 Agent 检索脱节",
        )
