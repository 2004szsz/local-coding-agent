# -*- coding: utf-8 -*-
"""转移表与停止条件的单测。**不需要模型、不需要网络、不需要真实文件系统。**"""
import unittest

from agents.state_loop import machine
from agents.state_loop.machine import (
    DEFAULT_LIMITS,
    EV,
    Event,
    IllegalTransition,
    Rule,
    force_halt,
    interrupt_event,
    transition,
)
from agents.state_loop.state import (
    Acceptance,
    Effect,
    ExecMode,
    Failure,
    FailureKind,
    HaltReason,
    LoopState,
    PermissionKind,
    Phase,
    TaskGraph,
    TaskNode,
    TaskStatus,
    ToolCall,
)


def make_state(phase: Phase, **kwargs) -> LoopState:
    return LoopState(phase=phase, **kwargs)


def make_task(task_id: str = "t1", *, status=TaskStatus.ready, attempts: int = 0,
              max_attempts: int = 3, perceived: bool = False) -> TaskNode:
    return TaskNode(
        id=task_id, title=f"任务 {task_id}", scope=("app/main.py",),
        acceptance=Acceptance(commands=(("python", "-m", "pytest"),)),
        status=status, attempts=attempts, max_attempts=max_attempts, perceived=perceived,
    )


def with_task(phase: Phase, task: TaskNode | None = None, **kwargs) -> LoopState:
    task = task or make_task()
    graph = TaskGraph(tasks={task.id: task}, order=[task.id])
    return LoopState(phase=phase, graph=graph, current_task_id=task.id, **kwargs)


class TableIntegrityTests(unittest.TestCase):
    """转移表自身的结构不变量。这类测试最能防止「表越改越松」。"""

    def test_table_self_check_passes(self):
        self.assertEqual(machine.validate_table(), [])

    def test_every_group_has_an_unguarded_fallback(self):
        """
        每个 (状态, 事件) 分组的最后一条必须无守卫。

        否则守卫全不过时 `_select` 会抛 IllegalTransition——
        把一个「编排缺陷」伪装成运行时噪声，这是最难查的一类 bug。
        """
        for key, group in machine._TRANSITIONS.items():
            with self.subTest(key=key):
                self.assertIsNone(group[-1].guard)

    def test_select_always_resolves_for_every_group(self):
        """用最小状态喂给每一组事件，都必须能选出规则（不会因守卫全灭而抛）。"""
        for (phase, event_name) in machine._TRANSITIONS:
            with self.subTest(phase=phase, event=event_name):
                rule = machine._select(make_state(phase), Event(event_name), DEFAULT_LIMITS)
                self.assertIsInstance(rule, Rule)

    def test_halt_is_never_a_literal_target(self):
        """停机必须用 to=None + halt=原因，不允许显式指向 Phase.halt（会漏设原因）。"""
        for key, group in machine._TRANSITIONS.items():
            for rule in group:
                self.assertIsNot(rule.to, Phase.halt, msg=f"{key} 显式指向 halt")


class IntakeTests(unittest.TestCase):
    def test_normal_message_goes_to_decompose(self):
        state = transition(make_state(Phase.intake),
                           Event(EV.TURN_ACCEPTED, {"trivial": False}))
        self.assertIs(state.phase, Phase.decompose)

    def test_trivial_message_short_circuits(self):
        """闲聊/单步问答不建任务图，直接收尾。"""
        state = transition(make_state(Phase.intake),
                           Event(EV.TURN_ACCEPTED, {"trivial": True}))
        self.assertIs(state.phase, Phase.halt)
        self.assertIs(state.halt_reason, HaltReason.completed)

    def test_rejected_turn_halts_blocked(self):
        state = transition(make_state(Phase.intake), Event(EV.TURN_REJECTED))
        self.assertIs(state.halt_reason, HaltReason.blocked)

    def test_transition_does_not_mutate_input(self):
        """transition 必须返回新对象：状态推进只有一条路径，回放才成立。"""
        original = make_state(Phase.intake)
        transition(original, Event(EV.TURN_ACCEPTED, {"trivial": False}))
        self.assertIs(original.phase, Phase.intake)
        self.assertEqual(original.step_count, 0)


