# -*- coding: utf-8 -*-
"""
HTTP 层端到端验证：请求真正穿过 **FastAPI → SSE → 状态机 → 工具 → 验收**。

与 `test_state_loop_e2e.py` 的区别：
- 那边直接驱动 `runtime.run_loop`，验证的是**状态机本身**；
- 这边从 `/api/chat/stream` 打进去，验证的是**整条链路接得上**：
  配置加载 → 运行时装配 → 框架选择 → SSE 事件形状 → 会话落盘 → 收尾文本。

模型侧用本地假服务（`tests/fake_llm_server.py`）代替 Ollama，
因此它在没有模型的机器上也能跑，且结论稳定。
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import DEFAULT_CONFIG
from tests.fake_llm_server import FakeLLMServer

INITIAL = 'GREETING = "old"\n'
CHECK_SCRIPT = (
    "import pathlib, sys\n"
    "text = pathlib.Path('app/greeting.py').read_text(encoding='utf-8')\n"
    "sys.exit(0 if 'GREETING = \"new\"' in text else 1)\n"
)

PLAN = {
    "summary": "修改问候常量",
    "tasks": [{
        "title": "把 app/greeting.py 里的 GREETING 改成 new",
        "detail": "把 app/greeting.py 里的 GREETING 常量改成 new。",
        "scope": ["app"],
        "commands": [["python", "check.py"]],
        "must_read": ["app/greeting.py"],
    }],
}


def _edit(old: str, new: str) -> dict:
    return {"kind": "tool_batch", "calls": [{"name": "edit_file", "arguments": {
        "path": "app/greeting.py", "old_string": old, "new_string": new}}]}


class HttpChainTests(unittest.TestCase):
    """同一个应用实例跑完整条链路，避免重复装配（RAG / MCP / 工具注册都不便宜）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "app").mkdir(parents=True, exist_ok=True)
        (cls.root / "app" / "greeting.py").write_text(INITIAL, encoding="utf-8")
        (cls.root / "check.py").write_text(CHECK_SCRIPT, encoding="utf-8")

        # 决策脚本：先写错（验收失败），再改对（验收通过）
        cls.llm = FakeLLMServer(plan=PLAN, actions=[
            _edit('GREETING = "old"', 'GREETING = "bad"'),
            _edit('GREETING = "bad"', 'GREETING = "new"'),
        ])
        base_url = cls.llm.start()

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.root)
        cfg["llm"].update({"base_url": base_url, "api_key": "test-key",
                           "model": "fake-model", "timeout": 30})
        cfg["llm"]["embedding"]["enabled"] = False
        cfg["rag"]["enabled"] = False                     # 不依赖嵌入服务
        cfg["storage"]["history_db"] = str(cls.root / "history.db")
        cfg["storage"]["sessions_dir"] = str(cls.root / "sessions")
        cfg["storage"]["preferences_path"] = str(cls.root / "preferences.json")
        cfg["executor"]["command_timeout_seconds"] = 30
        cls.cfg = cfg

        # 通过环境变量验证「框架可临时覆盖」这条路
        cls._previous_framework = os.environ.get("AGENT_FRAMEWORK")
        os.environ["AGENT_FRAMEWORK"] = "state_loop"

        import app.main as main_module

        cls._main = main_module
        cls._original_load_config = main_module.load_config
        main_module.load_config = lambda *args, **kwargs: cfg

        cls._client_cm = TestClient(main_module.app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._client_cm.__exit__(None, None, None)
        finally:
            cls._main.load_config = cls._original_load_config
            if cls._previous_framework is None:
                os.environ.pop("AGENT_FRAMEWORK", None)
            else:
                os.environ["AGENT_FRAMEWORK"] = cls._previous_framework
            cls.llm.stop()
            cls._tmp.cleanup()

    # ---------------- 用例 ----------------
    def test_01_health_reports_state_loop_runtime(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()

        self.assertEqual(body["framework"], "state_loop")
        self.assertEqual(body["exec_mode"], "auto_workspace")
        for tool in ("read_file", "edit_file", "write_file", "run_command",
                     "run_python_code", "file_search"):
            self.assertIn(tool, body["tools"], msg=f"缺少工具 {tool}")
        self.assertEqual(body["mcp"], [])
        self.assertNotIn("llm", body)
        self.assertEqual(body["model"], "fake-model")
        self.assertEqual(body["models"]["model"], "fake-model")

    def test_02_full_chain_over_sse(self):
        events = self._stream("把 app/greeting.py 里的 GREETING 改成 new")
        kinds = [event["type"] for event in events]

        for expected in ("session", "plan", "task", "tool_call", "tool_result",
                         "verify", "thought", "token", "done"):
            with self.subTest(event=expected):
                self.assertIn(expected, kinds, msg=f"缺少事件 {expected}")

        # 文件确实被改对了
        self.assertIn('GREETING = "new"',
                      (self.root / "app" / "greeting.py").read_text(encoding="utf-8"))

        # 验收先失败后成功——证明「验证没过就不算完成、修好才算」
        verifies = [event["data"] for event in events if event["type"] == "verify"]
        self.assertGreaterEqual(len(verifies), 2)
        self.assertFalse(verifies[0]["ok"])
        self.assertTrue(verifies[-1]["ok"])

        # 收尾文本来自模型（而不是机器兜底摘要）
        text = "".join(event["data"].get("content", "")
                       for event in events if event["type"] == "token")
        self.assertIn("已完成", text)

        done = [event["data"] for event in events if event["type"] == "done"][-1]
        self.assertEqual(done["finish_reason"], "completed")

    def test_03_tool_choice_is_forced(self):
        """
        `tool_choice` 必须是**强制**指定 `submit_decision`，而不是 `auto`。

        `auto` 意味着模型可以选择「不调用工具，直接说点什么」——
        那就等于给自由文本留了一个出口，协议约束形同虚设。
        """
        requests = self.llm.decision_requests()
        self.assertTrue(requests, "至少应当有拆解与决策两次带工具的请求")
        for request in requests:
            choice = request.get("tool_choice")
            self.assertIsInstance(choice, dict, msg=f"tool_choice 不是强制形式: {choice}")
            self.assertEqual(choice["function"]["name"], "submit_decision")
            self.assertEqual(request["tools"][0]["function"]["name"], "submit_decision")

    def test_04_sse_records_tool_arguments_and_effect(self):
        """初始请求已经跑过；这里复用其副作用，检查事件字段形状对前端可用。"""
        events = self._stream("把 app/greeting.py 里的 GREETING 改成 new")
        calls = [event["data"] for event in events if event["type"] == "tool_call"]
        self.assertTrue(calls)
        for call in calls:
            self.assertIn("id", call)
            self.assertIn("name", call)
            self.assertIn("arguments", call)
            self.assertIn("effect", call, msg="事件里应带上副作用等级，便于前端提示")

        results = [event["data"] for event in events if event["type"] == "tool_result"]
        self.assertTrue(results)
        for result in results:
            self.assertIn("is_error", result)
            self.assertIn("output", result)

    def test_05_unknown_session_is_rejected(self):
        response = self.client.post("/api/chat/stream",
                                    json={"session_id": "does-not-exist", "message": "hi"})
        self.assertEqual(response.status_code, 404)

    def test_06_empty_message_is_rejected(self):
        response = self.client.post("/api/chat/stream", json={"message": "   "})
        self.assertEqual(response.status_code, 400)

    def test_07_permission_hub_is_wired(self):
        """lifespan 必须把审批通道挂到 app.state（否则权限 API 返回 503）。"""
        response = self.client.get("/api/permissions/pending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"pending": []})

    def test_08_decide_unknown_request_is_404(self):
        response = self.client.post("/api/permissions/does-not-exist",
                                    json={"decision": "allow"})
        self.assertEqual(response.status_code, 404)

    def test_09_decide_invalid_decision_is_400(self):
        response = self.client.post("/api/permissions/whatever",
                                    json={"decision": "maybe"})
        self.assertEqual(response.status_code, 400)

    # ---------------- 辅助 ----------------
    def _stream(self, message: str):
        events = []
        with self.client.stream("POST", "/api/chat/stream",
                                json={"message": message}) as response:
            self.assertEqual(response.status_code, 200)
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                events.append(json.loads(line[5:].strip()))
        return events


class PermissionSseChainTests(unittest.TestCase):
    """
    SSE 确认闭环（切片 5 验收第二轮）：请求必须真实穿过
    HTTP → SSE permission_request → POST /api/permissions/{id} → 主循环放行 → 工具执行。

    与 HttpChainTests 不同：这里 local_access 启用且带可写根，流中收到
    确认请求后在同一会话内发起 POST（另一线程）决议 allow。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.workspace_root = cls.root / "ws"
        cls.ext_root = cls.root / "ext"
        cls.workspace_root.mkdir()
        cls.ext_root.mkdir()
        (cls.workspace_root / "check_ok.py").write_text(
            "import sys; sys.exit(0)\n", encoding="utf-8")

        cls.plan = {
            "summary": "在授权根内写一个外部文件",
            "tasks": [{
                "title": "向根 ext 写入 result.txt",
                "detail": "用 fs_write 在根 ext 创建 result.txt",
                "scope": ["ext:result.txt"],
                "commands": [["python", "check_ok.py"]],
                "must_read": [],
            }],
        }
        cls.llm = FakeLLMServer(plan=cls.plan, actions=[{
            "kind": "tool_batch",
            "calls": [{"name": "fs_write", "arguments": {
                "root": "ext", "path": "result.txt", "content": "approved"}}],
        }])
        base_url = cls.llm.start()

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["server"]["workspace_root"] = str(cls.workspace_root)
        cfg["llm"].update({"base_url": base_url, "api_key": "test-key",
                           "model": "fake-model", "timeout": 30})
        cfg["llm"]["embedding"]["enabled"] = False
        cfg["rag"]["enabled"] = False
        cfg["storage"]["history_db"] = str(cls.root / "history.db")
        cfg["storage"]["sessions_dir"] = str(cls.root / "sessions")
        cfg["storage"]["preferences_path"] = str(cls.root / "preferences.json")
        cfg["executor"]["command_timeout_seconds"] = 30
        cfg["local_access"]["enabled"] = True
        cfg["local_access"]["roots"] = [{"name": "ext", "path": str(cls.ext_root),
                                         "read": True, "write": True}]
        cfg["local_access"]["audit_log"] = ""        # 测试不写真实项目日志
        cfg["permissions"]["timeout_seconds"] = 60
        cfg["permissions"]["audit_log"] = str(cls.root / "perm.jsonl")
        cls.cfg = cfg

        cls._previous_framework = os.environ.get("AGENT_FRAMEWORK")
        os.environ["AGENT_FRAMEWORK"] = "state_loop"

        import app.main as main_module
        cls._main = main_module
        cls._original_load_config = main_module.load_config
        main_module.load_config = lambda *args, **kwargs: cfg
        cls._client_cm = TestClient(main_module.app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._client_cm.__exit__(None, None, None)
        finally:
            cls._main.load_config = cls._original_load_config
            if cls._previous_framework is None:
                os.environ.pop("AGENT_FRAMEWORK", None)
            else:
                os.environ["AGENT_FRAMEWORK"] = cls._previous_framework
            cls.llm.stop()
            cls._tmp.cleanup()

    def test_sse_permission_roundtrip(self):
        events = []
        # TestClient 的 stream+iter_lines 会占住 portal，期间再 POST
        # /api/permissions 会与读流死锁。决议改走进程内 Hub.decide。
        # 另外，部分环境下 SSE 帧会等到生成器推进后才到达本线程，
        # 所以不能「看到事件再决议」——要在 Hub 一登记就放行。
        hub = getattr(self.client.app.state.loop_deps.confirm_handler, "hub", None)
        if hub is None:
            hub = self.client.app.state.permissions

        stop = threading.Event()

        def approve_when_registered():
            deadline = time.time() + 15
            while not stop.is_set() and time.time() < deadline:
                view = hub.pending_view()
                if view:
                    hub.decide(view[0]["request_id"], True)
                    return
                time.sleep(0.05)

        approver = threading.Thread(target=approve_when_registered, daemon=True)
        approver.start()
        try:
            with self.client.stream("POST", "/api/chat/stream",
                                    json={"message": "写外部文件"}) as response:
                self.assertEqual(response.status_code, 200)
                for line in response.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    events.append(json.loads(line[5:].strip()))
        finally:
            stop.set()
            approver.join(timeout=1)

        kinds = [e["type"] for e in events]
        self.assertIn("permission_request", kinds)
        perm = [e["data"] for e in events if e["type"] == "permission_request"][0]
        self.assertEqual(perm["kind"], "tools")
        self.assertTrue(any(e["type"] == "tool_call" and
                            e["data"].get("name") == "fs_write" for e in events),
                        "fs_write 未执行。事件: " + ",".join(kinds))

        # 批准后文件必须落盘
        self.assertTrue((self.ext_root / "result.txt").exists(),
                        "允许后外部根文件必须被写入")
        self.assertIn("approved",
                      (self.ext_root / "result.txt").read_text(encoding="utf-8"))

        # 审计日志恰好一行 allow
        audit = Path(self.cfg["permissions"]["audit_log"])
        self.assertTrue(audit.exists(), "审批审计日志必须存在")
        rows = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "allow")
        self.assertEqual(rows[0]["request_id"], perm["request_id"])

        done = [e["data"] for e in events if e["type"] == "done"][-1]
        self.assertEqual(done["finish_reason"], "completed")


if __name__ == "__main__":
    unittest.main()
