# -*- coding: utf-8 -*-
"""
端到端：用可注入的假决策器驱动**完整主循环**。

这一组测试要证明的是闭环本身：

    拆解 → 感知 → 决策 → 授权 → 执行 → 验收失败 → 修复（增量/回滚）→ 再验收 → 通过

不需要模型、不需要网络，工具与命令执行器都是真的，因此结论稳定、可进 CI。
假决策器只是把「模型说的话」换成了脚本，**流程控制权仍在状态机手里**——
这些用例之所以能通过，恰恰因为它们无法靠提示词绕过任何一道门。
"""
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from tools.base import ToolRegistry
from tools.calculator import register_calculator
from tools.file_tools import register_file_tools
from tools.search import register_search_tools
from tools.shell import WorkspaceCommandRunner, build_shell_tool
from tools.workspace import WorkspaceSecurity

from agents.state_loop import context
from agents.state_loop.machine import EV, LoopLimits
from agents.state_loop.runtime import (
    LoopDeps,
    build_initial_state,
    fallback_summary,
    run_loop,
)
from agents.state_loop.state import ExecMode, HaltReason, Phase, TaskStatus

#: 验收脚本：只有把常量改成 new 才算通过。它独立于被测代码，是「外部裁判」。
CHECK_SCRIPT = (
    "import pathlib, sys\n"
    "text = pathlib.Path('app/greeting.py').read_text(encoding='utf-8')\n"
    "sys.exit(0 if 'GREETING = \"new\"' in text else 1)\n"
)
INITIAL = 'GREETING = "old"\n'
TASK_SCOPE = ["app"]
TASK_COMMANDS = [["python", "check.py"]]
TASK_MUST_READ = ["app/greeting.py"]


def plan_payload(title: str = "把 app/greeting.py 里的 GREETING 改成 new") -> Dict[str, Any]:
    return {
        "summary": "修改问候常量",
        "tasks": [{
            "title": title,
            "detail": "把 app/greeting.py 里的 GREETING 常量改成 new，并保持文件可编译。",
            "scope": TASK_SCOPE,
            "commands": TASK_COMMANDS,
            "must_read": TASK_MUST_READ,
        }],
    }


def edit(old: str, new: str, path: str = "app/greeting.py") -> Dict[str, Any]:
    return {"kind": "tool_batch", "calls": [{"name": "edit_file", "arguments": {
        "path": path, "old_string": old, "new_string": new}}]}


def write(path: str, content: str = "x = 1\n") -> Dict[str, Any]:
    return {"kind": "tool_batch", "calls": [{"name": "write_file", "arguments": {
        "path": path, "content": content}}]}


def read(path: str = "app/greeting.py") -> Dict[str, Any]:
    return {"kind": "tool_batch", "calls": [{"name": "read_file", "arguments": {
        "path": path}}]}


class Harness:
    """最小可运行环境：真实工具 + 真实命令执行器 + 脚本化决策器。"""

    def __init__(self, root: Path, decisions: List[Dict[str, Any]]):
        self.root = root
        self.workspace = WorkspaceSecurity(root)
        self.runner = WorkspaceCommandRunner(self.workspace, default_seconds=20)

        self.registry = ToolRegistry()
        register_file_tools(self.registry, self.workspace)
        register_calculator(self.registry)
        register_search_tools(self.registry, self.workspace, None)
        self.registry.register(build_shell_tool(self.runner))

        self.events: List[tuple] = []
        self.decisions = list(decisions)
        self.decompose_calls = 0
        self.decide_calls = 0
        self.notes: Dict[str, Any] = {}
        self.deps = LoopDeps(
            registry=self.registry,
            workspace=self.workspace,
            runner=self.runner,
            config=LoopLimits(max_steps=60, max_model_calls=20, max_attempts=3, max_replan=2),
            mode=ExecMode.auto_workspace,
            system_prompt="测试用系统提示词",
            allowed_tools=tuple(self.registry.names()),
            decision_hook=self._hook,
        )

    # ---------------- 假决策器 ----------------
    async def _hook(self, state, purpose: str):
        if purpose == "decompose":
            self.decompose_calls += 1
            return plan_payload()
        self.decide_calls += 1
        if not self.decisions:
            return {"kind": "mark_done"}
        return self.decisions.pop(0)

    def emit(self, kind: str, data: Dict[str, Any]) -> None:
        self.events.append((kind, data))

    async def run(self, text: str = "把 GREETING 改成 new") -> Any:
        state = build_initial_state(text, self.deps.mode)
        return await run_loop(self.deps, state, self.emit)

    # ---------------- 断言辅助 ----------------
    def kinds(self) -> List[str]:
        return [kind for kind, _ in self.events]

    def payloads(self, kind: str) -> List[Dict[str, Any]]:
        return [data for name, data in self.events if name == kind]

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