class TerminalTests(unittest.TestCase):
    def test_terminal_rejects_any_event(self):
        halted = make_state(Phase.halt, halt_reason=HaltReason.completed)
        with self.assertRaises(IllegalTransition):
            transition(halted, Event(EV.ALL_DONE))

    def test_unknown_event_raises(self):
        with self.assertRaises(IllegalTransition):
            transition(make_state(Phase.decide), Event(EV.ALL_DONE))

    def test_force_halt_is_idempotent(self):
        halted = force_halt(make_state(Phase.halt, halt_reason=HaltReason.stalled),
                            HaltReason.budget)
        self.assertIs(halted.halt_reason, HaltReason.stalled)

    def test_force_halt_clears_transient_fields(self):
        state = with_task(Phase.act, pending_calls=(ToolCall("c1", "read_file"),),
                          permission_request_id="perm_x",
                          permission_kind=PermissionKind.tools)
        halted = force_halt(state, HaltReason.blocked)
        self.assertIs(halted.phase, Phase.halt)
        self.assertEqual(halted.pending_calls, ())
        self.assertIsNone(halted.permission_request_id)
        self.assertIsNone(halted.permission_kind)


class ProtocolRetryTests(unittest.TestCase):
    def test_plan_invalid_retries_once_then_halts(self):
        state = make_state(Phase.decompose)
        state = transition(state, Event(EV.PLAN_INVALID))
        self.assertIs(state.phase, Phase.decompose)
        self.assertEqual(state.protocol_retries, 1)

        state = transition(state, Event(EV.PLAN_INVALID))
        self.assertIs(state.phase, Phase.halt)
        self.assertIs(state.halt_reason, HaltReason.blocked)

    def test_plan_needs_confirm_goes_to_authorize(self):
        graph = TaskGraph(tasks={"t1": make_task()}, order=["t1"])
        state = transition(make_state(Phase.decompose),
                           Event(EV.PLAN_NEEDS_CONFIRM, {"graph": graph,
                                                         "request_kind": "plan",
                                                         "request_id": "perm_1"}))
        self.assertIs(state.phase, Phase.authorize)
        self.assertIs(state.permission_kind, PermissionKind.plan)
        self.assertEqual(state.permission_request_id, "perm_1")
        self.assertIs(state.graph, graph)

    def test_decision_invalid_retries_once_then_halts(self):
        state = with_task(Phase.decide)
        state = transition(state, Event(EV.DECISION_INVALID))
        self.assertIs(state.phase, Phase.decide)
        state = transition(state, Event(EV.DECISION_INVALID))
        self.assertIs(state.halt_reason, HaltReason.blocked)


class ScheduleTests(unittest.TestCase):
    def test_task_id_recorded_from_payload(self):
        state = with_task(Phase.schedule)
        state = transition(state, Event(EV.NEED_DECIDE, {"task_id": "t1"}))
        self.assertEqual(state.current_task_id, "t1")
        self.assertIs(state.phase, Phase.decide)

    def test_all_done_completes(self):
        state = with_task(Phase.schedule, make_task(status=TaskStatus.done))
        state = transition(state, Event(EV.ALL_DONE))
        self.assertIs(state.halt_reason, HaltReason.completed)

    def test_graph_blocked(self):
        state = with_task(Phase.schedule, make_task(status=TaskStatus.blocked))
        state = transition(state, Event(EV.GRAPH_BLOCKED))
        self.assertIs(state.halt_reason, HaltReason.blocked)


