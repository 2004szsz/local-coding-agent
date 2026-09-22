# -*- coding: utf-8 -*-
"""
自测验证：三重门。

`verify` 是**零模型调用**的状态。它只认三个事实来源：

    门 1  范围门 —— FileJournal 的写入记录（真的改了哪些文件）
    门 2  命令门 —— 子进程退出码（验收命令过不过）
    门 3  诊断门 —— ruff / pyright 的输出（有没有新增错误）

「模型宣布完成」不是事实来源之一，这也是本设计里「完成归验证」的落地方式：
**你说了不算，机器跑一遍才算。**

顺序不可调换：范围门在前，是为了避免「用越界改动产生的结果」去污染验证结论——
一个写到 scope 外的文件，无论测试通不通，这个任务的边界已经破了。

证据不足时不假装通过：诊断工具不在 PATH 上就记 `skipped`，
在验证报告里「跳过」与「通过」必须是两个不同的值。
"""
from __future__ import annotations

import shutil
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import repair, scheduler
from .machine import EV, Event, LoopLimits
from .permissions import path_in_scope
from .state import Failure, FailureKind, LoopState, TaskNode

__all__ = ["run", "DIAGNOSTIC_ARGS"]

#: 各诊断工具的参数构造。加新工具时在这里登记，不要在 run() 里写 if。
DIAGNOSTIC_ARGS: Dict[str, Any] = {
    "ruff": lambda files: ["ruff", "check", *files],
    "pyright": lambda files: ["pyright", *files],
}


async def run(state: LoopState, deps, emit=None) -> Event:
    """执行三重门。返回 `verify_passed` 或 `verify_failed`。"""
    task = state.current_task
    if task is None:
        return Event(EV.VERIFY_FAILED, {
            "reason": "没有当前任务，无法验证",
            "failure": _failure(FailureKind.ArgError, "没有当前任务").one_line(),
        })

    touched = _touched_paths(deps, task)

    # ---------------- 门 1：范围门 ----------------
    # 外部根写入以 root:rel 记账，工作区写入以相对路径记账；两者都要对照 scope。
    outside = [p for p in touched if not path_in_scope(p, task.scope)]
    if outside:
        repair.bump_scope_violation(state, task)
        failure = _failure(
            FailureKind.ScopeViolation,
            f"本任务写入了范围外文件: {', '.join(outside)}；"
            f"允许范围 {list(task.scope) or '（未声明）'}",
            tool="verify")
        return _fail(state, task, failure, emit,
                     detail=f"范围越界 {len(outside)} 个文件，未执行验收命令")

    # ---------------- 门 2：命令门 ----------------
    # 纯外部根任务（scope 全是 root:rel）的验收命令不在工作区跑——
    # 外部目录不参与验收语义。工作区任务即使没写盘，验收命令仍要执行。
    from .planner import parse_root_rel

    runner = getattr(deps, "runner", None)
    command_log: List[Dict[str, Any]] = []
    workspace_touched = [p for p in touched if parse_root_rel(p) is None]
    external_only = bool(task.scope) and all(
        parse_root_rel(str(item)) is not None for item in task.scope)

    if task.acceptance.commands and not external_only:
        if runner is None:
            failure = _failure(FailureKind.TestFailed, "命令执行器不可用，无法验收", tool="verify")
            return _fail(state, task, failure, emit, detail="命令执行器缺失")
        for argv in task.acceptance.commands:
            result = runner.run(list(argv))
            command_log.append({
                "argv": list(argv),
                "exit_code": result.artifacts.get("exit_code"),
                "ok": result.ok,
            })
            _emit(emit, {
                "task": task.id, "command": " ".join(argv),
                "ok": result.ok, "exit_code": result.artifacts.get("exit_code"),
            })
            if not result.ok:
                failure = _failure(
                    FailureKind.TestFailed,
                    f"验收命令失败（退出码 {result.artifacts.get('exit_code')}）: {' '.join(argv)}",
                    tool="verify",
                    artifacts={"tail": result.artifacts.get("tail", ""),
                               "exit_code": result.artifacts.get("exit_code")})
                return _fail(state, task, failure, emit,
                             detail=f"命令失败: {' '.join(argv)}", commands=command_log)
    elif task.acceptance.commands and external_only:
        command_log.append({
            "argv": list(task.acceptance.commands[0]) if task.acceptance.commands else [],
            "exit_code": None,
            "ok": True,
            "skipped": "external-root-only",
        })

    # ---------------- 门 3：诊断门 ----------------
    diagnostics, diag_failure = await _run_diagnostics(task, workspace_touched, runner)
    if diag_failure is not None:
        return _fail(state, task, diag_failure, emit,
                     detail=diag_failure.message, commands=command_log,
                     diagnostics=diagnostics)

    # ---------------- 通过 ----------------
    unlocked = scheduler.mark_task_done(state, task)
    deps.journal.forget(task.id) if getattr(deps, "journal", None) else None

    summary = f"{len(touched)} 个文件改动通过验收" if touched else "无需改动，验证通过"
    state.verify_log.append({
        "task": task.id, "ok": True, "detail": summary,
        "changed": list(touched), "commands": command_log, "diagnostics": diagnostics,
    })
    state.note(f"[{task.id}] 验收通过（{summary}）")
    return Event(EV.VERIFY_PASSED, {
        "task_id": task.id, "changed": list(touched), "summary": summary,
        "commands": command_log, "diagnostics": diagnostics, "unlocked": unlocked,
    })


