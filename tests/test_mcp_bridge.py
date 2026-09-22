# -*- coding: utf-8 -*-
"""
MCP 扩展的验证：配置降级、协议客户端、工具桥接、连接管理与权限接入。

其中 `McpClientTests` / `McpBridgeTests` / `McpManagerTests` 会**真的拉起一个子进程**
（`tests/fake_mcp_server.py`），因此它们证明的是「这条路能跑通」，
而不是「这段代码看起来对」。
"""
import sys
import unittest
from pathlib import Path

from tools.base import ToolRegistry
from tools.errors import McpToolError
from tools.mcp import McpClient, McpManager, load_servers
from tools.mcp.bridge import is_read_only, register_mcp_tools, tool_name
from tools.mcp.config import McpServerConfig, parse_server
from tools.workspace import WorkspaceSecurity

FAKE_SERVER = Path(__file__).resolve().parent / "fake_mcp_server.py"


def fake_config(name: str = "fake", **overrides) -> McpServerConfig:
    base = {
        "name": name,
        "transport": "stdio",
        "command": (sys.executable, "-u", str(FAKE_SERVER)),
        "timeout_seconds": 20,
    }
    base.update(overrides)
    return McpServerConfig(**base)


# ======================================================================
# 配置解析（逐条降级）
# ======================================================================
class ConfigTests(unittest.TestCase):
    def test_valid_stdio_config(self):
        config, reason = parse_server(
            {"name": "docs", "transport": "stdio", "command": ["npx", "-y", "x"]})
        self.assertEqual(reason, "")
        self.assertEqual(config.name, "docs")
        self.assertEqual(config.command, ("npx", "-y", "x"))
        self.assertFalse(config.read_only)

    def test_rejects_bad_name(self):
        for name in ("Docs", "1docs", "doc-s", "", "a" * 40):
            with self.subTest(name=name):
                config, reason = parse_server({"name": name, "command": ["x"]})
                self.assertIsNone(config)
                self.assertIn("name 非法", reason)

    def test_rejects_unimplemented_transport(self):
        config, reason = parse_server({"name": "x", "transport": "sse", "url": "http://x"})
        self.assertIsNone(config)
        self.assertIn("尚未实现", reason)

    def test_rejects_stdio_without_command(self):
        config, reason = parse_server({"name": "x", "transport": "stdio"})
        self.assertIsNone(config)
        self.assertIn("command", reason)

    def test_rejects_http_without_url(self):
        config, reason = parse_server({"name": "x", "transport": "streamable_http"})
        self.assertIsNone(config)
        self.assertIn("url", reason)

    def test_disabled_entry_is_skipped(self):
        config, reason = parse_server({"name": "x", "command": ["a"], "enabled": False})
        self.assertIsNone(config)
        self.assertIn("禁用", reason)

    def test_timeout_is_clamped(self):
        config, _ = parse_server({"name": "x", "command": ["a"], "timeout_seconds": 9999})
        self.assertEqual(config.timeout_seconds, 120)

    def test_load_servers_degrades_entry_by_entry(self):
        """
        一条坏配置只跳过一条，不影响其他 server，也不阻止对话。

        与 RAG 初始化失败策略一致：可选增强不得成为主链路的单点故障。
        """
        servers, warnings = load_servers({"mcp": {"servers": [
            {"name": "Bad-Name", "command": ["x"]},
            {"name": "sse_one", "transport": "sse", "url": "http://x"},
            {"name": "good_one", "command": ["echo"]},
            {"name": "good_one", "command": ["echo"]},
        ]}})
        self.assertEqual([s.name for s in servers], ["good_one"])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any("重复" in w for w in warnings))

    def test_missing_section_is_not_an_error(self):
        self.assertEqual(load_servers({}), ([], []))


