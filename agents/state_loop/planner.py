# -*- coding: utf-8 -*-
"""
需求拆解与项目规划：把「一句需求」变成**可执行、可验收、可并行**的任务图。

这两个形容词不是修辞，它们各自对应一组字段：

    可执行 → depends_on（拓扑序，决定顺序与并行度）
    可验收 → acceptance.commands（完成定义）
    可并行 → scope（写路径边界，决定两个任务能不能同时跑）

本模块只做两件事，都是确定性代码：

1. `is_trivial()` —— 判断这条消息值不值得建图（小任务短路）。
2. `accept()` —— 把模型提交的计划转成 `TaskGraph`，或者给出「为什么不行」。

**accept 的九条校验一条也不依赖模型自查。** 校验失败不执行任何工具——
宁可让模型重规划一次，也不执行半张图：截断会丢掉依赖边，
产生「验收命令引用了没被改的文件」这类极难定位的失败。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from tools.fs_access import AccessDeniedError
from tools.shell import ARGV_ALLOWLIST, PIP_SUBCOMMANDS, normalize_argv0
from tools.workspace import PathTraversalError, WorkspaceSecurity

from .machine import LoopLimits
from .state import (
    Acceptance,
    Failure,
    FailureKind,
    TaskGraph,
    TaskNode,
    TaskStatus,
)

__all__ = ["accept", "is_trivial", "parse_root_rel", "ACTION_VERBS"]

#: 出现这些词就认为「这是一件要动手的事」，不能短路。
#: 故意写得宽：把该拆的误判成闲聊的代价（用户得再说一次）比反过来（多走一轮拆解）更大。
ACTION_VERBS = (
    "改", "修", "加", "删", "写", "建", "补", "替换", "调整", "重构", "优化",
    "实现", "新增", "移除", "迁移", "升级", "清理", "格式化", "拆分", "合并",
    "测试", "验证", "运行", "跑", "执行", "编译", "构建", "部署", "打包",
    "排查", "定位", "调试", "复现", "接入", "对接", "联调", "安装", "配置",
    "查看", "查询", "列出", "打开", "截图", "通知", "启动", "整理", "移动", "复制",
)


def is_trivial(text: str, has_active_graph: bool = False) -> bool:
    """
    是否可短路（不建任务图，直接给一次无工具回复）。

    规则：**消息里没有动作动词**，且**会话里没有未完成的任务图**。
    已有任务图时绝不短路——否则用户中途补充的一句话会把计划丢掉。
    """
    if has_active_graph:
        return False
    body = (text or "").strip()
    if not body:
        return True
    return not any(verb in body for verb in ACTION_VERBS)


def _invalid(message: str) -> Failure:
    failure = Failure(kind=str(FailureKind.ModelProtocolError), message=message,
                      tool="planner", retryable=True)
    failure.signature = ""
    return failure


def parse_root_rel(entry: str) -> Optional[Tuple[str, str]]:
    """
    把「root:rel」形态解析成 (root, rel)。

    工作区路径不含冒号（Windows 盘符出现在绝对路径里，会被 WorkspaceSecurity 拦掉），
    所以用第一个冒号切分是安全的。`desktop:` 表示根目录本身。
    """
    text = str(entry or "").strip().replace("\\", "/")
    if not text or ":" not in text:
        return None
    root, _, rel = text.partition(":")
    root = root.strip()
    rel = rel.strip().lstrip("/")
    if not root:
        return None
    return root, rel or "."


def _normalize_scope_entry(entry: str, workspace: WorkspaceSecurity,
                           broker: Any = None) -> Tuple[Optional[str], Optional[Failure]]:
    """工作区路径走 WorkspaceSecurity；`root:rel` 走 AccessBroker。"""
    text = str(entry or "").strip()
    if not text:
        return None, _invalid("scope / must_read 条目不能为空")

    parsed = parse_root_rel(text)
    if parsed is not None:
        if broker is None:
            return None, _invalid(
                f"scope 「{text}」引用了授权根，但本次运行未启用 local_access")
        root, rel = parsed
        try:
            broker.resolve(root, rel)
        except PathTraversalError as e:
            return None, _invalid(f"scope 越界：{e}")
        except (AccessDeniedError, PermissionError, OSError, ValueError) as e:
            return None, _invalid(f"scope 不可用：{e}")
        return f"{root}:{rel}", None

    try:
        resolved = workspace.resolve(text)
    except PathTraversalError as e:
        return None, _invalid(f"scope 越界：{e}")
    return workspace.relpath(resolved).rstrip("/"), None


def accept(raw: Dict[str, Any], workspace: WorkspaceSecurity,
           limits: LoopLimits, broker: Any = None
           ) -> Tuple[Optional[TaskGraph], Optional[Failure]]:
    """
    把 `decisions.parse_plan` 归一化后的计划转成 `TaskGraph`。

    :param broker: 启用本地访问时传入 AccessBroker，允许 scope / must_read 使用 `root:rel`。
    :return: `(task_graph, None)` 或 `(None, failure)`
    """
    items: List[Dict[str, Any]] = list(raw.get("tasks") or [])

    # ① 任务数量：超上限直接判无效，不截断
    if not items:
        return None, _invalid("任务图至少要有 1 个任务")
    if len(items) > limits.max_tasks:
        return None, _invalid(
            f"任务数 {len(items)} 超过上限 {limits.max_tasks}，请合并到 {limits.max_tasks} 个以内"
            f"（不要用「一个任务包多个文件」来绕过，那样验收命令就没法定位失败）")

    # ② 标题唯一：depends_on 允许用标题引用，重名会让引用产生歧义
    titles: Dict[str, int] = {}
    for index, item in enumerate(items):
        title = item.get("title") or ""
        if title in titles:
            return None, _invalid(f"任务标题重复: 「{title}」，请让每个任务标题唯一")
        titles[title] = index
    lowered_titles = {t.lower(): i for t, i in titles.items()}

    ids = [f"t{i + 1}" for i in range(len(items))]

    def resolve_dep(token: Any) -> Optional[str]:
        text = str(token).strip()
        if not text:
            return None
        if text.isdigit():
            position = int(text) - 1
            return ids[position] if 0 <= position < len(ids) else None
        if text in ids:
            return text
        if text in titles:
            return ids[titles[text]]
        if text.lower() in lowered_titles:
            return ids[lowered_titles[text.lower()]]
        return None

    # ③ 逐任务转换与校验
    nodes: Dict[str, TaskNode] = {}
    for index, item in enumerate(items):
        task_id = ids[index]
        title = item["title"]

        scope_raw: List[str] = list(item.get("scope") or [])
        if not scope_raw:
            return None, _invalid(f"[{title}] 缺少 scope：不清楚能改哪些文件就不能执行")

        scope: List[str] = []
        for entry in scope_raw:
            normalized, failure = _normalize_scope_entry(entry, workspace, broker)
            if failure is not None:
                return None, _invalid(f"[{title}] {failure.message}")
            if normalized and normalized not in scope:
                scope.append(normalized)

        must_read: List[str] = []
        for entry in item.get("must_read") or []:
            normalized, failure = _normalize_scope_entry(entry, workspace, broker)
            if failure is not None:
                prefix = failure.message
                if not prefix.startswith("scope"):
                    prefix = f"must_read {prefix}"
                return None, _invalid(f"[{title}] {prefix}")
            if normalized and normalized not in must_read:
                must_read.append(normalized)

        if not item.get("commands"):
            return None, _invalid(
                f"[{title}] 缺少验收命令：没有可运行的验收命令，机器就无法判定这个任务算不算完成。"
                f"纯文档类改动可用 [\"python\",\"-m\",\"py_compile\",\"<文件>\"] 之类的语法检查兜底。")

        commands: List[Tuple[str, ...]] = []
        for argv in item.get("commands") or []:
            argv0 = normalize_argv0(argv[0])
            if argv0 not in ARGV_ALLOWLIST:
                return None, _invalid(
                    f"[{title}] 验收命令 {argv[0]} 不在允许列表 "
                    f"({', '.join(sorted(ARGV_ALLOWLIST))})")
            if argv0 == "pip":
                sub = str(argv[1]).lower() if len(argv) > 1 else ""
                if sub not in PIP_SUBCOMMANDS:
                    return None, _invalid(
                        f"[{title}] 验收命令里的 pip 只允许 {', '.join(sorted(PIP_SUBCOMMANDS))}")
            commands.append(tuple(argv))

        depends: List[str] = []
        for token in item.get("depends_on") or []:
            dep_id = resolve_dep(token)
            if dep_id is None:
                return None, _invalid(
                    f"[{title}] 的 depends_on 引用了不存在的任务「{token}」；"
                    f"请用任务序号（从 1 开始）或标题原文")
            if dep_id == task_id:
                return None, _invalid(f"[{title}] 依赖了自己")
            if dep_id not in depends:
                depends.append(dep_id)

        nodes[task_id] = TaskNode(
            id=task_id,
            title=title,
            detail=item.get("detail") or "",
            depends_on=tuple(depends),
            scope=tuple(scope),
            acceptance=Acceptance(commands=tuple(commands), must_read=tuple(must_read)),
            max_attempts=limits.max_attempts,
        )

    # ④ 无环校验（Kahn）。有环说明计划不成立，直接判无效而不是「尽力执行」。
    order = _topological_order(nodes)
    if order is None:
        return None, _invalid("任务依赖存在环，请改成有向无环的计划")

    # ⑤ 无依赖的先跑
    for node in nodes.values():
        if not node.depends_on:
            node.status = TaskStatus.ready

    return TaskGraph(summary=raw.get("summary") or "", tasks=nodes, order=order), None


def _topological_order(nodes: Dict[str, TaskNode]) -> Optional[List[str]]:
    """
    Kahn 拓扑排序。返回稳定顺序（同层按 id 顺序），有环返回 None。

    稳定性很重要：任务图是并行调度与合并排序的依据，顺序漂移会让「同一份计划
    两次运行得到不同执行顺序」，那可复现性就没了。
    """
    indegree = {tid: 0 for tid in nodes}
    edges: Dict[str, List[str]] = {tid: [] for tid in nodes}
    for tid, node in nodes.items():
        for dep in node.depends_on:
            if dep not in nodes:
                return None
            edges[dep].append(tid)
            indegree[tid] += 1

    ready = sorted([tid for tid, deg in indegree.items() if deg == 0],
                   key=lambda t: (len(t), t))
    order: List[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in sorted(edges[current], key=lambda t: (len(t), t)):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort(key=lambda t: (len(t), t))

    if len(order) != len(nodes):
        return None
    return order