class BaseCase(unittest.IsolatedAsyncioTestCase):
    decisions: List[Dict[str, Any]] = []

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "app").mkdir(parents=True, exist_ok=True)
        (self.root / "app" / "greeting.py").write_text(INITIAL, encoding="utf-8")
        (self.root / "check.py").write_text(CHECK_SCRIPT, encoding="utf-8")
        self.harness = Harness(self.root, list(self.decisions))

    def tearDown(self):
        self._tmp.cleanup()


class HappyPathTests(BaseCase):
    """目标：改对了就算完成，且全过程有据可查。"""

    decisions = [edit('GREETING = "old"', 'GREETING = "new"')]

    async def test_full_loop_completes_and_writes_file(self):
        final = await self.harness.run()

        self.assertIs(final.phase, Phase.halt)
        self.assertIs(final.halt_reason, HaltReason.completed)
        self.assertIs(final.graph.tasks["t1"].status, TaskStatus.done)
        self.assertIn('GREETING = "new"', self.harness.read("app/greeting.py"))

    async def test_events_cover_the_whole_chain(self):
        await self.harness.run()
        kinds = self.harness.kinds()

        for expected in ("plan", "task", "tool_call", "tool_result", "verify", "thought"):
            with self.subTest(event=expected):
                self.assertIn(expected, kinds)

        # 顺序：计划在前，工具调用在结果之前，验收在工具之后
        self.assertLess(kinds.index("plan"), kinds.index("tool_call"))
        self.assertLess(kinds.index("tool_call"), kinds.index("verify"))

    async def test_verify_reports_success_with_command(self):
        await self.harness.run()
        verifies = self.harness.payloads("verify")
        self.assertTrue(verifies)
        self.assertTrue(verifies[-1]["ok"])
        self.assertEqual(verifies[-1]["exit_code"], 0)
        self.assertIn("check.py", verifies[-1]["command"])

    async def test_progress_note_recorded(self):
        final = await self.harness.run()
        self.assertTrue(any("验收通过" in note for note in final.notes))


class IterativeRepairTests(BaseCase):
    """目标：第一次改错 → 验收失败 → 增量修复 → 通过。"""

    decisions = [
        edit('GREETING = "old"', 'GREETING = "bad"'),
        edit('GREETING = "bad"', 'GREETING = "new"'),
    ]

    async def test_failed_verify_then_incremental_fix(self):
        final = await self.harness.run()

        self.assertIs(final.halt_reason, HaltReason.completed)
        self.assertIn('GREETING = "new"', self.harness.read("app/greeting.py"))

        verifies = self.harness.payloads("verify")
        self.assertGreaterEqual(len(verifies), 2)
        self.assertFalse(verifies[0]["ok"], "第一次验收必须失败（值还是 bad）")
        self.assertTrue(verifies[-1]["ok"])

    async def test_attempt_budget_is_consumed_by_verify_only(self):
        final = await self.harness.run()
        self.assertEqual(final.graph.tasks["t1"].attempts, 1,
                         "一次验收失败应当消耗且只消耗一次尝试额度")

    async def test_failure_becomes_an_observation_for_next_decision(self):
        """
        失败必须以**结构化观察**回到模型上下文，而不是一段来源不明的报错文本。

        注意这个场景的失败来自 `verify`（不是某个工具调用），所以断言要看观察环本身，
        而不是去 events 里找 is_error 的 tool_result——那正是最初写错这条断言的原因。
        """
        final = await self.harness.run()

        failed = [item for item in final.observations if not item.ok]
        self.assertTrue(failed, "验收失败必须以观察的形式留在环里")
        rendered = context.render_observations(final, LoopLimits())
        self.assertIn("TestFailed", rendered, "失败类别必须出现在给模型的上下文里")