# ======================================================================
# 工具命名与只读判定
# ======================================================================
class BridgeNamingTests(unittest.TestCase):
    def test_namespaced_and_sanitized(self):
        self.assertEqual(tool_name("docs", "look-up"), "mcp_docs_look_up")
        self.assertEqual(tool_name("docs", "a.b.c"), "mcp_docs_a_b_c")

    def test_long_names_stay_unique(self):
        long_a = "x" * 80 + "a"
        long_b = "x" * 80 + "b"
        self.assertNotEqual(tool_name("s", long_a), tool_name("s", long_b))
        self.assertLessEqual(len(tool_name("s", long_a)), 60)

    def test_read_only_from_server_flag(self):
        self.assertTrue(is_read_only({}, True))

    def test_read_only_from_annotations(self):
        self.assertTrue(is_read_only({"annotations": {"readOnlyHint": True}}, False))

    def test_description_text_never_implies_read_only(self):
        """绝不按描述文字猜只读——猜错的代价是外部状态被改。"""
        self.assertFalse(is_read_only(
            {"description": "只读查询，不会修改任何东西"}, False))
        self.assertFalse(is_read_only({"annotations": {"readOnlyHint": False}}, False))


# ======================================================================
# 真实子进程：协议客户端
# ======================================================================
class McpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient(fake_config())
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_handshake_populates_server_info(self):
        self.assertTrue(self.client.ready)
        self.assertEqual(self.client.server_info.get("name"), "fake-mcp")
        self.assertEqual(self.client.protocol_version, "2024-11-05")

    def test_list_tools(self):
        names = [tool["name"] for tool in self.client.list_tools()]
        self.assertIn("echo", names)
        self.assertIn("make-note", names)

    def test_call_tool_returns_text(self):
        result = self.client.call_tool("echo", {"text": "你好"})
        self.assertIn("echo: 你好", result["content"][0]["text"])

    def test_call_tool_returns_structured_content(self):
        result = self.client.call_tool("make-note", {"text": "x"})
        self.assertIn("structuredContent", result)

    def test_is_error_raises_mcp_error(self):
        with self.assertRaises(McpToolError):
            self.client.call_tool("make-note", {"fail": True})

    def test_unknown_tool_raises_mcp_error(self):
        with self.assertRaises(McpToolError):
            self.client.call_tool("does-not-exist", {})

    def test_notification_is_not_mistaken_for_response(self):
        """
        假 server 会在响应前插入一条通知。

        如果客户端把通知当成响应，这里的 `echo` 结果就会错位——这条断言就是在守这一点。
        """
        result = self.client.call_tool("echo", {"text": "顺序检查"})
        self.assertIn("顺序检查", result["content"][0]["text"])

    def test_poll_notifications_consumes_queue(self):
        notifications = self.client.poll_notifications()
        methods = [item.get("method") for item in notifications]
        self.assertIn("notifications/tools/list_changed", methods)
        self.assertEqual(self.client.poll_notifications(), [], "取走后应当清空")

    def test_tools_list_changed_after_notification(self):
        names = [tool["name"] for tool in self.client.list_tools()]
        self.assertIn("echo.extra", names, "收到 list_changed 后重新拉取应看到新工具")


class ClientFailureTests(unittest.TestCase):
    def test_command_not_found_raises(self):
        client = McpClient(McpServerConfig(name="nope", transport="stdio",
                                          command=("definitely-not-a-real-binary-xyz",)))
        with self.assertRaises(McpToolError):
            client.start()
        client.close()

    def test_call_before_start_raises_cleanly(self):
        client = McpClient(fake_config("unstarted"))
        with self.assertRaises(McpToolError):
            client.list_tools()


# ======================================================================
# 工具桥接
# ======================================================================
class McpBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = McpClient(fake_config())
        cls.client.start()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        self.registry = ToolRegistry()

    def test_registers_namespaced_tools(self):
        names = register_mcp_tools(self.registry, self.client)
        self.assertIn("mcp_fake_echo", names)
        self.assertIn("mcp_fake_make_note", names)
        self.assertTrue(self.registry.has("mcp_fake_echo"))

    def test_read_only_flag_survives_bridging(self):
        register_mcp_tools(self.registry, self.client)
        self.assertTrue(self.registry.get("mcp_fake_echo").read_only)
        self.assertFalse(self.registry.get("mcp_fake_make_note").read_only)

    def test_called_through_the_standard_registry(self):
        """MCP 工具与内置工具走同一个执行入口，这是「原生」的实质含义。"""
        register_mcp_tools(self.registry, self.client)
        result = self.registry.call("mcp_fake_echo", {"text": "经注册表"})
        self.assertTrue(result.ok, msg=result.text)
        self.assertIn("经注册表", result.text)

    def test_remote_error_is_classified_not_raised(self):
        register_mcp_tools(self.registry, self.client)
        result = self.registry.call("mcp_fake_make_note", {"fail": True})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_type, "McpToolError")

    def test_stale_tools_are_unregistered(self):
        """
        server 撤下工具后必须注销。

        注册表里留着一个调不通的幽灵工具，比没有这个工具更糟——
        模型会一直选它，然后一直失败。
        """
        self.registry.replace(_dummy_tool("mcp_fake_removed"))
        register_mcp_tools(self.registry, self.client, previously=["mcp_fake_removed"])
        self.assertFalse(self.registry.has("mcp_fake_removed"))


