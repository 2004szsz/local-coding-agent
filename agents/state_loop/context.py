# -*- coding: utf-8 -*-
"""
上下文预算与压缩。

**上下文是预算资源，不是日志。** 旧框架把每条 tool 结果都留在历史里线性追加，
这是上下文膨胀的主因；本模块把「模型看到什么」变成一件可以测量、可以裁剪的事。

预算组成（架构文档第 12 节）：

| 区块 | 策略 | 理由 |
|------|------|------|
| 系统提示词 | 图钉 | 角色与工具约定不能丢 |
| 任务图 | 图钉 | 每任务一行，成本固定且必须完整 |
| 当前任务正文 | 图钉 | `detail` 全文，上限 2000 字 |
| 最近观察 | **环**（默认 6 条） | 更旧的折叠成一行摘要 |
| 会话历史 | 只保留用户原话 + 上一轮答复 | **不回放旧框架的整段 tool 消息** |

两级压缩：先做**确定性折叠**（不调用模型），仍超软阈值才允许一次摘要调用。
压缩**不改任务图、不改 FileJournal**——它只影响「模型看到什么」，
不影响「机器知道什么」。这条边界保证了压缩不会污染控制面状态。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .machine import EV, Event, LoopLimits
from .state import HaltReason, LoopState, TaskGraph, TaskNode, ToolOutcome

__all__ = [
    "RING_SIZE", "build_messages", "estimate_chars", "over_soft_budget",
    "over_hard_budget", "fold_observations", "compress", "render_tool_catalog",
    "render_workspace_overview", "render_observation", "final_messages",
]

#: 观察环容量：完整保留的最近观察条数
RING_SIZE = 6
#: 单条观察渲染进上下文时的字符上限
OBSERVATION_CHARS = 1200
#: 折叠后一行摘要的长度
FOLDED_CHARS = 160
#: 超过这个工具数就切到「目录模式」（只给名字与一行描述）
CATALOG_FULL_LIMIT = 32
#: 任务 detail 的字符上限
TASK_DETAIL_CHARS = 2000
#: 摘要模型一次最多输出多少字
SUMMARY_LIMIT = 800


# ======================================================================
# 测量
# ======================================================================
def estimate_chars(messages: Sequence[Dict[str, Any]], extra_chars: int = 0) -> int:
    """
    估算一次请求的字符量。

    **用字符而不是 token**：本地模型没有可靠 tokenizer，硬引入 tiktoken 会带来
    一个与模型不匹配的依赖。按 `tokens ≈ chars / 2` 的保守估计，
    我们宁可早压缩——压缩的代价是信息有损，超窗的代价是整个请求失败。
    """
    total = max(0, int(extra_chars))
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content, ensure_ascii=False, default=str))
    return total


def over_soft_budget(chars: int, limits: LoopLimits) -> bool:
    return chars >= int(limits.context_char_budget * limits.soft_budget_ratio)


def over_hard_budget(chars: int, limits: LoopLimits) -> bool:
    return chars >= int(limits.context_char_budget * limits.hard_budget_ratio)


# ======================================================================
# 渲染
# ======================================================================
def render_observation(outcome: ToolOutcome, *, tail_lines: int = 80,
                       limit: int = OBSERVATION_CHARS) -> str:
    """
    渲染单条观察。

    命令类结果**优先取尾部**：错误栈、失败列表都在末尾，头尾对半截断会把关键信息切掉。
    这是对 `tools.base.truncate_output` 的专门化，只用于进入模型上下文这一步。
    """
    head = f"[{outcome.name}] {'ok' if outcome.ok else '失败:' + (outcome.failure.kind if outcome.failure else 'Error')}"
    code = outcome.exit_code
    if code is not None:
        head += f"（退出码 {code}）"

    body = outcome.text or ""
    if code is not None and outcome.tail:
        body = outcome.tail
    elif len(body) > limit:
        body = body[:limit] + f"\n...[已截断，原长 {len(outcome.text)} 字符]"
    return f"{head}\n{body.rstrip()}"


def render_observations(state: LoopState, limits: LoopLimits) -> str:
    if not state.observations:
        return "（本轮还没有工具调用）"
    return "\n\n".join(render_observation(o, tail_lines=limits.tail_lines)
                       for o in state.observations)


def render_tool_catalog(registry, names: Iterable[str]) -> str:
    """
    渲染模型可见工具清单。

    工具数 > `CATALOG_FULL_LIMIT` 时切到「目录模式」：只给名字与描述首行，不列参数。
    这是**预算措施**，不是第二套协议——参数校验仍由 `decisions.parse_decision` +
    `permissions` 在本地完成，模型写错参数会得到 `ArgError`，请求不会白跑一趟远端。
    """
    items: List[str] = []
    full = True
    available = [name for name in names if registry.has(name)]
    if len(available) > CATALOG_FULL_LIMIT:
        full = False

    for name in available:
        tool = registry.get(name)
        summary = (tool.description or "").strip().splitlines()
        line = f"- {name}: {summary[0] if summary else ''}"
        if full:
            props = ((tool.parameters or {}).get("properties") or {})
            required = set((tool.parameters or {}).get("required") or [])
            args = ", ".join(
                f"{key}{'*' if key in required else ''}" for key in props
            )
            if args:
                line += f"\n  参数: {args}（* 为必填）"
        items.append(line)

    if not full:
        items.append(f"（工具较多，此处只列名称与用途；参数以工具自身 schema 为准）")
    return "\n".join(items) if items else "（没有可用工具）"


def render_workspace_overview(workspace, *, max_entries: int = 60) -> str:
    """
    工作区概览：**规划阶段最有用的一条信息**。

    没有它，模型只能凭空猜文件路径，`scope` 与 `must_read` 会大量写错
    （然后被 accept 的越界校验拒掉，白烧一轮）。
    """
    if workspace is None:
        return "（工作区不可用）"
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
            ".idea", ".pytest_cache", "chroma", "logs"}
    lines: List[str] = []
    try:
        entries = workspace.list_dir(".")
    except Exception:  # noqa: BLE001 - 概览失败不该挡住规划
        return "（无法列出工作区）"

    for entry in entries:
        if entry["name"] in skip:
            continue
        if entry["type"] == "dir":
            lines.append(f"{entry['relpath']}/")
            if len(lines) >= max_entries:
                break
            try:
                children = workspace.list_dir(entry["relpath"])
            except Exception:  # noqa: BLE001
                continue
            for child in children[:12]:
                if child["name"] in skip:
                    continue
                suffix = "/" if child["type"] == "dir" else ""
                lines.append(f"{child['relpath']}{suffix}")
                if len(lines) >= max_entries:
                    break
        else:
            lines.append(entry["relpath"])
        if len(lines) >= max_entries:
            break

    if len(lines) >= max_entries:
        lines.append(f"...（已截断到 {max_entries} 条）")
    return "\n".join(lines) if lines else "（工作区为空）"


def render_graph(graph: Optional[TaskGraph]) -> str:
    if graph is None or not graph.tasks:
        return "（尚未建立任务图）"
    lines = []
    for node in graph.nodes():
        mark = {"done": "✓", "blocked": "✗", "ready": "→", "running": "▶"}.get(str(node.status), "·")
        deps = f" 依赖 {','.join(node.depends_on)}" if node.depends_on else ""
        lines.append(f"{mark} {node.id} [{node.status}] {node.title}{deps}")
    return "\n".join(lines)


def render_task_card(task: Optional[TaskNode]) -> str:
    if task is None:
        return "（没有当前任务）"
    return task.card(limit=TASK_DETAIL_CHARS)


# ======================================================================
# 组装请求
# ======================================================================
_STATE_LOOP_NOTE = (
    "## 运行方式\n"
    "流程由运行时（状态机）负责推进：你不需要自己决定「先读还是先改」，"
    "机器会在合适的时机给你任务卡与观察结果。\n"
    "你唯一的输出方式是调用 `submit_decision` 函数。**不要输出散文当作答复。**\n"
)


def build_messages(state: LoopState, deps, purpose: str) -> List[Dict[str, Any]]:
    """
    组装一次模型请求。

    :param purpose: `decompose` | `decide`
    """
    system = "\n\n".join(part for part in (
        deps.system_prompt,
        _STATE_LOOP_NOTE,
        # 执行档在 deps 上，不在 deps.config 上（config 是 LoopLimits，只装阈值）
        f"## 执行档\n{deps.mode}: 允许的自动动作范围见工具说明；越界操作会被拒绝。",
    ) if part)

    if purpose == "decompose":
        body = _decompose_user_block(state, deps)
    else:
        body = _decide_user_block(state, deps)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": body},
    ]


def _decompose_user_block(state: LoopState, deps) -> str:
    blocks = [
        "## 用户需求\n" + (state.user_text or "").strip(),
        "## 工作区文件\n" + render_workspace_overview(deps.workspace),
        "## 可用工具\n" + render_tool_catalog(deps.registry, deps.visible_tools()),
    ]
    if state.graph is not None:
        blocks.append("## 上一次的任务图（本次为重新规划）\n" + render_graph(state.graph))
        if state.current_task and state.current_task.last_failure:
            blocks.append("## 上次失败\n" + state.current_task.last_failure.one_line())
    if state.notes:
        blocks.append("## 已发生的事实\n" + "\n".join(f"- {n}" for n in state.notes[-8:]))

    blocks.append(
        "## 要求\n"
        "调用 submit_decision 提交任务图。\n"
        f"- 任务数 1..{deps.config.max_tasks}，每个任务要小到「一次改动 + 一条验收命令」能闭环；\n"
        "- 每个任务必须给出**非空 scope**（允许写入的相对路径或目录前缀）；\n"
        "- 验收命令写成 argv 数组，例如 [[\"python\",\"-m\",\"pytest\",\"tests/test_x.py\",\"-q\"]]；\n"
        "- 依赖用任务序号（从 1 开始）引用，不要自己编 id；\n"
        "- 只有无依赖的任务才会并行执行，因此请让互不相干的改动互相独立。"
    )
    return "\n\n".join(blocks)


def _decide_user_block(state: LoopState, deps) -> str:
    task = state.current_task
    blocks = [
        "## 当前任务\n" + render_task_card(task),
        "## 任务图进度\n" + render_graph(state.graph),
        "## 最近观察\n" + render_observations(state, deps.config),
    ]
    if state.graph is None or not state.graph.tasks:
        blocks.insert(1, "## 注意\n当前没有任务图。")
    if task is not None and state.current_task_id:
        blocks.append(f"## 本任务进度\n已尝试 {task.attempts}/{task.max_attempts}"
                      f"｜已感知: {'是' if task.perceived else '否'}"
                      f"｜当前文件已写入: {'是' if deps.journal and deps.journal.has_writes(task.id) else '否'}")

    blocks.append("## 可用工具\n" + render_tool_catalog(deps.registry, deps.visible_tools()))
    blocks.append(
        "## 要求\n"
        "调用 submit_decision 提交下一步动作。\n"
        "- 只允许使用上面列出的工具；写文件必须落在「可写范围」内；\n"
        "- 需要读更多代码就继续调用只读工具，但**不要连续只读而不推进任务**；\n"
        "- 认为任务已完成时用 kind=mark_done：机器仍会运行验收命令，你说完成不算完成；\n"
        "- 若任务划分本身不合理，用 kind=replan 并说明原因；\n"
        "- 缺关键信息无法继续时用 kind=ask_user 提出问题。"
    )
    return "\n\n".join(blocks)


def final_messages(state: LoopState, deps) -> List[Dict[str, Any]]:
    """
    收尾文本的请求。

    只允许散文，不允许再发工具调用——终态之后禁止任何副作用。
    这次调用失败时，机器会用任务图自己拼一段摘要（见 runtime.fallback_summary）。
    """
    lines = [f"## 用户需求\n{(state.user_text or '').strip()}"]
    if state.graph is not None:
        lines.append("## 任务结果\n" + render_graph(state.graph))
    if state.verify_log:
        lines.append("## 验收记录\n" + "\n".join(
            f"- {entry.get('task')}: {'通过' if entry.get('ok') else '未通过'}"
            f"｜{entry.get('detail', '')}" for entry in state.verify_log[-8:]))
    if state.notes:
        lines.append("## 机器记录\n" + "\n".join(f"- {n}" for n in state.notes[-10:]))
    lines.append(
        "## 要求\n"
        "用中文写一段面向用户的收尾说明，覆盖：做完了什么、改了哪些文件、验证结果如何、"
        "还有什么没做完或需要注意。直接输出正文，不要调用任何工具。"
    )
    return [
        {"role": "system", "content": deps.system_prompt or ""},
        {"role": "user", "content": "\n\n".join(lines)},
    ]


# ======================================================================
# 折叠与压缩
# ======================================================================
def fold_observations(state: LoopState, limits: LoopLimits) -> int:
    """
    确定性折叠（**不调用模型**）：把环外的观察压成一行摘要。

    :return: 折叠掉的条数
    """
    observations = list(state.observations)
    if len(observations) <= RING_SIZE:
        return 0

    folded: List[ToolOutcome] = []
    for outcome in observations[:-RING_SIZE]:
        if outcome.artifacts.get("folded"):
            folded.append(outcome)
            continue
        line = outcome.summary_line()[:FOLDED_CHARS]
        folded.append(ToolOutcome(
            call_id=outcome.call_id,
            name=outcome.name,
            ok=outcome.ok,
            text=line,
            failure=None,
            artifacts={**outcome.artifacts, "folded": True},
            effect=outcome.effect,
            duration_ms=outcome.duration_ms,
        ))
    state.observations = tuple(folded + observations[-RING_SIZE:])
    return len(observations) - RING_SIZE


async def compress(state: LoopState, deps, limits: LoopLimits,
                   emit=None) -> Event:
    """
    compress 阶段的执行体。

    顺序固定：**先确定性折叠，再考虑摘要调用**。原因是折叠零成本零风险，
    而摘要调用会引入一次模型请求（可能失败、可能编造）。只有折叠不够时才升级。
    """
    before = estimate_chars(build_messages(state, deps, "decide"))
    fold_observations(state, limits)
    after_fold = estimate_chars(build_messages(state, deps, "decide"))

    detail = {"chars_before": before, "chars_after_fold": after_fold}
    if over_hard_budget(after_fold, limits):
        state.chars_used = after_fold
        return Event(EV.BUDGET_HARD, {**detail, "chars_used": after_fold})

    if over_soft_budget(after_fold, limits) and deps.can_call_model():
        summarized = await _summarize(state, deps, emit)
        if summarized:
            detail["summarized"] = True

    after = estimate_chars(build_messages(state, deps, "decide"))
    state.chars_used = after
    if over_hard_budget(after, limits):
        return Event(EV.BUDGET_HARD, {**detail, "chars_after": after, "chars_used": after})
    return Event(EV.COMPRESSED, {**detail, "chars_after": after, "chars_used": after})


async def _summarize(state: LoopState, deps, emit) -> bool:
    """
    一次受限于 800 字的「已做完的事实」摘要，替换掉环外的观察。

    输入是**已被折叠的观察**，不是整段历史——否则摘要请求自己就会超预算。
    失败时保留折叠结果并返回 False，**不重试**：上下文已经紧张，再烧一次不值得。
    """
    folded = [o for o in state.observations if o.artifacts.get("folded")]
    if not folded:
        return False
    material = "\n".join(render_observation(o, limit=300) for o in folded)
    messages = [
        {"role": "system",
         "content": "你负责把工具调用记录压缩成事实清单。只输出事实，不要建议、不要客套。"},
        {"role": "user",
         "content": (f"把下面的记录压缩成不超过 {SUMMARY_LIMIT} 字的「已发生的事实」清单，"
                     f"每行一条，保留文件路径与命令结论：\n\n{material}")},
    ]
    try:
        response = await deps.call_model(messages)
    except Exception:  # noqa: BLE001 - 摘要失败不影响主流程
        return False

    text = str((response or {}).get("content") or "").strip()[:SUMMARY_LIMIT]
    if not text:
        return False

    keep = [o for o in state.observations if not o.artifacts.get("folded")]
    summary = ToolOutcome(
        call_id="summary", name="历史摘要", ok=True, text=text,
        artifacts={"folded": True, "summary": True}, effect=0,
    )
    state.observations = tuple([summary] + keep)
    return True