class RollbackTests(BaseCase):
    """目标：补丁定位失败 → 回滚到检查点 → 回到干净状态重做。"""

    decisions = [
        edit('GREETING = "does-not-exist"', 'GREETING = "new"'),   # 必然冲突
        edit('GREETING = "old"', 'GREETING = "new"'),              # 回滚后这次能定位
    ]

    async def test_patch_conflict_triggers_rollback(self):
        final = await self.harness.run()

        self.assertIn("rollback", self.harness.kinds(), "补丁冲突必须走回滚路径")
        payload = self.harness.payloads("rollback")[0]
        self.assertEqual(payload["task"], "t1")
        self.assertIn("app/greeting.py", payload["paths"])

    async def test_rollback_restores_content_then_succeeds(self):
        final = await self.harness.run()
        self.assertIs(final.halt_reason, HaltReason.completed)
        self.assertIn('GREETING = "new"', self.harness.read("app/greeting.py"))

    async def test_rollback_resets_perception(self):
        """回滚后磁盘内容已变，必须重新感知——否则模型会对着旧内容打补丁。"""
        await self.harness.run()
        reads = [data for kind, data in self.harness.events if kind == "tool_call" and
                 data.get("name") == "read_file"]
        self.assertGreaterEqual(len(reads), 2, "回滚后应当再读一次文件")


class ScopeGuardTests(BaseCase):
    """目标：写到范围外被当场拒绝，且文件根本不会被创建。"""

    decisions = [
        write("other/outside.py"),
        edit('GREETING = "old"', 'GREETING = "new"'),
    ]

    async def test_out_of_scope_write_is_rejected(self):
        final = await self.harness.run()
        self.assertFalse((self.root / "other" / "outside.py").exists(),
                         "越界写入必须没有落盘")
        self.assertIs(final.halt_reason, HaltReason.completed)

    async def test_denial_is_announced(self):
        await self.harness.run()
        thoughts = " ".join(data.get("content", "") for _, data in self.harness.events
                            if _ == "thought")
        self.assertIn("拒绝", thoughts)


class ReadStreakTests(BaseCase):
    """目标：只读空转被打断并停下来，而不是陪着它一直读下去。"""

    decisions = [read(), read(), read(), read(), read(), read()]

    async def test_read_only_spin_is_interrupted(self):
        final = await self.harness.run()
        self.assertIs(final.phase, Phase.halt)
        self.assertIsNot(final.halt_reason, HaltReason.completed)
        self.assertLess(final.model_calls, LoopLimits().max_model_calls,
                        "不应该靠模型调用上限才停下来")


class ProtocolViolationTests(BaseCase):
    """目标：模型不按协议输出时，重试一次然后明确停机——不从散文里抠 JSON。"""

    decisions = [{"nonsense": True}, {"also_nonsense": True}]

    async def test_protocol_violation_retries_then_halts(self):
        final = await self.harness.run()
        self.assertIs(final.halt_reason, HaltReason.blocked)
        self.assertEqual(self.harness.decide_calls, 2, "应当只重试一次")

    async def test_violation_is_reported_to_user(self):
        await self.harness.run()
        thoughts = " ".join(data.get("content", "") for name, data in self.harness.events
                            if name == "thought")
        self.assertIn("submit_decision", thoughts)


class TrivialTurnTests(BaseCase):
    """目标：闲聊不建任务图，直接收尾。"""

    decisions: List[Dict[str, Any]] = []

    async def test_chat_turn_short_circuits(self):
        final = await self.harness.run("你好呀")
        self.assertIs(final.halt_reason, HaltReason.completed)
        self.assertIsNone(final.graph)
        self.assertEqual(self.harness.decompose_calls, 0)
        self.assertNotIn("tool_call", self.harness.kinds())

    async def test_fallback_summary_mentions_no_plan(self):
        final = await self.harness.run("你好呀")
        self.assertIn("没有建立任务图", fallback_summary(final))


class FallbackSummaryTests(BaseCase):
    decisions = [edit('GREETING = "old"', 'GREETING = "new"')]

    async def test_summary_reports_progress_and_verification(self):
        final = await self.harness.run()
        text = fallback_summary(final)
        self.assertIn("1/1 完成", text)
        self.assertIn("验收", text)


class StateObservabilityTests(BaseCase):
    decisions = [edit('GREETING = "old"', 'GREETING = "new"')]

    async def test_state_is_serializable(self):
        """LoopState 必须可直接序列化，为第二期落盘/续跑留好接口。"""
        import json

        final = await self.harness.run()
        payload = json.dumps(final.to_jsonable(), ensure_ascii=False)
        self.assertIn("\"phase\": \"halt\"", payload)
        self.assertIn("t1", payload)

    async def test_step_and_model_counters_are_tracked(self):
        final = await self.harness.run()
        self.assertGreater(final.step_count, 0)
        self.assertEqual(final.model_calls, 2, "一次拆解 + 一次决策")