class DecideTests(unittest.TestCase):
    def test_mark_done_is_forced_into_verify(self):
        state = with_task(Phase.decide)
        state = transition(state, Event(EV.DECISION_IS_VERIFY))
        self.assertIs(state.phase, Phase.verify)

    def test_replan_consumes_quota(self):
        state = with_task(Phase.decide)
        state = transition(state, Event(EV.DECISION_REPLAN, {"reason": "拆分不对"}))
        self.assertIs(state.phase, Phase.decompose)
        self.assertEqual(state.replan_count, 1)
        self.assertIsNone(state.current_task_id)

    def test_replan_quota_exhausted_halts(self):
        state = with_task(Phase.decide, replan_count=DEFAULT_LIMITS.max_replan)
        state = transition(state, Event(EV.DECISION_REPLAN, {"reason": "再来"}))
        self.assertIs(state.halt_reason, HaltReason.blocked)

    def test_ask_user_halts_blocked(self):
        state = transition(with_task(Phase.decide),
                           Event(EV.DECISION_ASK, {"question": "用哪个库？"}))
        self.assertIs(state.halt_reason, HaltReason.blocked)


class AuthorizeTests(unittest.TestCase):
    def test_granted_with_write_resets_read_streak(self):
        state = with_task(Phase.authorize, read_streak=2,
                          pending_calls=(ToolCall("c1", "edit_file", {"path": "app/main.py"},
                                                  effect=Effect.L1_WRITE),))
        state = transition(state, Event(EV.GRANTED, {"pending_calls": state.pending_calls}))
        self.assertIs(state.phase, Phase.act)
        self.assertEqual(state.read_streak, 0)

    def test_granted_all_read_counts_streak(self):
        state = with_task(Phase.authorize, read_streak=1,
                          pending_calls=(ToolCall("c1", "read_file", {"path": "a.py"},
                                                  effect=Effect.L0_READ),))
        state = transition(state, Event(EV.GRANTED, {"pending_calls": state.pending_calls}))
        self.assertEqual(state.read_streak, 2)

    def test_needs_confirm_stays_in_authorize(self):
        state = with_task(Phase.authorize)
        state = transition(state, Event(EV.NEEDS_CONFIRM, {
            "request_kind": "tools", "request_id": "perm_9"}))
        self.assertIs(state.phase, Phase.authorize)
        self.assertEqual(state.permission_request_id, "perm_9")

    def test_confirm_granted_routes_by_request_kind(self):
        plan_state = with_task(Phase.authorize, permission_kind=PermissionKind.plan,
                               permission_request_id="perm_1")
        self.assertIs(transition(plan_state, Event(EV.CONFIRM_GRANTED)).phase, Phase.schedule)

        tool_state = with_task(Phase.authorize, permission_kind=PermissionKind.tools,
                               permission_request_id="perm_2",
                               pending_calls=(ToolCall("c1", "edit_file",
                                                       {"path": "app/main.py"},
                                                       effect=Effect.L1_WRITE),))
        result = transition(tool_state, Event(EV.CONFIRM_GRANTED,
                                              {"pending_calls": tool_state.pending_calls}))
        self.assertIs(result.phase, Phase.act)

    def test_leaving_authorize_clears_permission_fields(self):
        state = with_task(Phase.authorize, permission_kind=PermissionKind.tools,
                          permission_request_id="perm_3")
        state = transition(state, Event(EV.DENIED))
        self.assertIs(state.phase, Phase.repair)
        self.assertIsNone(state.permission_request_id)
        self.assertIsNone(state.permission_kind)


