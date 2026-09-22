# -*- coding: utf-8 -*-
"""
失败分类、签名归一化与修复策略。

三件事，各自独立、各自可单测：

1. `classify_error()` —— 把「异常类名 / 错误标识 + 消息」映射成一个 `FailureKind`。
   **顺序固定：先按类型，再按关键词，最后兜底。** 类型是第一等信号，
   关键词是补漏（给那些只能抛内置异常的旧工具），兜底保证「分类永不失败」。
2. `signature()` —— 归一化成稳定签名，供停滞检测计数。
   不归一化的话，同一个错误每次报的行号都不同，`failure_counts` 永远只到 1，
   停滞检测就变成「写了但永远不触发」的死代码——这是最容易踩的坑。
3. `choose()` —— 纯函数，根据失败决定下一个事件（增量 / 回滚 / 重规划 / 阻塞 / 停机）。

**本模块从不改文件。** 补丁始终由模型在 decide 里提出，再走完整的
authorize → act → verify。这条约束换来「模型是唯一写入者」：
审计时「谁改的」只有一个答案，且修复路径与正常路径享有同一套权限与验证。
"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, Optional, Tuple

from .machine import EV, LoopLimits
from .state import (
    Failure,
    FailureKind,
    LoopState,
    TaskNode,
    TaskStatus,
)

__all__ = [
    "classify_error", "signature", "record", "choose", "clear_task_failure",
    "NON_RETRYABLE", "STALL_THRESHOLD", "FAILURE_KIND_HINTS",
]

#: 同一签名出现这么多次就判定停滞。取 2 而不是 3：本地小模型重复同一错误的概率很高，
#: 给它三次机会通常是三次相同的失败，白白烧掉上下文与时间。
STALL_THRESHOLD = 2

#: 不可自动重试的失败：重试只会得到同样的结果，必须换策略或交还用户
NON_RETRYABLE = frozenset({
    FailureKind.PathDenied, FailureKind.PermissionDenied, FailureKind.ScopeViolation,
    FailureKind.LoopStall, FailureKind.BudgetExhausted, FailureKind.Cancelled,
})

#: 失败类别 → 一句话修复动作。用于日志与事件，避免「分类有了但人看不懂」。
FAILURE_KIND_HINTS: Dict[str, str] = {
    str(FailureKind.ModelProtocolError): "模型没有提交合法决策 → 同状态重试一次",
    str(FailureKind.ArgError): "参数不对 → 带着错误原文让模型改参数",
    str(FailureKind.NotFound): "目标不存在 → 让模型重新定位",
    str(FailureKind.PathDenied): "路径越界 → 任务阻塞，不重试",
    str(FailureKind.PermissionDenied): "权限被拒 → 任务阻塞，不自动改道",
    str(FailureKind.PatchConflict): "补丁定位失败 → 回滚到检查点后重新读文件",
    str(FailureKind.CommandFailed): "命令退出码非 0 → 带着输出尾部让模型改代码",
    str(FailureKind.Timeout): "超时 → 同命令最多再试一次",
    str(FailureKind.SandboxViolation): "沙箱拒绝 → 改用 run_command",
    str(FailureKind.TestFailed): "验收失败 → 增量修复",
    str(FailureKind.DiagnosticError): "静态检查报新增错误 → 增量修复",
    str(FailureKind.ScopeViolation): "写到范围外 → 回滚；再犯则重规划",
    str(FailureKind.McpError): "MCP 调用失败 → 最多重试一次",
    str(FailureKind.ToolError): "未归类工具失败 → 按可修复处理",
    str(FailureKind.LoopStall): "同一失败重复 → 停机",
    str(FailureKind.BudgetExhausted): "预算耗尽 → 停机",
    str(FailureKind.Cancelled): "用户取消 → 停机",
}

# ======================================================================
# 1. 分类
# ======================================================================
#: 异常类名 / 工具自报的 error_type → 失败类别。
#: 工具自报的 error_type 可以**直接写 FailureKind 的值**（见 tools/shell.py），
#: 因此这张表同时服务「异常」与「显式标识」两种来源。
_TYPE_TO_KIND: Dict[str, FailureKind] = {
    # 找不到
    "NotFound": FailureKind.NotFound,
    "KeyError": FailureKind.NotFound,
    "FileNotFoundError": FailureKind.NotFound,
    "NotADirectoryError": FailureKind.NotFound,
    # 参数
    "ArgError": FailureKind.ArgError,
    "ValueError": FailureKind.ArgError,
    "TypeError": FailureKind.ArgError,
    "AttributeError": FailureKind.ArgError,
    "IsADirectoryError": FailureKind.ArgError,
    "UnicodeDecodeError": FailureKind.ArgError,
    "UnicodeError": FailureKind.ArgError,
    # 越界 / 权限
    "PathDenied": FailureKind.PathDenied,
    "PathTraversalError": FailureKind.PathDenied,
    "PermissionDenied": FailureKind.PermissionDenied,
    "PermissionError": FailureKind.PermissionDenied,
    "ScopeViolation": FailureKind.ScopeViolation,
    "ScopeViolationError": FailureKind.ScopeViolation,
    # 补丁
    "PatchConflict": FailureKind.PatchConflict,
    "PatchConflictError": FailureKind.PatchConflict,
    # 命令
    "CommandFailed": FailureKind.CommandFailed,
    "CommandFailedError": FailureKind.CommandFailed,
    "CalledProcessError": FailureKind.CommandFailed,
    "OSError": FailureKind.CommandFailed,
    "Timeout": FailureKind.Timeout,
    "TimeoutError": FailureKind.Timeout,
    "TimeoutExpired": FailureKind.Timeout,
    "CommandTimeoutError": FailureKind.Timeout,
    # 沙箱 / MCP
    "SandboxViolation": FailureKind.SandboxViolation,
    "SandboxViolationError": FailureKind.SandboxViolation,
    "McpError": FailureKind.McpError,
    "McpToolError": FailureKind.McpError,
    # 校验
    "TestFailed": FailureKind.TestFailed,
    "DiagnosticError": FailureKind.DiagnosticError,
    "ModelProtocolError": FailureKind.ModelProtocolError,
}

#: 关键词补漏。只在类型表未命中时使用，因此**必须写得具体**，
#: 否则会把一个偶发消息误判成别的类别。顺序 = 优先级。
_KEYWORD_TO_KIND: Tuple[Tuple[str, FailureKind], ...] = (
    ("old_string", FailureKind.PatchConflict),
    ("未在文件中找到", FailureKind.PatchConflict),
    ("非唯一匹配", FailureKind.PatchConflict),
    ("安全拦截", FailureKind.PathDenied),
    ("超出工作空间", FailureKind.PathDenied),
    ("不在允许列表", FailureKind.PermissionDenied),
    ("编译被拒绝", FailureKind.SandboxViolation),
    ("超时", FailureKind.Timeout),
    ("timed out", FailureKind.Timeout),
    ("不在本任务允许范围", FailureKind.ScopeViolation),
)


#: **泛用异常类型**。这些类型几乎不携带信息（`ValueError` 可能是任何事），
#: 因此当它们出现时，消息里的具体关键词比类型更可信——关键词可以覆盖它们的判定。
_GENERIC_TYPES = frozenset({
    "ValueError", "TypeError", "RuntimeError", "Exception", "OSError",
    "ToolError", "Error", "ArgError", "",
})


def classify_error(error_type: str, message: str, tool: Optional[str] = None,
                   artifacts: Optional[Dict] = None) -> Failure:
    """
    构造一个分类明确的 `Failure`。

    判定顺序：**先按类型；类型缺失或属于泛用类型时，再按关键词；最后兜底**。
    泛用类型要走关键词，是因为 `ValueError("未在文件中找到 old_string")` 与
    `ValueError("缺少必填参数: path")` 是两种完全不同的修复动作，
    只看类型会把它们压成同一类。

    :param error_type: `ToolResult.error_type`（异常类名，或工具显式给出的 FailureKind 值）
    """
    key = (error_type or "").strip()
    kind = _TYPE_TO_KIND.get(key)

    if kind is None or key in _GENERIC_TYPES:
        lowered = (message or "").lower()
        for needle, candidate in _KEYWORD_TO_KIND:
            if needle.lower() in lowered:
                kind = candidate
                break

    if kind is None:
        kind = FailureKind.ToolError

    failure = Failure(
        kind=str(kind),
        message=(message or "").strip() or str(kind),
        tool=tool,
        retryable=str(kind) not in {str(k) for k in NON_RETRYABLE},
        artifacts=dict(artifacts or {}),
    )
    failure.signature = signature(failure.kind, failure.tool, failure.message)
    return failure


# ======================================================================
# 2. 签名
# ======================================================================
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_WS = re.compile(r"\s+")
#: 会随每次运行变化、但不改变错误本质的噪声。逐条去掉，只保留错误的「语义指纹」。
_NOISE_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bline \d+\b", re.I), "line N"),
    (re.compile(r"\blines \d+-\d+\b", re.I), "lines N"),
    (re.compile(r"\brow \d+\b", re.I), "row N"),
    (re.compile(r"行\s*\d+"), "行 N"),
    (re.compile(r"第\s*\d+\s*行"), "第 N 行"),
    (re.compile(r":\d+:\d+\b"), ":N:N"),                 # file.py:12:34
    (re.compile(r":\d+\b"), ":N"),
    (re.compile(r"\b\d+(\.\d+)?\s*(ms|s|秒)\b"), "T"),    # 耗时
    (re.compile(r"\b\d+\.\d+s\b"), "T"),
    # 临时目录整段抹掉：pytest 的临时路径每次都变，而且变的不只是数字
    # （`/tmp/pytest-of-user/pytest-12/test_x0`），只归一化数字是不够的
    (re.compile(r"(/tmp/\S+|\\Temp\\\S+|/var/folders/\S+)"), "TMP"),
    (re.compile(r"pytest-\d+"), "TMP"),
    (re.compile(r"[A-Za-z]:\\\\?Users\\\\?[^\\\s]+\\\\?AppData\\\\?Local\\\\?Temp\\\\?"), "TMP"),
    (re.compile(r"\b0x[0-9a-fA-F]{6,}\b"), "ADDR"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\b"), "TS"),
)


def normalize_for_signature(message: str, limit: int = 200) -> str:
    """
    去掉「每次都会变」的部分，保留错误的语义指纹。

    这是停滞检测能生效的前提：行号、耗时、临时路径一变就换签名的话，
    同一个错误永远不会被数到第二次。
    """
    text = _ANSI.sub("", message or "")
    for pattern, repl in _NOISE_PATTERNS:
        text = pattern.sub(repl, text)
    text = _WS.sub(" ", text).strip()
    return text[:limit]


def signature(kind: str, tool: Optional[str], message: str) -> str:
    """稳定签名：`sha1(kind|tool|normalized_message)[:12]`。"""
    material = f"{kind}|{tool or ''}|{normalize_for_signature(message)}"
    return hashlib.sha1(material.encode("utf-8", errors="replace")).hexdigest()[:12]


# ======================================================================
# 3. 记账与策略
# ======================================================================
def record(state: LoopState, failure: Failure) -> int:
    """
    记录一次失败并返回该签名的累计次数。就地更新 `failure_counts`（记录级字段）。

    只增不清：次数是「本次用户回合内这个错误出现过几次」，跨任务共用一个计数池，
    这样「换个任务犯同一个错」也会被发现。
    """
    if not failure.signature:
        failure.signature = signature(failure.kind, failure.tool, failure.message)
    key = failure.signature
    state.failure_counts[key] = state.failure_counts.get(key, 0) + 1
    return state.failure_counts[key]


def scope_violation_count(state: LoopState, task: TaskNode) -> int:
    """本任务累计越界次数。用于「再犯一次就重规划」的升级判断。"""
    return state.failure_counts.get(f"scope:{task.id}", 0)


def bump_scope_violation(state: LoopState, task: TaskNode) -> int:
    key = f"scope:{task.id}"
    state.failure_counts[key] = state.failure_counts.get(key, 0) + 1
    return state.failure_counts[key]


def clear_task_failure(task: TaskNode) -> None:
    """任务被阻塞或跳过时清掉 last_failure，避免它继续污染后续任务卡。"""
    task.last_failure = None


def choose(state: LoopState, failure: Failure, limits: LoopLimits,
           count: int = 1) -> str:
    """
    选择修复策略，返回事件名。**纯函数**：只读 state 与 count，不做任何写入。

    判定顺序即优先级，每一步的理由都写在行内注释里。
    """
    node = state.current_task

    # ① 停滞优先于一切：同一个错误重复出现，再换策略也是白烧预算
    if count >= STALL_THRESHOLD:
        return EV.STALLED

    kind = failure.kind

    # ② 权限与越界类失败不可重试，任务直接阻塞（不自动改道去试同一件事）
    if kind in (str(FailureKind.PermissionDenied), str(FailureKind.PathDenied)):
        return EV.REPAIR_BLOCKED

    # ③ 必读文件不存在 → 不是「参数写错了」，而是任务描述里的文件名本身就是错的。
    #    改参数再试没有意义，必须重新规划。用 artifacts 传递这个语义，
    #    让「改参数」与「改计划」分开，而不是靠消息里的路径去猜。
    if kind == str(FailureKind.NotFound) and failure.artifacts.get("must_read"):
        return EV.REPAIR_REPLAN

    # ④ 写到范围外：先回滚；同一个任务再犯，说明任务划分本身有问题 → 重规划
    if kind == str(FailureKind.ScopeViolation):
        if node is not None and scope_violation_count(state, node) >= 1:
            return EV.REPAIR_REPLAN
        return EV.REPAIR_ROLLBACK

    # ⑤ 补丁定位失败：文件已被改动，继续对着旧内容打补丁必然再失败 → 回滚后重读
    if kind == str(FailureKind.PatchConflict):
        return EV.REPAIR_ROLLBACK

    # ⑥ 盲改已两次仍不过验证：回到干净状态重新读真实文件，比第三次盲改更有效
    if kind in (str(FailureKind.TestFailed), str(FailureKind.CommandFailed)):
        if node is not None and node.attempts >= max(1, limits.max_attempts - 1):
            return EV.REPAIR_ROLLBACK
        return EV.REPAIR_EDIT

    # ⑦ 其余（参数错、找不到、超时、沙箱、诊断、MCP、未归类）一律增量修复，
    #    把失败原文放进下一次 decide 的观察里，由模型提出新补丁。
    return EV.REPAIR_EDIT