class FakeLLM:
    """
    最小可用的模型替身，但**走真实的协议路径**：

        context.build_messages  →  llm.chat(强制 tool_choice)  →  decisions.extract_tool_arguments

    与 `decision_hook` 的区别很关键：注入 hook 会跳过整条消息构建与协议解析链路。
    实际就这么漏过一个 bug —— `context.build_messages` 里写错了属性名，
    而离线测试因为走 hook、从不构建消息，所以全绿。
    """

    def __init__(self, plan: Dict[str, Any], actions: List[Dict[str, Any]]):
        self.plan = plan
        self.actions = list(actions)
        self.calls: List[Dict[str, Any]] = []

    def chat(self, messages, tools=None, tool_choice=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        if not tools:
            return {"role": "assistant", "content": "已按要求完成。"}

        properties = ((tools[0].get("function") or {}).get("parameters") or {}).get("properties") or {}
        if "tasks" in properties:
            arguments: Any = self.plan
        elif "kind" in properties:
            arguments = self.actions.pop(0) if self.actions else {"kind": "mark_done"}
        else:
            arguments = {}

        return {"role": "assistant", "tool_calls": [{
            "id": "call_1", "type": "function",
            # 直接给 dict：extract_tool_arguments 同时支持 dict 与 JSON 字符串
            "function": {"name": "submit_decision", "arguments": arguments},
        }]}


class RealProtocolPathTests(BaseCase):
    """不注入 decision_hook，强制走消息构建 + 协议解析的真路径。"""

    decisions: List[Dict[str, Any]] = []

    async def test_runs_without_decision_hook(self):
        llm = FakeLLM(plan_payload(), [_edit_action()])
        self.harness.deps.llm = llm
        self.harness.deps.decision_hook = None

        final = await self.harness.run()

        self.assertIs(final.halt_reason, HaltReason.completed)
        self.assertIn('GREETING = "new"', self.harness.read("app/greeting.py"))
        self.assertTrue(llm.calls, "模型必须真的被调用过")

    async def test_messages_carry_task_card_and_tool_catalog(self):
        """
        给模型的消息必须真的带上「当前任务卡 + 工具目录 + 执行档」。

        这三块是模型能做出正确决策的全部依据；少任何一块，
        它就只能靠猜——而这正是「提示词工程式 Agent」最容易出的问题。
        """
        llm = FakeLLM(plan_payload(), [_edit_action()])
        self.harness.deps.llm = llm
        self.harness.deps.decision_hook = None
        await self.harness.run()

        decide_calls = [call for call in llm.calls
                        if call["tools"] and "kind" in
                        call["tools"][0]["function"]["parameters"].get("properties", {})]
        self.assertTrue(decide_calls, "至少应有一次 decide 阶段的请求")
        user_content = decide_calls[0]["messages"][-1]["content"]

        self.assertIn("当前任务", user_content)
        self.assertIn("可用工具", user_content)
        self.assertIn("edit_file", user_content)
        self.assertIn("可写范围", user_content)
        self.assertIn("submit_decision", decide_calls[0]["messages"][0]["content"])

    async def test_forced_tool_choice_is_used(self):
        llm = FakeLLM(plan_payload(), [_edit_action()])
        self.harness.deps.llm = llm
        self.harness.deps.decision_hook = None
        await self.harness.run()

        for call in llm.calls:
            if not call["tools"]:
                continue
            choice = call["tool_choice"]
            self.assertIsInstance(choice, dict, msg="tool_choice 必须是强制形式")
            self.assertEqual(choice["function"]["name"], "submit_decision")

    async def test_protocol_violation_is_reported_not_swallowed(self):
        """模型只回散文（不调函数）时必须变成显式失败，而不是被当成答复。"""

        class ChattyLLM(FakeLLM):
            def chat(self, messages, tools=None, tool_choice=None):
                self.calls.append({"messages": messages, "tools": tools,
                                   "tool_choice": tool_choice})
                return {"role": "assistant", "content": "我觉得应该先改 app/greeting.py。"}

        llm = ChattyLLM({}, [])
        self.harness.deps.llm = llm
        self.harness.deps.decision_hook = None

        final = await self.harness.run()
        self.assertIs(final.halt_reason, HaltReason.blocked)
        self.assertNotIn('GREETING = "new"', self.harness.read("app/greeting.py"))


def _edit_action() -> Dict[str, Any]:
    return edit('GREETING = "old"', 'GREETING = "new"')


if __name__ == "__main__":
    unittest.main()