class ActTests(unittest.TestCase):
    def test_leaving_act_clears_pending_calls(self):
        state = with_task(Phase.act, pending_calls=(ToolCall("c1", "edit_file"),))
        state = transition(state, Event(EV.ACT_FAILED, {"outcomes": ()}))
        self.assertEqual(state.pending_calls, ())

    def test_read_only_streak_below_cap_continues(self):
        state = with_task(Phase.act, read_streak=DEFAULT_LIMITS.read_streak_cap - 1)
        self.assertIs(transition(state, Event(EV.ACT_OK_READONLY)).phase, Phase.decide)

    def test_read_only_streak_at_cap_goes_to_repair(self):
        """连续只读不推进 → 当作空转打断，而不是陪着它一直读下去。"""
        state = with_task(Phase.act, read_streak=DEFAULT_LIMITS.read_streak_cap)
        self.assertIs(transition(state, Event(EV.ACT_OK_READONLY)).phase, Phase.repair)

    def test_read_only_streak_at_cap_without_budget_stalls(self):
        state = with_task(Phase.act, make_task(attempts=3, max_attempts=3),
                          read_streak=DEFAULT_LIMITS.read_streak_cap)
        state = transition(state, Event(EV.ACT_OK_READONLY))
        self.assertIs(state.halt_reason, HaltReason.stalled)

    def test_outcomes_are_appended_by_transition(self):
        from agents.state_loop.state import ToolOutcome

        outcome = ToolOutcome(call_id="c1", name="read_file", ok=True, text="内容")
        state = with_task(Phase.act)
        state = transition(state, Event(EV.ACT_OK, {"outcomes": [outcome]}))
        self.assertEqual(len(state.observations), 1)
        state = transition(state, Event(EV.VERIFY_PASSED, {"task_id": "t1"}))
        self.assertEqual(len(state.observations), 1, "验证阶段不该清空观察环")


class VerifyTests(unittest.TestCase):
    def test_failure_with_budget_goes_to_repair(self):
        state = with_task(Phase.verify, make_task(attempts=1, max_attempts=3))
        self.assertIs(transition(state, Event(EV.VERIFY_FAILED)).phase, Phase.repair)

    def test_budget_exhausted_but_replan_left(self):
        state = with_task(Phase.verify, make_task(attempts=3, max_attempts=3), replan_count=0)
        self.assertIs(transition(state, Event(EV.VERIFY_FAILED)).phase, Phase.decompose)

    def test_both_exhausted_halts(self):
        state = with_task(Phase.verify, make_task(attempts=3, max_attempts=3),
                          replan_count=DEFAULT_LIMITS.max_replan)
        state = transition(state, Event(EV.VERIFY_FAILED))
        self.assertIs(state.halt_reason, HaltReason.blocked)

    def test_pass_clears_task_focus(self):
        state = with_task(Phase.verify)
        state = transition(state, Event(EV.VERIFY_PASSED, {"task_id": "t1"}))
        self.assertIs(state.phase, Phase.schedule)
        self.assertIsNone(state.current_task_id)


class RepairTests(unittest.TestCase):
    def test_repair_row_clears_observations_and_keeps_failure(self):
        from agents.state_loop.state import ToolOutcome

        old = ToolOutcome(call_id="c0", name="read_file", ok=True, text="旧内容")
        failure = ToolOutcome(call_id="failure", name="[失败]", ok=False, text="冲突")
        state = with_task(Phase.repair, observations=(old,))
        state = transition(state, Event(EV.REPAIR_ROLLBACK, {
            "outcomes": [failure], "reset_observations": True}))
        self.assertEqual([item.call_id for item in state.observations], ["failure"])

    def test_stalled_halts(self):
        state = transition(with_task(Phase.repair), Event(EV.STALLED, {"count": 2}))
        self.assertIs(state.halt_reason, HaltReason.stalled)

    def test_blocked_task_returns_to_schedule(self):
        """任务阻塞不等于回合结束：继续调度兄弟任务，部分成功好过全盘失败。"""
        state = with_task(Phase.repair)
        self.assertIs(transition(state, Event(EV.REPAIR_BLOCKED)).phase, Phase.schedule)


