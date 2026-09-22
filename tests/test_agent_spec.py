# -*- coding: utf-8 -*-
import os
import tempfile
import unittest
from pathlib import Path

from agents.agent import compose_system_prompt, load_agent_spec, resolve_framework
from skills import SKILLS, render_skill_prompt, tools_for_skills
from tools.base import Tool, ToolRegistry


class AgentSpecTests(unittest.TestCase):
    def test_yaml_skills_and_prompts_line_up(self):
        spec = load_agent_spec()
        self.assertEqual(spec["name"], "local-coding-agent")
        for name in spec["skills"]:
            self.assertIn(name, SKILLS)
        prompt = compose_system_prompt(spec)
        self.assertIn(spec["role"], prompt)
        self.assertIn("先定位再修改", prompt)
        self.assertIn("任务流程", prompt)
        self.assertIn("已启用技能", prompt)
        self.assertIn("agents/agent.yaml", prompt)
        self.assertIn("tools/workspace.py", prompt)
        self.assertNotIn("auth/jwt.py", prompt)
        self.assertIn("data_analysis", render_skill_prompt(spec["skills"]))

    def test_disabling_a_skill_drops_its_tools(self):
        read_only = tools_for_skills(["data_analysis", "report_generation"])
        self.assertIn("file_search", read_only)
        self.assertNotIn("edit_file", read_only)
        self.assertNotIn("run_python_code", read_only)
        full = tools_for_skills(["data_analysis", "code_interpreter", "report_generation"])
        self.assertIn("edit_file", full)
        self.assertIn("write_file", full)
        self.assertEqual(full.count("read_file"), 1)

    def test_framework_prefers_yaml_over_config_default(self):
        spec = {"framework": "native_react"}
        cfg = {"agent": {"framework": "native_react"}}
        previous = os.environ.pop("AGENT_FRAMEWORK", None)
        try:
            self.assertEqual(resolve_framework(spec, cfg), "native_react")
            os.environ["AGENT_FRAMEWORK"] = "crewai"
            self.assertEqual(resolve_framework(spec, cfg), "crewai")
        finally:
            if previous is None:
                os.environ.pop("AGENT_FRAMEWORK", None)
            else:
                os.environ["AGENT_FRAMEWORK"] = previous

    def test_debug_log_records_tool_call(self):
        registry = ToolRegistry()
        registry.register(Tool(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {}},
            handler=lambda kwargs: "ok",
        ))
        previous_debug = os.environ.get("AGENT_DEBUG")
        previous_path = os.environ.get("AGENT_LOG_PATH")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log_path = Path(tmp) / "agent.log"
                os.environ["AGENT_DEBUG"] = "1"
                os.environ["AGENT_LOG_PATH"] = str(log_path)
                self.assertEqual(registry.execute("echo", {}), "ok")
                text = log_path.read_text(encoding="utf-8")
                self.assertIn("tool=echo", text)
                self.assertIn("result=ok", text)
        finally:
            if previous_debug is None:
                os.environ.pop("AGENT_DEBUG", None)
            else:
                os.environ["AGENT_DEBUG"] = previous_debug
            if previous_path is None:
                os.environ.pop("AGENT_LOG_PATH", None)
            else:
                os.environ["AGENT_LOG_PATH"] = previous_path


if __name__ == "__main__":
    unittest.main()
