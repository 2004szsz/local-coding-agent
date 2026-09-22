# -*- coding: utf-8 -*-
"""
任务调度：选下一个可运行任务、计算写租约、生成感知批次。

调度的输入是**任务图**，不是模型的话。本模块的所有输出都是「事件」或「只读批次」：
它不执行写入，也不定义任务何时算完成（那是 verify 的事）。

三个职责，各自独立：

1. `pick()` —— schedule 状态该往哪走：感知 / 决策 / 委派 / 结束 / 阻塞。
   优先级刻意如此：**感知门闩 > 委派**。没读过代码的任务不允许先动笔。
2. `lease()` / `select_parallel_batch()` —— 写路径租约与并行批次的贪心选择。
3. `perceive_batch()` —— 固定只读批次。模型不能决定第一批读什么，
   否则「强制性」就退化成提示词里的叮嘱。
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .machine import EV, Event, LoopLimits
from .state import Effect, TaskGraph, TaskNode, TaskStatus, ToolCall

__all__ = [
    "pick", "lease", "leases_conflict", "select_parallel_batch",
    "perceive_batch", "perceive_extras", "extract_paths",
    "mark_task_done", "has_symbol",
]

#: 默认每个文件先读多少行。大文件整读会挤掉「任务卡 + 工具目录」的预算。
DEFAULT_HEAD_LINES = 200
#: 搜索命中后额外精读的文件数上限
DEFAULT_MAX_EXTRA_READS = 3

# file_search 的输出形如 "app/main.py:12: 命中行"
_FILE_SEARCH_HIT = re.compile(r"^([^\s:]+\.\w+):\d+:", re.MULTILINE)
# rag_search 的输出形如 "--- 命中 1 (相似度距离 0.1) app/main.py::func 行1-20 ---"
_RAG_SEARCH_HIT = re.compile(r"^\s*---\s*命中\s*\d+.*?([^\s:()]+\.\w+)::", re.MULTILINE)
#: 判断 detail 里有没有「具体符号」——文件名、函数名、下划线/驼峰标识符
_SYMBOL_HINT = re.compile(r"[\w\-/]+\.\w{1,5}\b|[A-Za-z_]\w*_\w*|[a-z][A-Z]")


# ======================================================================
# 1. 调度
# ======================================================================
def pick(state, limits: LoopLimits) -> Event:
    """schedule 状态的唯一决策点。返回事件，不返回目标状态。"""
    graph: Optional[TaskGraph] = state.graph
    if graph is None or not graph.tasks:
        return Event(EV.GRAPH_BLOCKED, {"reason": "没有可用任务图"})

    if graph.all_finished():
        return Event(EV.ALL_DONE, {"progress": graph.progress_line()})

    ready = graph.ready_ids()
    if not ready:
        return Event(EV.GRAPH_BLOCKED, {
            "blocked": graph.blocked_ids(),
            "unfinished": graph.unfinished_ids(),
            "reason": "没有依赖已满足且未结束的任务",
        })

    batch = select_parallel_batch(state, limits)
    if len(batch) >= 2:
        return Event(EV.CAN_DELEGATE, {"task_ids": [node.id for node in batch]})

    target = _focus_task(state, ready)
    if not target.perceived:
        return Event(EV.NEED_PERCEIVE, {"task_id": target.id})
    return Event(EV.NEED_DECIDE, {"task_id": target.id})


def _focus_task(state, ready_ids: Sequence[str]) -> TaskNode:
    """
    选当前要处理的任务。

    优先**继续**手头这个：多任务并行排布时，如果每轮都重新挑「拓扑序第一个」，
    模型会在任务之间反复横跳，每次都丢掉上一个任务的上下文。
    """
    current = state.current_task
    if current is not None and current.id in ready_ids and not current.status.is_finished:
        return current
    graph = state.graph
    for task_id in ready_ids:
        node = graph.get(task_id)
        if node is not None:
            return node
    raise RuntimeError("ready 列表非空却挑不出任务，任务图结构已损坏")


# ======================================================================
# 2. 写租约与并行
# ======================================================================
def lease(node: TaskNode) -> Tuple[str, ...]:
    """
    任务的写路径租约 = 它的 scope。

    命名成「租约」而不是「scope」，是因为它在两处被消费：权限（能不能写）
    与调度（能不能并行）。这两件事共享同一个值，才不会出现「权限允许但并行冲突」的缝。
    """
    return tuple(node.scope)


def leases_conflict(left: Iterable[str], right: Iterable[str]) -> bool:
    """
    两个租约是否冲突。**前缀关系也算冲突**：

    `app/` 与 `app/main.py` 看起来是两个不同的字符串，但一个会改到另一个的目录。
    只做集合求交会漏掉这种情况，而这类漏检的后果是两个子代理互相覆盖对方的写入。
    """
    for a in left:
        for b in right:
            if a == b:
                return True
            if a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def select_parallel_batch(state, limits: LoopLimits) -> List[TaskNode]:
    """
    贪心挑出「两两租约不相交」的并行批次，按拓扑序，上限 `delegate_parallel`。

    为什么是贪心而不是「全对两两检查」：后者只要有一对冲突就整批不并行，
    3 个任务里有 1 对冲突时会白白浪费另外两个的并行机会。
    """
    if state.depth >= limits.max_depth:
        return []
    graph = state.graph
    if graph is None:
        return []

    batch: List[TaskNode] = []
    taken: List[Tuple[str, ...]] = []
    for task_id in graph.ready_ids():
        node = graph.get(task_id)
        if node is None or node.perceived:
            continue                      # 已感知的任务已经在处理，不再重复委派
        keys = lease(node)
        # 外部根写入无法用租约证明不相交（授权根可能重叠），强制串行。
        if any(":" in str(item) for item in keys):
            continue
        if any(leases_conflict(keys, other) for other in taken):
            continue
        batch.append(node)
        taken.append(keys)
        if len(batch) >= limits.delegate_parallel:
            break
    return batch


# ======================================================================
# 3. 感知批次
# ======================================================================
def has_symbol(text: str) -> bool:
    """任务描述里有没有「具体符号」。有的话直接定位更准，不必做语义检索。"""
    return bool(_SYMBOL_HINT.search(text or ""))


def perceive_batch(
    task: TaskNode,
    available_tools: Iterable[str],
    *,
    rag_available: bool = False,
    head_lines: int = DEFAULT_HEAD_LINES,
) -> Tuple[List[ToolCall], List[str]]:
    """
    为任务生成**固定**的只读批次。

    1. `must_read` 逐个读（感知下限，模型声明的「动手前必须看」的文件）
    2. 描述里没有具体符号时，再补一次检索
    3. 检索命中的文件在 `perceive_extras` 里精读（需要先拿到检索结果）

    :return: (calls, notes)。notes 会作为 thought 事件让用户看到「机器在读什么」
    """
    tools = set(available_tools)
    calls: List[ToolCall] = []
    notes: List[str] = []

    for path in task.acceptance.must_read:
        calls.append(_read_call(len(calls), path, head_lines))
    if calls:
        notes.append(f"读必读文件 {len(calls)} 个")

    if not has_symbol(task.detail) and not has_symbol(task.title):
        prefer_fs = any(":" in str(item) for item in task.scope)
        search = _pick_search_tool(tools, rag_available, prefer_fs=prefer_fs)
        if search is not None:
            calls.append(_search_call(len(calls), search, task.title, task.scope))
            notes.append(f"任务描述没有具体符号，先用 {search} 定位文件")

    return calls, notes


def perceive_extras(
    task: TaskNode,
    search_outcomes: Sequence[Any],
    already_read: Iterable[str],
    *,
    max_extra: int = DEFAULT_MAX_EXTRA_READS,
    head_lines: int = DEFAULT_HEAD_LINES,
) -> List[ToolCall]:
    """
    把检索结果转成精读批次：命中文件里**落在 scope 内且还没读过**的，最多再读 `max_extra` 个。

    只读 scope 内的文件，避免感知阶段就把上下文灌满无关代码。
    """
    seen = {str(p).replace("\\", "/") for p in already_read}
    picked: List[str] = []

    for outcome in search_outcomes:
        if not getattr(outcome, "ok", False):
            continue
        for path in extract_paths(getattr(outcome, "name", ""), getattr(outcome, "text", "")):
            normalized = path.replace("\\", "/")
            if normalized in seen:
                continue
            if not _in_scope(normalized, task.scope):
                continue
            seen.add(normalized)
            picked.append(normalized)
            if len(picked) >= max_extra:
                break
        if len(picked) >= max_extra:
            break

    return [_read_call(index, path, head_lines) for index, path in enumerate(picked)]


def extract_paths(tool_name: str, text: str) -> List[str]:
    """
    从检索工具的输出里抽出文件路径。

    这里解析的是**我们自己工具的输出格式**（tools/search.py、tools/rag_tools.py），
    不是任意第三方文本，所以用固定正则即可；一旦输出格式变化，两处必须同步改，
    因此两个正则就放在本模块顶部显眼位置。
    """
    if not text:
        return []
    pattern = _RAG_SEARCH_HIT if tool_name == "rag_search" else _FILE_SEARCH_HIT
    found: List[str] = []
    for match in pattern.finditer(text):
        candidate = match.group(1).strip()
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def _in_scope(path: str, scope: Iterable[str]) -> bool:
    from .permissions import path_in_scope      # 局部导入：避免 state→permissions 的环

    return path_in_scope(path, scope)


def _pick_search_tool(tools: Iterable[str], rag_available: bool,
                      *, prefer_fs: bool = False) -> Optional[str]:
    """
    选检索工具。**优先关键词检索，而不是语义检索**：

    本地 7B 模型 + 中文注释 + 小仓库的场景下，ripgrep 式精确匹配比余弦相似度更准更便宜。
    RAG 的定位是「不知道目标叫什么」时的兜底召回，不是主路径。
    """
    available = set(tools)
    if prefer_fs and "fs_search" in available:
        return "fs_search"
    if rag_available and "rag_search" in available:
        return "rag_search"
    if "file_search" in available:
        return "file_search"
    if "fs_search" in available:
        return "fs_search"
    return None


def _read_call(index: int, path: str, head_lines: int) -> ToolCall:
    from .planner import parse_root_rel

    parsed = parse_root_rel(path)
    if parsed is not None:
        root, rel = parsed
        return ToolCall(
            id=f"p{index + 1}", name="fs_read",
            arguments={"root": root, "path": rel, "start_line": 1, "end_line": head_lines},
            effect=Effect.L0_READ)
    return ToolCall(id=f"p{index + 1}", name="read_file",
                    arguments={"path": path, "start_line": 1, "end_line": head_lines},
                    effect=Effect.L0_READ)


def _search_call(index: int, tool: str, query: str,
                 scope: Iterable[str] = ()) -> ToolCall:
    if tool == "rag_search":
        arguments: Dict[str, Any] = {"query": query, "top_k": 5}
    elif tool == "fs_search":
        from .planner import parse_root_rel
        root = "desktop"
        for item in scope:
            parsed = parse_root_rel(str(item))
            if parsed is not None:
                root = parsed[0]
                break
        arguments = {"root": root, "pattern": query, "max_results": 20}
    else:
        arguments = {"pattern": query, "max_results": 20}
    return ToolCall(id=f"p{index + 1}", name=tool, arguments=arguments, effect=Effect.L0_READ)


# ======================================================================
# 4. 任务收尾记账
# ======================================================================
def mark_task_done(state, task: TaskNode) -> List[str]:
    """
    验收通过后的记账：任务标 done，并把依赖它的任务提升为 ready。

    **这是「新并行度的唯一来源」**：任务图一开始只有无依赖任务可跑，
    后续的 ready 全靠这里解锁。返回本次被解锁的任务 id（用于事件与日志）。
    """
    task.status = TaskStatus.done
    task.last_failure = None
    if state.graph is None:
        return []
    return state.graph.promote_ready()