def _dummy_tool(name: str):
    from tools.base import Tool

    return Tool(name=name, description="d", parameters={"type": "object",
                                                       "properties": {}},
                handler=lambda kwargs: "ok")


# ======================================================================
# 连接管理
# ======================================================================
class McpManagerTests(unittest.TestCase):
    def test_dead_server_does_not_break_the_others(self):
        manager = McpManager([
            McpServerConfig(name="dead", transport="stdio",
                            command=("definitely-not-a-real-binary-xyz",)),
            fake_config("alive"),
        ], warn=lambda message: None)
        try:
            ready = manager.connect_all()
            self.assertEqual(ready, ["alive"])
            registry = ToolRegistry()
            self.assertIn("mcp_alive_echo", manager.register_all(registry))
            status = {item["name"]: item["status"] for item in manager.status()}
            self.assertEqual(status["dead"], "down")
            self.assertEqual(status["alive"], "ready")
        finally:
            manager.close()

    def test_refresh_only_after_list_changed(self):
        """
        没收到通知就不重拉工具表。

        无条件重拉会让每个用户回合都多出 N 次进程/网络往返，是很容易被忽略的开销。
        """
        manager = McpManager([fake_config("live")], warn=lambda message: None)
        try:
            manager.connect_all()
            registry = ToolRegistry()
            manager.register_all(registry)
            self.assertFalse(registry.has("mcp_live_echo_extra"))

            manager.refresh(registry)          # 还没有通知
            self.assertFalse(registry.has("mcp_live_echo_extra"))

            registry.call("mcp_live_echo", {"text": "触发通知"})
            manager.refresh(registry)
            self.assertTrue(registry.has("mcp_live_echo_extra"),
                            "收到 tools/list_changed 之后应当把新工具注册进来")
        finally:
            manager.close()

    def test_close_is_idempotent(self):
        manager = McpManager([fake_config("once")], warn=lambda message: None)
        manager.connect_all()
        manager.close()
        manager.close()
        self.assertEqual(manager.ready_names, [])


# ======================================================================
# 接入权限管线
# ======================================================================
class McpPermissionTests(unittest.TestCase):
    def setUp(self):
        self.client = McpClient(fake_config())
        self.client.start()
        self.registry = ToolRegistry()
        register_mcp_tools(self.registry, self.client)

    def tearDown(self):
        self.client.close()

    def test_read_only_tool_is_l0(self):
        from agents.state_loop.permissions import classify
        from agents.state_loop.state import Effect

        result = classify("mcp_fake_echo", {}, self.registry, None)
        self.assertEqual(result.effect, Effect.L0_READ)
        self.assertTrue(result.accepted)

    def test_mutating_tool_needs_confirmation(self):
        """MCP 工具与内置工具共用同一条权限管线：默认档下改外部状态要人点头。"""
        from agents.state_loop.permissions import authorize
        from agents.state_loop.state import Effect, ExecMode, ToolCall

        call = ToolCall("c1", "mcp_fake_make_note", {"text": "x"})
        result = authorize([call], ExecMode.auto_workspace, self.registry, None)
        self.assertEqual(result.calls[0].effect, Effect.L4_MCP_MUTATE)
        self.assertTrue(result.needs_confirm)
        self.assertFalse(result.granted)

    def test_workspace_is_not_required_for_mcp_tools(self):
        from agents.state_loop.permissions import classify
        from agents.state_loop.state import Effect

        result = classify("mcp_fake_echo", {}, self.registry, WorkspaceSecurity("."))
        self.assertEqual(result.effect, Effect.L0_READ)


if __name__ == "__main__":
    unittest.main()
