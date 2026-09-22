# -*- coding: utf-8 -*-
"""
系统数据只读层与受控系统动作的单测（切片 3 验收清单）：

- psutil 缺失 → 工具不注册、对话继续（注册函数返回空列表）
- expose 白名单决定注册哪些只读工具
- 环境变量 / 进程命令行脱敏生效
- allow_actions 白名单默认空 = 动作组全关
- permissions 分类：sys_* 只读 = L0、sys_notify = L2、动作 = L4
"""
import unittest
from unittest.mock import patch

from tools.base import ToolRegistry
from tools import system_tools
from tools.system_tools import (
    redact,
    redact_cmdline,
    register_system_action_tools,
    register_system_tools,
)


class RedactTests(unittest.TestCase):
    def test_redact_hits(self):
        self.assertEqual(redact("my-secret-value"), "***")
        self.assertEqual(redact("api_token_here"), "***")

    def test_redact_misses(self):
        self.assertEqual(redact("hello-world"), "hello-world")
        self.assertEqual(redact(""), "")

    def test_redact_cmdline_masks_flags_and_truncates(self):
        cmd = ["python", "--api-key", "SECRETVALUE", "run.py", "--token=XYZ"]
        text = redact_cmdline(cmd)
        self.assertNotIn("SECRETVALUE", text)
        self.assertNotIn("XYZ", text)
        self.assertIn("--api-key", text)
        self.assertIn("***", text)

    def test_redact_cmdline_truncates_to_200(self):
        text = redact_cmdline(["prog", "x" * 500])
        self.assertLessEqual(len(text), 200 + 1)


class SystemToolsRegistrationTests(unittest.TestCase):
    def test_expose_whitelist(self):
        if not system_tools.psutil_available:
            self.skipTest("本机未装 psutil")
        registry = ToolRegistry()
        names = register_system_tools(registry, {"expose": ["overview", "env"]})
        self.assertEqual(sorted(names), ["sys_env", "sys_overview"])

    def test_psutil_missing_degrades_silently(self):
        registry = ToolRegistry()
        with patch.object(system_tools, "psutil_available", False), \
                patch.object(system_tools, "psutil", None):
            names = register_system_tools(registry, {})
        self.assertEqual(names, [])
        self.assertEqual(registry.names(), [])

    def test_actions_default_closed(self):
        registry = ToolRegistry()
        names = register_system_action_tools(registry, {})
        self.assertEqual(names, [])
        self.assertEqual(registry.names(), [])

    def test_actions_follow_allowlist(self):
        registry = ToolRegistry()
        names = register_system_action_tools(registry, {"allow_actions": ["notify", "launch"],
                                                        "allow_apps": {"记事本": "notepad.exe"}})
        self.assertEqual(sorted(names), ["sys_launch", "sys_notify"])

    def test_screenshot_action_registered_only_when_allowed(self):
        registry = ToolRegistry()
        names = register_system_action_tools(registry, {"allow_actions": ["screenshot"]},
                                             workspace=object())
        self.assertEqual(names, ["sys_screenshot"])
        self.assertIn("sys_screenshot", registry.names())

    def test_screenshot_missing_workspace_raises_permission_error(self):
        registry = ToolRegistry()
        register_system_action_tools(registry, {"allow_actions": ["screenshot"]},
                                     workspace=None)
        result = registry.call("sys_screenshot", {"path": "shot.png"})
        self.assertFalse(result.ok)
        self.assertIn("PermissionError", result.error_type)


class SystemToolsBehaviorTests(unittest.TestCase):
    def setUp(self):
        if not system_tools.psutil_available:
            self.skipTest("本机未装 psutil")
        self.registry = ToolRegistry()
        register_system_tools(self.registry, {"expose": ["overview", "processes",
                                                         "disks", "network", "env",
                                                         "battery", "hardware"]})

    def test_sys_overview_returns_text(self):
        result = self.registry.call("sys_overview", {})
        self.assertTrue(result.ok, result.error_message)
        self.assertIn("系统", result.text)

    def test_sys_env_redacts_secrets(self):
        with patch.dict("os.environ", {"MY_API_KEY": "hunter2", "PATH": "C:/x",
                                       "DB_PASSWORD": "p@ss"}, clear=False):
            result = self.registry.call("sys_env", {})
        self.assertTrue(result.ok)
        self.assertIn("MY_API_KEY=***", result.text)
        self.assertIn("DB_PASSWORD=***", result.text)
        self.assertNotIn("hunter2", result.text)

    def test_sys_processes_runs(self):
        result = self.registry.call("sys_processes", {"top_n": 10})
        self.assertTrue(result.ok, result.error_message)
        self.assertIn("pid=", result.text)


class PermissionsClassificationTests(unittest.TestCase):
    def test_effects_registered(self):
        from agents.state_loop.permissions import TOOL_EFFECTS
        from agents.state_loop.state import Effect
        self.assertEqual(TOOL_EFFECTS["sys_overview"], Effect.L0_READ)
        self.assertEqual(TOOL_EFFECTS["sys_notify"], Effect.L2_SANDBOX)
        self.assertEqual(TOOL_EFFECTS["sys_launch"], Effect.L4_MCP_MUTATE)
        self.assertEqual(TOOL_EFFECTS["fs_read"], Effect.L0_READ)
        self.assertEqual(TOOL_EFFECTS["fs_write"], Effect.L4_MCP_MUTATE)
        self.assertEqual(TOOL_EFFECTS["fs_delete"], Effect.L4_MCP_MUTATE)


if __name__ == "__main__":
    unittest.main()