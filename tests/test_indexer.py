# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from memory.indexer import CodeIndexer
from memory.vector_store import CodeVectorStore, HashEmbedder
from tools.workspace import WorkspaceSecurity


class IndexerPurgeGuardTests(unittest.TestCase):
    def test_empty_scan_does_not_purge_existing_chunks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            src = root / "workspace"
            src.mkdir()
            (src / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
            persist = root / "chroma"
            cfg = {
                "rag": {
                    "include_ext": [".py"],
                    "ignore": [],
                    "chunk_size": 400,
                    "chunk_overlap": 40,
                    "max_file_size_kb": 512,
                    "use_tree_sitter": False,
                }
            }
            store = CodeVectorStore(str(persist), "guard_test", HashEmbedder())
            indexer = CodeIndexer(WorkspaceSecurity(src), cfg, store)
            first = indexer.index()
            self.assertGreater(first["chunks_total"], 0)

            empty = root / "empty"
            empty.mkdir()
            indexer.ws = WorkspaceSecurity(empty)
            try:
                guarded = indexer.index()
                self.assertEqual(guarded.get("scanned"), 0)
                self.assertTrue(guarded.get("skipped_purge"))
                self.assertGreater(store.count(), 0)
            finally:
                closer = getattr(store, "close", None)
                if closer:
                    closer()


class WorkbuddyManualNotesSkipTests(unittest.TestCase):
    """data/.workbuddy/memory/ 为人工笔记，不得进入 RAG。"""

    def test_workbuddy_dir_is_hard_skipped(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            src = root / "workspace"
            notes = src / ".workbuddy" / "memory"
            notes.mkdir(parents=True)
            (notes / "MEMORY.md").write_text(
                "# 人工笔记\n不参与 Agent 记忆\n", encoding="utf-8"
            )
            (src / "app.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
            persist = root / "chroma"
            cfg = {
                "rag": {
                    "include_ext": [".py", ".md"],
                    "ignore": [],
                    "chunk_size": 400,
                    "chunk_overlap": 40,
                    "max_file_size_kb": 512,
                    "use_tree_sitter": False,
                }
            }
            store = CodeVectorStore(str(persist), "workbuddy_skip", HashEmbedder())
            indexer = CodeIndexer(WorkspaceSecurity(src), cfg, store)
            try:
                result = indexer.index()
                self.assertEqual(result["scanned"], 1)
                paths = set(store.indexed_hashes().keys())
                self.assertTrue(any(p.endswith("app.py") or p == "app.py" for p in paths))
                self.assertFalse(any(".workbuddy" in p for p in paths))
            finally:
                closer = getattr(store, "close", None)
                if closer:
                    closer()
