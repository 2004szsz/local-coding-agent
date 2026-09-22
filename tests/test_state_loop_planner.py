# -*- coding: utf-8 -*-
"""需求拆解与任务图校验的单测。**校验规则全是确定性的，一条也不靠模型自查。**"""
import tempfile
import unittest
from pathlib import Path

from tools.fs_access import AccessBroker, AccessRoot
from tools.workspace import WorkspaceSecurity

from agents.state_loop import planner
from agents.state_loop.machine import DEFAULT_LIMITS
from agents.state_loop.state import TaskStatus


def task(title: str = "改 app/main.py", *, scope=("app",), commands=(("python", "-m", "pytest"),),
         depends=(), must_read=(), detail="") -> dict:
    return {
        "title": title,
        "detail": detail,
        "depends_on": list(depends),
        "scope": list(scope),
        "commands": [list(item) for item in commands],
        "must_read": list(must_read),
    }


class TrivialTests(unittest.TestCase):
    def test_chat_without_action_verb_is_trivial(self):
        self.assertTrue(planner.is_trivial("你好"))
        self.assertTrue(planner.is_trivial("这个项目是干什么的"))

    def test_action_verbs_are_not_trivial(self):
        for text in ("帮我改一下登录逻辑", "实现一个缓存层", "跑一下测试", "重构这个模块"):
            with self.subTest(text=text):
                self.assertFalse(planner.is_trivial(text))

    def test_active_graph_never_short_circuits(self):
        """会话里已有未完成任务图时，一句补充说明不该把计划丢掉。"""
        self.assertFalse(planner.is_trivial("你好", has_active_graph=True))

    def test_empty_message_is_trivial(self):
        self.assertTrue(planner.is_trivial("   "))


class AcceptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = WorkspaceSecurity(self.root)
        (self.root / "app").mkdir(parents=True, exist_ok=True)
        (self.root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def accept(self, tasks, summary=""):
        return planner.accept({"summary": summary, "tasks": tasks},
                              self.workspace, DEFAULT_LIMITS)

    # ---------------- 通过 ----------------
    def test_single_task_becomes_ready(self):
        graph, failure = self.accept([task()])
        self.assertIsNone(failure)
        self.assertEqual(graph.order, ["t1"])
        self.assertIs(graph.tasks["t1"].status, TaskStatus.ready)

    def test_dependencies_hold_tasks_pending(self):
        graph, failure = self.accept([task("第一"), task("第二", depends=(1,))])
        self.assertIsNone(failure)
        self.assertIs(graph.tasks["t1"].status, TaskStatus.ready)
        self.assertIs(graph.tasks["t2"].status, TaskStatus.pending)
        self.assertEqual(graph.tasks["t2"].depends_on, ("t1",))

    def test_dependency_by_title_is_accepted(self):
        graph, failure = self.accept([task("第一"), task("第二", depends=("第一",))])
        self.assertIsNone(failure)
        self.assertEqual(graph.tasks["t2"].depends_on, ("t1",))

    def test_promote_unlocks_dependents(self):
        """新并行度的唯一来源：任务标 done 之后 promote_ready 解锁后继。"""
        graph, _ = self.accept([task("第一"), task("第二", depends=(1,))])
        graph.tasks["t1"].status = TaskStatus.done
        self.assertEqual(graph.promote_ready(), ["t2"])
        self.assertIs(graph.tasks["t2"].status, TaskStatus.ready)

    def test_scope_is_normalized_to_relative(self):
        graph, failure = self.accept([task(scope=("./app/",))])
        self.assertIsNone(failure)
        self.assertEqual(graph.tasks["t1"].scope, ("app",))

    def test_topological_order_is_stable(self):
        first, _ = self.accept([task("甲"), task("乙"), task("丙", depends=(1,))])
        second, _ = self.accept([task("甲"), task("乙"), task("丙", depends=(1,))])
        self.assertEqual(first.order, second.order)

    # ---------------- 拒绝 ----------------
    def test_rejects_empty_plan(self):
        _, failure = self.accept([])
        self.assertIsNotNone(failure)

    def test_rejects_too_many_tasks_without_truncating(self):
        """超上限不截断执行：截断会丢依赖边，产生极难定位的失败。"""
        _, failure = self.accept([task(f"任务{i}") for i in range(DEFAULT_LIMITS.max_tasks + 1)])
        self.assertIsNotNone(failure)
        self.assertIn("超过上限", failure.message)

    def test_rejects_duplicate_titles(self):
        _, failure = self.accept([task("同名"), task("同名")])
        self.assertIsNotNone(failure)
        self.assertIn("重复", failure.message)

    def test_rejects_missing_scope(self):
        _, failure = self.accept([task(scope=())])
        self.assertIsNotNone(failure)
        self.assertIn("scope", failure.message)

    def test_rejects_scope_escape(self):
        _, failure = self.accept([task(scope=("../outside",))])
        self.assertIsNotNone(failure)
        self.assertIn("越界", failure.message)

    def test_rejects_missing_commands(self):
        """没有验收命令就无法被机器判定完成——这条不能放宽。"""
        _, failure = self.accept([task(commands=())])
        self.assertIsNotNone(failure)
        self.assertIn("验收命令", failure.message)

    def test_rejects_cycle(self):
        _, failure = self.accept([task("甲", depends=(2,)), task("乙", depends=(1,))])
        self.assertIsNotNone(failure)
        self.assertIn("环", failure.message)

    def test_rejects_self_dependency(self):
        _, failure = self.accept([task("甲", depends=(1,))])
        self.assertIsNotNone(failure)

    def test_rejects_unknown_dependency(self):
        _, failure = self.accept([task("甲", depends=("不存在的任务",))])
        self.assertIsNotNone(failure)
        self.assertIn("不存在", failure.message)

    def test_rejects_command_outside_allowlist(self):
        _, failure = self.accept([task(commands=(("rm", "-rf", "."),))])
        self.assertIsNotNone(failure)
        self.assertIn("允许列表", failure.message)

    def test_rejects_pip_install_as_acceptance(self):
        _, failure = self.accept([task(commands=(("pip", "install", "x"),))])
        self.assertIsNotNone(failure)

    def test_accepts_python_m_pytest(self):
        graph, failure = self.accept([task(commands=(("python", "-m", "pytest", "-q"),))])
        self.assertIsNone(failure)
        self.assertEqual(graph.tasks["t1"].acceptance.commands, (("python", "-m", "pytest", "-q"),))

    def test_rejects_must_read_escape(self):
        _, failure = self.accept([task(must_read=("../secret.txt",))])
        self.assertIsNotNone(failure)

    def test_failure_leaves_no_graph(self):
        graph, failure = self.accept([task(scope=())])
        self.assertIsNone(graph)
        self.assertIsNotNone(failure)

    def test_root_rel_scope_requires_broker(self):
        _, failure = self.accept([task(scope=("desktop:notes.md",))])
        self.assertIsNotNone(failure)
        self.assertIn("local_access", failure.message)

    def test_root_rel_scope_accepted_when_broker_grants(self):
        extra = self.root / "desktop"
        extra.mkdir()
        (extra / "notes.md").write_text("hi", encoding="utf-8")
        broker = AccessBroker(
            {"desktop": AccessRoot(name="desktop", path=extra, read=True, write=False)})
        graph, failure = planner.accept(
            {"summary": "", "tasks": [task(scope=("desktop:notes.md",),
                                           must_read=("desktop:notes.md",))]},
            self.workspace, DEFAULT_LIMITS, broker=broker)
        self.assertIsNone(failure, getattr(failure, "message", None))
        self.assertEqual(graph.tasks["t1"].scope, ("desktop:notes.md",))
        self.assertEqual(graph.tasks["t1"].acceptance.must_read, ("desktop:notes.md",))

    def test_unknown_root_rejected(self):
        extra = self.root / "desktop"
        extra.mkdir()
        broker = AccessBroker(
            {"desktop": AccessRoot(name="desktop", path=extra, read=True, write=False)})
        _, failure = planner.accept(
            {"summary": "", "tasks": [task(scope=("other:x.txt",))]},
            self.workspace, DEFAULT_LIMITS, broker=broker)
        self.assertIsNotNone(failure)
        self.assertIn("越界", failure.message)


if __name__ == "__main__":
    unittest.main()