class CompressTests(unittest.TestCase):
    def test_budget_soft_enters_compress_and_remembers_resume_point(self):
        state = with_task(Phase.decide)
        state = transition(state, Event(EV.BUDGET_SOFT))
        self.assertIs(state.phase, Phase.compress)
        self.assertIs(state.resume_state, Phase.decide)

    def test_compressed_returns_to_resume_point(self):
        state = with_task(Phase.decide)
        state = transition(state, Event(EV.BUDGET_SOFT))
        state = transition(state, Event(EV.COMPRESSED, {"chars_used": 100}))
        self.assertIs(state.phase, Phase.decide)
        self.assertIsNone(state.resume_state)
        self.assertEqual(state.chars_used, 100)

    def test_compress_does_not_consume_attempts(self):
        task = make_task(attempts=1)
        state = with_task(Phase.decide, task)
        state = transition(state, Event(EV.BUDGET_SOFT))
        state = transition(state, Event(EV.COMPRESSED))
        self.assertEqual(state.tasks["t1"].attempts, 1)

    def test_hard_budget_halts(self):
        state = with_task(Phase.compress)
        state = transition(state, Event(EV.BUDGET_HARD))
        self.assertIs(state.halt_reason, HaltReason.budget)

    def test_cannot_enter_compress_twice(self):
        with self.assertRaises(IllegalTransition):
            transition(with_task(Phase.compress), Event(EV.BUDGET_SOFT))


class DelegationTests(unittest.TestCase):
    def test_children_joined_returns_to_schedule(self):
        state = with_task(Phase.delegate)
        self.assertIs(transition(state, Event(EV.CHILDREN_JOINED)).phase, Phase.schedule)

    def test_child_crash_still_returns_to_schedule(self):
        state = with_task(Phase.delegate)
        self.assertIs(transition(state, Event(EV.CHILD_CRASHED)).phase, Phase.schedule)


class InterruptTests(unittest.TestCase):
    def test_cancel_wins_over_step_cap(self):
        state = with_task(Phase.decide, step_count=999, model_calls=999)
        event = interrupt_event(state, DEFAULT_LIMITS, cancel_requested=True,
                               over_soft_budget=True)
        self.assertEqual(event.name, EV.CANCEL)
        self.assertIs(transition(state, event).halt_reason, HaltReason.cancelled)

    def test_step_cap_wins_over_budget(self):
        state = with_task(Phase.decide, step_count=DEFAULT_LIMITS.max_steps)
        event = interrupt_event(state, DEFAULT_LIMITS, over_soft_budget=True)
        self.assertEqual(event.name, EV.STEP_CAP)
        self.assertIs(transition(state, event).halt_reason, HaltReason.budget)

    def test_model_call_cap_triggers_step_cap(self):
        state = with_task(Phase.decide, model_calls=DEFAULT_LIMITS.max_model_calls)
        self.assertEqual(interrupt_event(state, DEFAULT_LIMITS).name, EV.STEP_CAP)

    def test_budget_soft_only_when_not_already_compressing(self):
        state = with_task(Phase.compress)
        self.assertIsNone(interrupt_event(state, DEFAULT_LIMITS, over_soft_budget=True))

    def test_no_interrupt_on_terminal(self):
        halted = make_state(Phase.halt, halt_reason=HaltReason.completed)
        self.assertIsNone(interrupt_event(halted, DEFAULT_LIMITS, cancel_requested=True))


class LimitParsingTests(unittest.TestCase):
    def test_build_limits_reads_spec_then_config(self):
        from agents.state_loop.runtime import build_limits

        limits = build_limits(
            {"max_iterations": 6, "max_replan": 1, "context_char_budget": 9000},
            {"executor": {"command_timeout_seconds": 30}, "agent": {"max_iterations": 99}},
        )
        self.assertEqual(limits.max_model_calls, 6)
        self.assertEqual(limits.max_steps, 24)
        self.assertEqual(limits.max_replan, 1)
        self.assertEqual(limits.context_char_budget, 9000)
        self.assertEqual(limits.max_command_seconds, 30)

    def test_build_limits_survives_garbage_values(self):
        """配置写错一个数字不该让服务起不来。"""
        from agents.state_loop.runtime import build_limits

        limits = build_limits({"max_replan": "很多", "context_char_budget": None}, {})
        self.assertEqual(limits.max_replan, 2)
        self.assertEqual(limits.context_char_budget, 24000)


if __name__ == "__main__":
    unittest.main()
