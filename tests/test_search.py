# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path

from tools.base import ToolRegistry
from tools.search import build_file_search_tool
from tools.workspace import PathTraversalError, WorkspaceSecurity


class SearchTests(unittest.TestCase):
    def test_file_search_stays_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("def parse_port():\n    return 8000\n", encoding="utf-8")
            ws = WorkspaceSecurity(root)
            registry = ToolRegistry()
            registry.register(build_file_search_tool(ws))
            text = registry.execute("file_search", {"pattern": "parse_port"})
            self.assertIn("app.py:1:", text)
            with self.assertRaises(PathTraversalError):
                ws.resolve("../outside.txt")


if __name__ == "__main__":
    unittest.main()