# ======================================================================
# 内部
# ======================================================================
def _touched_paths(deps, task: TaskNode) -> List[str]:
    journal = getattr(deps, "journal", None)
    if journal is None:
        return []
    paths = list(journal.changed_paths(task.id))
    extra = getattr(journal, "external_changed_paths", None)
    if extra is not None:
        paths.extend(extra(task.id))
    return paths


async def _run_diagnostics(task: TaskNode, touched: Sequence[str], runner
                           ) -> Tuple[List[Dict[str, Any]], Optional[Failure]]:
    """
    对触碰过的文件跑静态检查。

    找不到工具就记 `skipped` 并**不算失败**——这是诚实的做法：
    把「没检查」说成「检查通过」会让整个 verify 状态变成一句口号。
    """
    records: List[Dict[str, Any]] = []
    if not touched or runner is None:
        return records, None

    for name in task.acceptance.diagnostics:
        builder = DIAGNOSTIC_ARGS.get(name)
        if builder is None:
            records.append({"tool": name, "status": "skipped", "reason": "未登记的参数构造"})
            continue
        if shutil.which(name) is None:
            records.append({"tool": name, "status": "skipped", "reason": "不在 PATH 上"})
            continue

        result = runner.run(builder(list(touched)))
        record = {
            "tool": name,
            "status": "ok" if result.ok else "failed",
            "exit_code": result.artifacts.get("exit_code"),
            "tail": (result.artifacts.get("tail") or "")[-2000:],
        }
        records.append(record)
        if not result.ok:
            # 第一期没有诊断基线，取不到「新增」错误时按退出码判定。
            # 这会有误伤（历史遗留错误也算），但比「假装通过」正确。
            failure = _failure(
                FailureKind.DiagnosticError,
                f"{name} 在本次改动文件上报告了问题（退出码 {record['exit_code']}）",
                tool="verify", artifacts={"tail": record["tail"]})
            return records, failure

    return records, None


def _fail(state: LoopState, task: TaskNode, failure: Failure, emit,
          *, detail: str = "", commands: Optional[List[Dict[str, Any]]] = None,
          diagnostics: Optional[List[Dict[str, Any]]] = None) -> Event:
    """
    验证失败的统一收尾：**在这里消耗一次尝试额度**。

    额度归属刻意放在 verify 而不是 act：只有「跑过验收」才算一次真正的尝试，
    否则一次参数写错的只读调用也会吃掉修复预算（见架构文档 8.5）。
    """
    task.attempts += 1
    task.last_failure = failure
    count = repair.record(state, failure)
    state.verify_log.append({
        "task": task.id, "ok": False, "detail": detail or failure.message,
        "commands": commands or [], "diagnostics": diagnostics or [],
    })
    state.note(f"[{task.id}] 验收未通过（{failure.kind}）")
    return Event(EV.VERIFY_FAILED, {
        "task_id": task.id,
        "failure": failure.one_line(),
        "failure_kind": failure.kind,
        "signature": failure.signature,
        "count": count,
        "detail": detail or failure.message,
        "tail": failure.artifacts.get("tail", ""),
    })


def _failure(kind: FailureKind, message: str, tool: Optional[str] = None,
             artifacts: Optional[Dict[str, Any]] = None) -> Failure:
    return repair.classify_error(str(kind), message, tool, artifacts)


def _emit(emit, data: Dict[str, Any]) -> None:
    if emit is not None:
        emit("verify", data)
